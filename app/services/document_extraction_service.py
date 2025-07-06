# project_root/app/services/document_extraction_service.py

import base64
import io
import os
import asyncio
import logging
from typing import Optional, List, Tuple, Dict, Any
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
import time

from pypdf import PdfReader
from PIL import Image, ImageOps
from pdf2image import convert_from_bytes
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage

from app.config import (
    GOOGLE_API_KEY, MODEL_TEXT_EXTRACTION, MODEL_DATA_ANALYSIS,
    RAW_OCR_PROMPT, DATA_STRUCTURING_PROMPT
)

Image.MAX_IMAGE_PIXELS = None

logger = logging.getLogger(__name__)

# --- NEW: Custom Exception for clear error propagation ---
class DocumentExtractionError(Exception):
    """Custom exception for errors during the document extraction process."""
    pass

# --- Optimized Single-Pass OCR Implementation ---
class OptimizedGeminiProcessor:
    """Optimized single-pass Gemini processor for faster OCR"""
    
    def __init__(self):
        self.llm = ChatGoogleGenerativeAI(
            model=MODEL_TEXT_EXTRACTION,
            google_api_key=GOOGLE_API_KEY,
            temperature=0.1,
            request_timeout=30.0,
            max_retries=2
        )
        self.executor = ThreadPoolExecutor(max_workers=4)
    
    # MODIFIED: Accepts a context dictionary for improved logging
    def process_document_single_pass(self, image_bytes: bytes, context: dict) -> str:
        """Single-pass OCR with built-in structuring to reduce API calls"""
        try:
            combined_prompt = f"""
            {RAW_OCR_PROMPT}
            
            Additionally, after extracting the text, please structure it into clean Markdown format following these guidelines:
            {DATA_STRUCTURING_PROMPT}
            
            Please provide the final structured Markdown output directly, not the raw OCR text.
            """
            
            encoded_image = base64.b64encode(image_bytes).decode('utf-8')
            image_url_content = f"data:image/jpeg;base64,{encoded_image}"

            messages = [
                HumanMessage(
                    content=[
                        {"type": "text", "text": combined_prompt},
                        {"type": "image_url", "image_url": {"url": image_url_content}},
                    ]
                )
            ]
            
            start_time = time.time()
            response = self.llm.invoke(messages)
            processing_time = time.time() - start_time
            
            logger.info(f"Context: {context} - Single-pass OCR completed in {processing_time:.2f} seconds.")
            return response.content
            
        except Exception as e:
            # MODIFIED: Now raises the custom exception
            logger.error(f"Context: {context} - Error in optimized OCR: {e}", exc_info=True)
            raise DocumentExtractionError(f"AI model failed during OCR processing: {e}")

# --- Streamlined Image Processing ---
class FastImageProcessor:
    """Lightweight image processing focused on speed"""
    
    MAX_DIMENSION = 4096

    @staticmethod
    # MODIFIED: Accepts a context dictionary for improved logging
    def quick_enhance(image_bytes: bytes, context: dict) -> bytes:
        """Quick image enhancement focused on OCR performance vs quality trade-off"""
        try:
            img = Image.open(io.BytesIO(image_bytes))
            
            if img.width > FastImageProcessor.MAX_DIMENSION or img.height > FastImageProcessor.MAX_DIMENSION:
                img.thumbnail((FastImageProcessor.MAX_DIMENSION, FastImageProcessor.MAX_DIMENSION), Image.Resampling.LANCZOS)
            
            if img.mode != 'RGB':
                img = img.convert('RGB')
            
            buffer = io.BytesIO()
            img.save(buffer, format='JPEG', quality=90, optimize=True)
            return buffer.getvalue()
            
        except Exception as e:
            logger.warning(f"Context: {context} - Quick enhancement failed, using original image: {e}")
            return image_bytes

# --- Optimized Main Service Class ---
class FastDocumentExtractor:
    def __init__(self):
        self.image_processor = FastImageProcessor()
        self.ocr_processor = OptimizedGeminiProcessor()

    # MODIFIED: Raises an exception on failure instead of returning a tuple
    def _decode_base64(self, b64_data: str) -> bytes:
        """Fast base64 decoding that raises an exception on failure."""
        try:
            if ',' in b64_data:
                b64_data = b64_data.split(',', 1)[1]
            return base64.b64decode(b64_data, validate=True)
        except Exception as e:
            raise DocumentExtractionError(f"Invalid Base64 input: {e}")

    def _pdf_to_image_fast(self, pdf_bytes: bytes, page_num: int = 0) -> bytes:
        """Fast PDF to image conversion for single page"""
        try:
            images = convert_from_bytes(
                pdf_bytes, 
                dpi=200,
                fmt='jpeg',
                first_page=page_num + 1,
                last_page=page_num + 1,
                thread_count=1,
                poppler_path=None
            )
            
            if not images:
                raise ValueError("No image extracted from PDF page.")
            
            with io.BytesIO() as buf:
                images[0].save(buf, format='JPEG', quality=85, optimize=True)
                return buf.getvalue()
            
        except Exception as e:
            logger.error(f"Fast PDF conversion failed for page {page_num+1}: {e}")
            raise  # Re-raise the original exception

    # RENAMED: 'extract_text_fast' -> 'extract_text_from_document' for clarity
    # MODIFIED: Added 'context_log_id' for traceable logging
    async def extract_text_from_document(self, file_base64_data: str, file_extension: str, context_log_id: str) -> str:
        """
        Optimized extraction method that raises exceptions on failure.
        """
        start_time = time.time()
        log_context = {"file_ext": file_extension, "id": context_log_id}

        try:
            file_bytes = self._decode_base64(file_base64_data)

            if file_extension.lower() == 'pdf':
                reader = PdfReader(io.BytesIO(file_bytes))
                num_pages = len(reader.pages)
                logger.info(f"Context: {log_context} - PDF with {num_pages} pages detected.")
                
                all_pages_text = []
                for page_num in range(num_pages):
                    page_log_context = {**log_context, "page": f"{page_num + 1}/{num_pages}"}
                    
                    image_bytes = await asyncio.to_thread(
                        self._pdf_to_image_fast, file_bytes, page_num
                    )
                    enhanced_bytes = await asyncio.to_thread(
                        self.image_processor.quick_enhance, image_bytes, page_log_context
                    )
                    page_result = await asyncio.to_thread(
                        self.ocr_processor.process_document_single_pass, enhanced_bytes, page_log_context
                    )
                    all_pages_text.append(f"## Page {page_num + 1}\n\n{page_result}")
                
                result = "\n\n---\n\n".join(all_pages_text)

            elif file_extension.lower() in {"png", "jpg", "jpeg", "webp", "bmp", "tiff"}:
                logger.info(f"Context: {log_context} - Single image detected.")
                enhanced_bytes = await asyncio.to_thread(
                    self.image_processor.quick_enhance, file_bytes, log_context
                )
                result = await asyncio.to_thread(
                    self.ocr_processor.process_document_single_pass, enhanced_bytes, log_context
                )
            else:
                raise DocumentExtractionError(f"Unsupported file format '{file_extension}'")

            total_time = time.time() - start_time
            logger.info(f"Context: {log_context} - Total extraction completed in {total_time:.2f} seconds.")
            return result
            
        except Exception as e:
            # MODIFIED: All errors are caught and re-raised as the custom exception
            logger.error(f"Context: {log_context} - Document extraction failed: {e}", exc_info=True)
            if isinstance(e, DocumentExtractionError):
                raise  # Re-raise the already specific exception
            raise DocumentExtractionError(f"An unexpected error occurred in extraction: {e}")

# --- Dependency Injection (Unchanged) ---
_extractor_instance: Optional[FastDocumentExtractor] = None

@asynccontextmanager
async def lifespan(app):
    """Optimized application lifespan manager"""
    global _extractor_instance
    try:
        if _extractor_instance is None:
            _extractor_instance = FastDocumentExtractor()
            logger.info("FastDocumentExtractor initialized.")
        yield
    except Exception as e:
        logger.critical(f"FastDocumentExtractor startup error: {e}", exc_info=True)
        raise
    finally:
        if _extractor_instance and hasattr(_extractor_instance.ocr_processor, 'executor'):
            _extractor_instance.ocr_processor.executor.shutdown(wait=True)
        logger.info("FastDocumentExtractor shutdown complete.")

async def get_text_extractor() -> FastDocumentExtractor:
    """Fast dependency injection"""
    if _extractor_instance is None:
        raise RuntimeError("FastDocumentExtractor is not initialized. The application may not have started correctly.")
    return _extractor_instance

# --- Simplified Public API ---
# MODIFIED: This function now passes a context ID for logging
async def extract_text_from_file(file_base64_data: str, file_extension: str, record_id: str) -> str:
    """
    High-speed document extraction that propagates errors via exceptions.
    A record_id is required for traceable logging.
    """
    extractor = await get_text_extractor()
    # RENAMED: Calling the renamed method
    return await extractor.extract_text_from_document(file_base64_data, file_extension, context_log_id=record_id)