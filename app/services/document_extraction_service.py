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

logger = logging.getLogger(__name__)

# --- Optimized Single-Pass OCR Implementation ---

class OptimizedGeminiProcessor:
    """Optimized single-pass Gemini processor for faster OCR"""
    
    def __init__(self):
        # Use faster model for OCR if available
        self.llm = ChatGoogleGenerativeAI(
            model=MODEL_TEXT_EXTRACTION,  # Consider using gemini-1.5-flash for speed
            google_api_key=GOOGLE_API_KEY,
            temperature=0.1,
            # Add timeout and retry configurations
            request_timeout=30.0,
            max_retries=2
        )
        
        # Thread pool for CPU-intensive operations
        self.executor = ThreadPoolExecutor(max_workers=4)
    
    def process_document_single_pass(self, image_bytes: bytes) -> str:
        """Single-pass OCR with built-in structuring to reduce API calls"""
        try:
            # Combined prompt for OCR + structuring in one go
            combined_prompt = f"""
            {RAW_OCR_PROMPT}
            
            Additionally, after extracting the text, please structure it into clean Markdown format following these guidelines:
            {DATA_STRUCTURING_PROMPT}
            
            Please provide the final structured Markdown output directly, not the raw OCR text.
            """
            
            # Encode image to base64
            encoded_image = base64.b64encode(image_bytes).decode('utf-8')
            image_url_content = f"data:image/png;base64,{encoded_image}"

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
            
            logger.info(f"Single-pass OCR completed in {processing_time:.2f} seconds")
            return response.content
            
        except Exception as e:
            logger.error(f"Error in optimized OCR: {e}", exc_info=True)
            return f"OCR Error: {str(e)}"

# --- Streamlined Image Processing ---
class FastImageProcessor:
    """Lightweight image processing focused on speed"""
    
    @staticmethod
    def quick_enhance(image_bytes: bytes) -> bytes:
        """Quick image enhancement focused on OCR performance vs quality trade-off"""
        try:
            img = Image.open(io.BytesIO(image_bytes))
            
            # Skip enhancement for already good quality images
            width, height = img.size
            if width >= 1200 and height >= 1200:
                # Image is already high quality, return as-is
                return image_bytes
            
            # Minimal processing for speed
            if img.mode != 'RGB':
                img = img.convert('RGB')
            
            # Only resize if image is very small
            if width < 800 or height < 800:
                scale_factor = max(800 / width, 800 / height)
                new_size = (int(width * scale_factor), int(height * scale_factor))
                img = img.resize(new_size, Image.Resampling.LANCZOS)
            
            # Save with optimized settings
            buffer = io.BytesIO()
            img.save(buffer, format='JPEG', quality=85, optimize=True)
            return buffer.getvalue()
            
        except Exception as e:
            logger.warning(f"Quick enhancement failed, using original: {e}")
            return image_bytes

# --- Optimized Main Service Class ---
class FastDocumentExtractor:
    def __init__(self):
        self.image_processor = FastImageProcessor()
        self.ocr_processor = OptimizedGeminiProcessor()

    def _decode_base64(self, b64_data: str) -> Tuple[bool, bytes, str]:
        """Fast base64 decoding with minimal validation"""
        try:
            if ',' in b64_data:
                b64_data = b64_data.split(',', 1)[1]
            return True, base64.b64decode(b64_data, validate=True), ""
        except Exception as e:
            return False, b'', f"Invalid base64: {str(e)}"

    def _pdf_to_image_fast(self, pdf_bytes: bytes, page_num: int = 0) -> bytes:
        """Fast PDF to image conversion for single page"""
        try:
            # Lower DPI for speed vs quality trade-off
            images = convert_from_bytes(
                pdf_bytes, 
                dpi=200,  # Reduced from 300 for speed
                fmt='jpeg',  # JPEG is faster than PNG
                first_page=page_num + 1,
                last_page=page_num + 1,  # Only convert needed page
                thread_count=1,  # Reduce threading overhead for single page
                poppler_path=None
            )
            
            if not images:
                raise ValueError("No images extracted from PDF")
            
            with io.BytesIO() as buf:
                images[0].save(buf, format='JPEG', quality=85, optimize=True)
                return buf.getvalue()
            
        except Exception as e:
            logger.error(f"Fast PDF conversion failed: {e}")
            raise e
    
    async def extract_text_fast(self, file_base64_data: str, file_extension: str) -> str:
        """Optimized extraction method prioritizing speed"""
        
        start_time = time.time()
        
        # Fast decode
        success, file_bytes, error = self._decode_base64(file_base64_data)
        if not success:
            return f"Error: {error}"

        try:
            # Process different file types with speed optimizations
            if file_extension.lower() == 'pdf':
                # Only process first page by default for speed
                image_bytes = await asyncio.to_thread(
                    self._pdf_to_image_fast, 
                    file_bytes, 
                    0  # First page only
                )
                
            elif file_extension.lower() in {"png", "jpg", "jpeg", "webp", "bmp", "tiff"}:
                image_bytes = file_bytes
                
            else:
                return f"Error: Unsupported file format '{file_extension}'"

            # Quick enhancement in thread pool
            enhanced_image_bytes = await asyncio.to_thread(
                self.image_processor.quick_enhance, 
                image_bytes
            )

            # Single-pass OCR processing
            result = await asyncio.to_thread(
                self.ocr_processor.process_document_single_pass, 
                enhanced_image_bytes
            )
            
            total_time = time.time() - start_time
            logger.info(f"Total extraction completed in {total_time:.2f} seconds")
            
            return result
            
        except Exception as e:
            logger.error(f"Fast extraction failed: {e}", exc_info=True)
            return f"Error: Fast extraction failed - {str(e)}"

    async def extract_text_batch(self, file_data_list: List[Tuple[str, str]]) -> List[str]:
        """Batch processing for multiple files with concurrency"""
        try:
            # Process multiple files concurrently
            tasks = [
                self.extract_text_fast(file_data, extension) 
                for file_data, extension in file_data_list
            ]
            
            # Limit concurrency to avoid overwhelming the API
            semaphore = asyncio.Semaphore(3)  # Max 3 concurrent operations
            
            async def process_with_semaphore(task):
                async with semaphore:
                    return await task
            
            results = await asyncio.gather(*[
                process_with_semaphore(task) for task in tasks
            ])
            
            return results
            
        except Exception as e:
            logger.error(f"Batch processing failed: {e}", exc_info=True)
            return [f"Error: Batch processing failed - {str(e)}"]

    async def extract_pdf_pages_selective(self, file_base64_data: str, max_pages: int = 5) -> List[str]:
        """Extract from PDF with page limit for speed"""
        success, file_bytes, error = self._decode_base64(file_base64_data)
        if not success:
            return [f"Error: {error}"]

        try:
            # Quick page count check
            reader = PdfReader(io.BytesIO(file_bytes))
            total_pages = len(reader.pages)
            pages_to_process = min(total_pages, max_pages)
            
            logger.info(f"Processing {pages_to_process} of {total_pages} pages for speed")
            
            # Process pages concurrently with limit
            async def process_page(page_num):
                try:
                    image_bytes = await asyncio.to_thread(
                        self._pdf_to_image_fast, 
                        file_bytes, 
                        page_num
                    )
                    enhanced_image = await asyncio.to_thread(
                        self.image_processor.quick_enhance, 
                        image_bytes
                    )
                    result = await asyncio.to_thread(
                        self.ocr_processor.process_document_single_pass, 
                        enhanced_image
                    )
                    return f"## Page {page_num + 1}\n\n{result}"
                except Exception as e:
                    return f"## Page {page_num + 1}\n\nError: {str(e)}"
            
            # Concurrent processing with semaphore
            semaphore = asyncio.Semaphore(2)  # Max 2 pages at once
            
            async def process_page_with_semaphore(page_num):
                async with semaphore:
                    return await process_page(page_num)
            
            tasks = [
                process_page_with_semaphore(i) 
                for i in range(pages_to_process)
            ]
            
            results = await asyncio.gather(*tasks)
            
            if pages_to_process < total_pages:
                results.append(f"\n---\n**Note**: Only processed {pages_to_process} of {total_pages} pages for performance. Use full extraction if needed.")
            
            return results
            
        except Exception as e:
            logger.error(f"Selective PDF extraction failed: {e}", exc_info=True)
            return [f"Error: Selective extraction failed - {str(e)}"]

# --- Dependency Injection with Connection Pooling ---
_extractor_instance: Optional[FastDocumentExtractor] = None

@asynccontextmanager
async def lifespan(app):
    """Optimized application lifespan manager"""
    global _extractor_instance
    try:
        if _extractor_instance is None:
            _extractor_instance = FastDocumentExtractor()
            logger.info("FastDocumentExtractor initialized")
        
        yield
        
    except Exception as e:
        logger.error(f"Startup error: {e}")
        raise
    finally:
        # Cleanup thread pools
        if _extractor_instance and hasattr(_extractor_instance.ocr_processor, 'executor'):
            _extractor_instance.ocr_processor.executor.shutdown(wait=True)
        logger.info("FastDocumentExtractor shutdown complete")

async def get_text_extractor() -> FastDocumentExtractor:
    """Fast dependency injection"""
    if _extractor_instance is None:
        raise RuntimeError("FastDocumentExtractor not initialized")
    return _extractor_instance

# --- Optimized Public API ---
async def extract_text_from_file(file_base64_data: str, file_extension: str) -> str:
    """High-speed single document extraction"""
    extractor = await get_text_extractor()
    return await extractor.extract_text_fast(file_base64_data, file_extension)

async def extract_text_from_pdf_limited(file_base64_data: str, max_pages: int = 5) -> List[str]:
    """Fast PDF extraction with page limits"""
    extractor = await get_text_extractor()
    return await extractor.extract_pdf_pages_selective(file_base64_data, max_pages)

async def extract_text_batch_processing(file_data_list: List[Tuple[str, str]]) -> List[str]:
    """Batch processing for multiple files"""
    extractor = await get_text_extractor()
    return await extractor.extract_text_batch(file_data_list)

# --- Performance Monitoring ---
class PerformanceMonitor:
    """Simple performance monitoring for optimization"""
    
    def __init__(self):
        self.metrics = {}
    
    def record_timing(self, operation: str, duration: float):
        if operation not in self.metrics:
            self.metrics[operation] = []
        self.metrics[operation].append(duration)
    
    def get_average_time(self, operation: str) -> float:
        if operation not in self.metrics:
            return 0.0
        return sum(self.metrics[operation]) / len(self.metrics[operation])
    
    def get_performance_report(self) -> Dict[str, float]:
        return {
            operation: self.get_average_time(operation) 
            for operation in self.metrics
        }

# Global performance monitor
performance_monitor = PerformanceMonitor()