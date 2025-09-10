# document_extraction_service.py - Gemini OCR for all images and PDF pages

import base64
import io
import os
import asyncio
import logging
from typing import Optional, List, Tuple, Dict, Any
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
import time

import fitz  # PyMuPDF
from PIL import Image, ImageOps
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage

from app.config import (
    DOC_GOOGLE_API_KEY, MODEL_TEXT_EXTRACTION, MODEL_DATA_ANALYSIS,
    RAW_OCR_PROMPT, DATA_STRUCTURING_PROMPT
)

# Set reasonable limit for PIL
Image.MAX_IMAGE_PIXELS = 178956970

logger = logging.getLogger(__name__)

# --- Custom Exception ---
class DocumentExtractionError(Exception):
    """Custom exception for errors during the document extraction process."""
    pass

# --- Optimized Gemini Processor ---
class OptimizedGeminiProcessor:
    """Single-pass Gemini processor for all OCR tasks"""
    
    def __init__(self):
        self.llm = ChatGoogleGenerativeAI(
            model=MODEL_TEXT_EXTRACTION,
            google_api_key=DOC_GOOGLE_API_KEY,
            temperature=0.1,
            request_timeout=30.0,
            max_retries=2
        )
        self.executor = ThreadPoolExecutor(max_workers=4)
    
    def process_document_single_pass(self, image_bytes: bytes, context: dict) -> str:
        """Single-pass OCR with built-in structuring - used for ALL content"""
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
            
            logger.info(f"Context: {context} - Gemini OCR completed in {processing_time:.2f} seconds.")
            return response.content
            
        except Exception as e:
            logger.error(f"Context: {context} - Error in Gemini OCR: {e}", exc_info=True)
            raise DocumentExtractionError(f"AI model failed during OCR processing: {e}")

# --- Fast Image Processor ---
class FastImageProcessor:
    """Lightweight image processing for optimization"""
    
    MAX_DIMENSION = 4096

    @staticmethod
    def quick_enhance(image_bytes: bytes, context: dict) -> bytes:
        """Quick image enhancement for better OCR results"""
        try:
            img = Image.open(io.BytesIO(image_bytes))
            
            # Resize if too large
            if img.width > FastImageProcessor.MAX_DIMENSION or img.height > FastImageProcessor.MAX_DIMENSION:
                img.thumbnail((FastImageProcessor.MAX_DIMENSION, FastImageProcessor.MAX_DIMENSION), Image.Resampling.LANCZOS)
            
            # Convert to RGB
            if img.mode != 'RGB':
                img = img.convert('RGB')
            
            # Save as optimized JPEG
            buffer = io.BytesIO()
            img.save(buffer, format='JPEG', quality=90, optimize=True)
            return buffer.getvalue()
            
        except Exception as e:
            logger.warning(f"Context: {context} - Enhancement failed, using original: {e}")
            return image_bytes

# --- PDF Handler using PyMuPDF ---
class MuPDFHandler:
    """Handles PDF to image conversion using PyMuPDF"""
    
    def __init__(self, config: dict):
        self.config = config
    
    def convert_pdf_page_to_image(self, pdf_doc: fitz.Document, page_num: int, context: dict) -> bytes:
        """Convert a single PDF page to image for Gemini OCR"""
        try:
            page = pdf_doc[page_num]
            
            # Render at specified DPI
            zoom = self.config['dpi'] / 72.0
            matrix = fitz.Matrix(zoom, zoom)
            
            # Get page as image
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            
            # Convert to PIL Image
            img_data = pix.tobytes("ppm")
            img = Image.open(io.BytesIO(img_data))
            
            # Convert to JPEG for consistency
            buffer = io.BytesIO()
            img.save(buffer, format='JPEG', quality=self.config['jpeg_quality'], optimize=True)
            return buffer.getvalue()
            
        except Exception as e:
            logger.error(f"Context: {context} - Error rendering PDF page {page_num + 1}: {e}")
            raise

# --- Main Document Extractor ---
class FastDocumentExtractor:
    def __init__(self):
        self.image_processor = FastImageProcessor()
        self.ocr_processor = OptimizedGeminiProcessor()
        self.pdf_handler = MuPDFHandler({
            'dpi': 200,  # Good balance of quality and size
            'jpeg_quality': 85
        })

    def _decode_base64(self, b64_data: str) -> bytes:
        """Decode base64 data"""
        try:
            if ',' in b64_data:
                b64_data = b64_data.split(',', 1)[1]
            return base64.b64decode(b64_data, validate=True)
        except Exception as e:
            raise DocumentExtractionError(f"Invalid Base64 input: {e}")

    async def _process_pdf_with_gemini(self, pdf_bytes: bytes, log_context: dict) -> str:
        """Process PDF by converting each page to image and using Gemini OCR"""
        all_pages_text = []
        doc = None
        
        try:
            # Open PDF with PyMuPDF
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            
            # Check if password protected
            if doc.is_encrypted and doc.needs_pass:
                return "## Document Processing Error\n\nThis PDF is password protected and cannot be processed."
            
            num_pages = len(doc)
            logger.info(f"Context: {log_context} - PDF with {num_pages} pages detected.")
            
            # Process each page
            for page_num in range(num_pages):
                page_context = {**log_context, "page": f"{page_num + 1}/{num_pages}"}
                
                try:
                    # Convert PDF page to image
                    logger.info(f"Context: {page_context} - Converting page to image for OCR")
                    image_bytes = await asyncio.to_thread(
                        self.pdf_handler.convert_pdf_page_to_image, doc, page_num, page_context
                    )
                    
                    # Enhance image
                    enhanced_bytes = await asyncio.to_thread(
                        self.image_processor.quick_enhance, image_bytes, page_context
                    )
                    
                    # OCR with Gemini
                    page_text = await asyncio.to_thread(
                        self.ocr_processor.process_document_single_pass, enhanced_bytes, page_context
                    )
                    
                    all_pages_text.append(f"## Page {page_num + 1}\n\n{page_text}")
                    
                except Exception as page_error:
                    logger.error(f"Context: {page_context} - Error processing page: {page_error}")
                    # Handle page-level errors gracefully
                    error_text = (
                        f"## Page {page_num + 1}\n\n"
                        f"*Error: This page could not be processed.*\n"
                        f"Reason: {str(page_error)[:200]}"
                    )
                    all_pages_text.append(error_text)
            
            # Join all pages
            result = "\n\n---\n\n".join(all_pages_text) if all_pages_text else "No content could be extracted from this PDF."
            return result
            
        except Exception as e:
            logger.error(f"Context: {log_context} - Critical error in PDF processing: {e}", exc_info=True)
            
            # Return informative error message
            return (
                f"## Document Processing Error\n\n"
                f"This PDF document could not be processed.\n\n"
                f"**Error Details:**\n"
                f"- Error type: {type(e).__name__}\n"
                f"- Description: {str(e)[:500]}\n\n"
                f"**Possible causes:**\n"
                f"- Corrupted PDF structure\n"
                f"- Unsupported PDF features\n"
                f"- Processing timeout\n\n"
                f"Please try providing the document in a different format if possible."
            )
        
        finally:
            if doc:
                doc.close()

    async def extract_text_from_document(self, file_base64_data: str, file_extension: str, context_log_id: str) -> str:
        """
        Extract text from document using Gemini OCR for ALL content
        """
        start_time = time.time()
        log_context = {"file_ext": file_extension, "id": context_log_id}

        try:
            file_bytes = self._decode_base64(file_base64_data)

            if file_extension.lower() == 'pdf':
                # Process PDF by converting each page to image and using Gemini
                result = await self._process_pdf_with_gemini(file_bytes, log_context)
                
            elif file_extension.lower() in {"png", "jpg", "jpeg", "webp", "bmp", "tiff"}:
                # Process image directly with Gemini
                logger.info(f"Context: {log_context} - Processing image file.")
                
                # Enhance image
                enhanced_bytes = await asyncio.to_thread(
                    self.image_processor.quick_enhance, file_bytes, log_context
                )
                
                # OCR with Gemini
                result = await asyncio.to_thread(
                    self.ocr_processor.process_document_single_pass, enhanced_bytes, log_context
                )
            else:
                raise DocumentExtractionError(f"Unsupported file format '{file_extension}'")

            total_time = time.time() - start_time
            logger.info(f"Context: {log_context} - Total extraction completed in {total_time:.2f} seconds.")
            return result
            
        except Exception as e:
            logger.error(f"Context: {log_context} - Document extraction failed: {e}", exc_info=True)
            if isinstance(e, DocumentExtractionError):
                raise
            raise DocumentExtractionError(f"An unexpected error occurred in extraction: {e}")

# --- Dependency Injection ---
_extractor_instance: Optional[FastDocumentExtractor] = None

@asynccontextmanager
async def lifespan(app):
    """Application lifespan manager"""
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
    """Get the document extractor instance"""
    if _extractor_instance is None:
        raise RuntimeError("FastDocumentExtractor is not initialized.")
    return _extractor_instance

# --- Public API ---
async def extract_text_from_file(file_base64_data: str, file_extension: str, record_id: str) -> str:
    """
    Extract text from file using Gemini OCR for all content
    - Images: Direct Gemini OCR
    - PDFs: Convert each page to image, then Gemini OCR
    """
    extractor = await get_text_extractor()
    return await extractor.extract_text_from_document(file_base64_data, file_extension, context_log_id=record_id)