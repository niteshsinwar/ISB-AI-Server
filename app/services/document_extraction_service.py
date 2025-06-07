# project_root/app/services/document_extraction_service.py
import base64
import io
import os
import asyncio
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
import logging
from contextlib import asynccontextmanager

from pypdf import PdfReader
import google.generativeai as genai
from PIL import Image
from pdf2image import convert_from_bytes, pdfinfo_from_bytes # Poppler dependency

from app.config import (
    GOOGLE_API_KEY, GEMINI_MODEL_NAME, TEXT_EXTRACTION_OCR_PROMPT,
    MAX_CONCURRENT_OCR_PAGES
)

logger = logging.getLogger(__name__)

class ExtractionMethod(Enum):
    DIRECT_PDF = "direct_pdf"
    OCR_GEMINI = "ocr_gemini"

@dataclass
class ExtractionResult:
    success: bool
    text: str
    method: Optional[ExtractionMethod] = None
    page_count: int = 0
    error_message: Optional[str] = None
    warnings: List[str] = field(default_factory=list)

@dataclass
class DocumentInfo:
    file_bytes: bytes
    extension: str
    size_kb: float
    mime_type: Optional[str] = None

class DocumentTextExtractor:
    SUPPORTED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif", "bmp", "heic", "tif", "tiff"}
    MIME_TYPE_MAP = {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "webp": "image/webp", "gif": "image/gif", "bmp": "image/bmp",
        "heic": "image/heic", "tif": "image/tiff", "tiff": "image/tiff",
        "pdf": "application/pdf"
    }
    OCR_DPI = 200
    OCR_FORMAT = 'png'
    MIN_MEANINGFUL_TEXT_LENGTH = 20

    def __init__(self):
        self.gemini_model: Optional[genai.GenerativeModel] = None
        self.ocr_prompt: str = TEXT_EXTRACTION_OCR_PROMPT
        self._initialize_gemini()

    def _initialize_gemini(self):
        if not GOOGLE_API_KEY:
            logger.warning("GOOGLE_API_KEY not found. OCR functionality (Gemini) will be unavailable.")
            return
        try:
            genai.configure(api_key=GOOGLE_API_KEY)
            self.gemini_model = genai.GenerativeModel(GEMINI_MODEL_NAME)
            logger.info(f"Gemini Vision model for OCR initialized successfully ('{GEMINI_MODEL_NAME}').")
        except Exception as e:
            logger.error(f"Failed to initialize Gemini model ('{GEMINI_MODEL_NAME}') for OCR: {e}", exc_info=True)
            self.gemini_model = None

    def _decode_base64_file(self, file_base64_data: str) -> Tuple[bool, bytes, str]:
        if not file_base64_data:
            return False, b'', "No file data provided (base64 string is empty)"
        try:
            file_bytes = base64.b64decode(file_base64_data, validate=True)
            if not file_bytes:
                return False, b'', "Base64 decoded to empty data"
            return True, file_bytes, ""
        except base64.binascii.Error as e:
            logger.error(f"Base64 decoding error: {e}", exc_info=True)
            return False, b'', f"Invalid base64 data: {str(e)}"
        except Exception as e:
            logger.error(f"Unexpected error during base64 decoding: {e}", exc_info=True)
            return False, b'', f"Unexpected error decoding base64: {str(e)}"

    def _create_document_info(self, file_bytes: bytes, extension: str) -> DocumentInfo:
        size_kb = len(file_bytes) / 1024.0
        ext_lower = extension.lower().lstrip('.')
        mime_type = self.MIME_TYPE_MAP.get(ext_lower)
        if not mime_type:
             logger.warning(f"Could not determine MIME type for extension '{ext_lower}'.")
        return DocumentInfo(
            file_bytes=file_bytes, extension=ext_lower,
            size_kb=size_kb, mime_type=mime_type
        )

    def _sync_extract_pdf_text(self, file_bytes: bytes) -> Tuple[bool, str, int, bool]:
        """Synchronous helper for PyPDF processing to run in a thread."""
        try:
            pdf_reader = PdfReader(io.BytesIO(file_bytes))
            page_count = len(pdf_reader.pages)
            if page_count == 0:
                return False, "PDF contains no pages (PyPDF)", 0, False

            extracted_pages = []
            meaningful_text_found = False
            for i, page in enumerate(pdf_reader.pages):
                try:
                    page_text = page.extract_text()
                    if page_text:
                        clean_text = page_text.strip()
                        if clean_text:
                            extracted_pages.append(clean_text)
                            if len(clean_text) >= self.MIN_MEANINGFUL_TEXT_LENGTH:
                                meaningful_text_found = True
                except Exception as page_e:
                    logger.warning(f"PyPDF: Error extracting text from page {i+1}: {page_e}")
                    extracted_pages.append(f"[Error on page {i+1}: PyPDF extraction failed]")
            
            full_text = "\n\n--- Page Break (Direct) ---\n\n".join(extracted_pages)
            return True, full_text, page_count, meaningful_text_found
        except Exception as e:
            error_msg = str(e)
            logger.error(f"PyPDF processing error in thread: {error_msg}", exc_info=True)
            if "PdfReadError" in str(type(e)) or "EOF marker not found" in error_msg or "Invalid PDF" in error_msg:
                error_msg = f"Corrupted or invalid PDF structure: {error_msg}"
            return False, f"PDF processing error (direct): {error_msg}", 0, False

    async def _extract_pdf_text_direct(self, doc_info: DocumentInfo) -> ExtractionResult:
        logger.info("Attempting direct PDF text extraction in a background thread...")
        
        success, result_data, page_count, meaningful_text_found = await asyncio.to_thread(
            self._sync_extract_pdf_text, doc_info.file_bytes
        )

        if not success:
            return ExtractionResult(success=False, text="", error_message=result_data, page_count=page_count)

        if meaningful_text_found:
            logger.info(f"Successfully extracted text from {page_count} pages using direct PDF method.")
            return ExtractionResult(success=True, text=result_data, method=ExtractionMethod.DIRECT_PDF, page_count=page_count)
        else:
            logger.info("PyPDF: No meaningful text found via direct extraction. Text might be image-based or empty.")
            return ExtractionResult(
                success=False,
                text=result_data,
                error_message="No meaningful text found via direct PDF extraction (document might be image-based or empty)",
                page_count=page_count
            )

    async def _extract_text_from_image_with_gemini(self, image_bytes: bytes, mime_type: str, page_num: int = 0) -> Tuple[bool, str, str]:
        if not self.gemini_model:
            return False, "", "OCR Error: Gemini model not available"

        page_id = f"Page {page_num}" if page_num > 0 else "Image"
        try:
            logger.info(f"Processing {page_id} with Gemini Vision ({mime_type}, {len(image_bytes)//1024} KB)")
            image_part = {"mime_type": mime_type, "data": image_bytes}
            
            response = await self.gemini_model.generate_content_async([self.ocr_prompt, image_part])

            if response.parts:
                extracted_text = "".join(part.text for part in response.parts if hasattr(part, 'text'))
                processed_text = extracted_text.strip()
                if processed_text and processed_text.upper() != "NO_TEXT_FOUND":
                    logger.info(f"Successfully extracted text from {page_id} using Gemini.")
                    return True, processed_text, ""
                else:
                    logger.info(f"No significant text found in {page_id} by Gemini (or model indicated 'NO_TEXT_FOUND').")
                    return True, "", ""
            elif hasattr(response, 'prompt_feedback') and response.prompt_feedback and response.prompt_feedback.block_reason:
                block_reason = response.prompt_feedback.block_reason
                error_msg = f"Content blocked by Gemini for {page_id}: {block_reason}"
                logger.warning(error_msg)
                return False, "", error_msg
            else:
                candidate_info = str(response.candidates[0])[:200] if hasattr(response, 'candidates') and response.candidates else 'No candidates'
                logger.warning(f"Unexpected Gemini response for {page_id}. No parts or block reason. Candidate: {candidate_info}")
                return False, "", f"Unexpected API response from Gemini for {page_id}"
        except Exception as e:
            error_msg = f"Gemini API error for {page_id}: {str(e)}"
            logger.error(error_msg, exc_info=True)
            if "API key not valid" in str(e): error_msg = "Gemini API Key not valid. Please check configuration."
            elif "Quota" in str(e): error_msg = "Gemini API Quota exceeded."
            return False, "", error_msg

    async def _extract_pdf_text_ocr(self, doc_info: DocumentInfo, pypdf_page_count: int) -> ExtractionResult:
        logger.info("Attempting PDF OCR extraction...")
        if not self.gemini_model:
            return ExtractionResult(success=False, text="", error_message="OCR unavailable: Gemini model not initialized")

        page_count_for_conversion = pypdf_page_count
        if pypdf_page_count <= 0:
            logger.info("PyPDF found 0 pages or failed. Trying Poppler's pdfinfo in a background thread.")
            try:
                pdf_info_meta = await asyncio.to_thread(
                    pdfinfo_from_bytes, doc_info.file_bytes, timeout=30
                )
                poppler_page_count = pdf_info_meta.get('Pages', 0)
                if isinstance(poppler_page_count, str) and poppler_page_count.isdigit():
                    poppler_page_count = int(poppler_page_count)
                
                if poppler_page_count > 0:
                    page_count_for_conversion = poppler_page_count
                    logger.info(f"Poppler (pdfinfo) reports {poppler_page_count} pages. Using this for conversion.")
                else:
                    logger.warning("Poppler (pdfinfo) also reports 0 pages. Cannot proceed with OCR.")
                    return ExtractionResult(success=False, text="", error_message="PDF has no pages (PyPDF and Poppler report 0)")
            except Exception as e:
                logger.error(f"Poppler (pdfinfo_from_bytes) failed: {e}. Cannot determine page count for OCR.", exc_info=True)
                return ExtractionResult(success=False, text="", error_message=f"Failed to get PDF info for OCR (Poppler pdfinfo error): {e}")
        
        if page_count_for_conversion == 0:
             return ExtractionResult(success=False, text="", error_message="PDF has no pages for OCR processing")

        try:
            logger.info(f"Converting {page_count_for_conversion} PDF page(s) to images for OCR in a background thread...")
            conversion_timeout = 15 + (page_count_for_conversion * 10)
            images = await asyncio.to_thread(
                convert_from_bytes,
                doc_info.file_bytes,
                fmt=self.OCR_FORMAT,
                dpi=self.OCR_DPI,
                thread_count=min(os.cpu_count() or 1, 4),
                timeout=conversion_timeout
            )
            if not images:
                logger.warning("Poppler (convert_from_bytes) returned no images despite expected pages.")
                return ExtractionResult(success=False, text="", error_message="Failed to convert PDF to images (Poppler returned no images)")
        except Exception as e:
            logger.error(f"Poppler (convert_from_bytes) failed during PDF to image conversion: {e}", exc_info=True)
            return ExtractionResult(success=False, text="", error_message=f"PDF to image conversion error (Poppler): {e}")

        # --- The rest of the OCR logic is already async-friendly and remains the same ---
        ocr_results, warnings_list = [], []
        actual_images_converted = len(images)
        logger.info(f"Successfully converted {actual_images_converted} PDF page(s) to images. Starting OCR (Batch size: {MAX_CONCURRENT_OCR_PAGES}).")

        for i in range(0, actual_images_converted, MAX_CONCURRENT_OCR_PAGES):
            batch_pil_images = images[i:i + MAX_CONCURRENT_OCR_PAGES]
            tasks_with_pagenum = []
            for j, pil_image in enumerate(batch_pil_images):
                page_num_actual = i + j + 1
                try:
                    img_buffer = io.BytesIO()
                    pil_image.save(img_buffer, format='PNG')
                    img_bytes = img_buffer.getvalue()
                    task = self._extract_text_from_image_with_gemini(img_bytes, "image/png", page_num_actual)
                    tasks_with_pagenum.append({'page_num': page_num_actual, 'task': task})
                except Exception as img_save_err:
                    logger.error(f"Error processing image for page {page_num_actual} before OCR: {img_save_err}")
                    warnings_list.append(f"Page {page_num_actual} pre-OCR processing error: {img_save_err}")
            
            if not tasks_with_pagenum: continue

            try:
                gathered_page_results = await asyncio.gather(*[item['task'] for item in tasks_with_pagenum], return_exceptions=True)
            except Exception as gather_e:
                logger.error(f"Unexpected error during asyncio.gather for OCR tasks (batch from page {tasks_with_pagenum[0]['page_num']}): {gather_e}", exc_info=True)
                for item in tasks_with_pagenum:
                    ocr_results.append(f"[Page {item['page_num']} OCR Batch Exception: {str(gather_e)}]")
                    warnings_list.append(f"Page {item['page_num']} OCR task failed in batch: {str(gather_e)}")
                continue

            for item_idx, result_or_exc in enumerate(gathered_page_results):
                page_num_processed = tasks_with_pagenum[item_idx]['page_num']
                if isinstance(result_or_exc, Exception):
                    error_msg = f"OCR task for page {page_num_processed} failed: {str(result_or_exc)}"
                    logger.error(error_msg, exc_info=result_or_exc)
                    ocr_results.append(f"[Page {page_num_processed} OCR Error: {str(result_or_exc)}]")
                    warnings_list.append(error_msg)
                else:
                    success, text, error_msg_gemini = result_or_exc
                    if success and text:
                        ocr_results.append(text)
                    elif error_msg_gemini:
                        ocr_results.append(f"[Page {page_num_processed} OCR Error: {error_msg_gemini}]")
                        warnings_list.append(f"Page {page_num_processed}: {error_msg_gemini}")
        
        meaningful_pages = [txt for txt in ocr_results if txt and not txt.startswith("[Page")]
        full_text_output = "\n\n--- Page Break (OCR) ---\n\n".join(ocr_results) if ocr_results else ""

        if meaningful_pages:
            logger.info(f"OCR extraction completed: {len(meaningful_pages)}/{actual_images_converted} pages yielded meaningful text.")
            return ExtractionResult(success=True, text=full_text_output, method=ExtractionMethod.OCR_GEMINI, page_count=actual_images_converted, warnings=warnings_list)
        else:
            logger.warning(f"OCR completed but no readable text found across {actual_images_converted} pages.")
            return ExtractionResult(success=False, text=full_text_output, 
                                    error_message="OCR completed but no readable text found", 
                                    page_count=actual_images_converted, warnings=warnings_list)

    async def _extract_image_text(self, doc_info: DocumentInfo) -> ExtractionResult:
        logger.info(f"Processing {doc_info.extension.upper()} image ({doc_info.size_kb:.1f} KB)")
        if not doc_info.mime_type:
            logger.warning(f"MIME type not found for image extension: {doc_info.extension}. Cannot OCR.")
            return ExtractionResult(success=False, text="", error_message=f"Unsupported image format (no MIME type): {doc_info.extension}")

        success, text, error_msg = await self._extract_text_from_image_with_gemini(doc_info.file_bytes, doc_info.mime_type)
        return ExtractionResult(
            success=success and bool(text.strip()),
            text=text, method=ExtractionMethod.OCR_GEMINI if success and text.strip() else None,
            page_count=1, error_message=error_msg if not success or not text.strip() else None
        )

    async def extract_text_from_file_data(self, file_base64_data: str, file_extension: str) -> str:
        """ Main method to extract text. Returns text or "Error: ..." / "Note: ..." string. """
        logger.info(f"Starting text extraction for {file_extension.upper()} file")
        success_decode, file_bytes, error_msg_decode = self._decode_base64_file(file_base64_data)
        if not success_decode:
            logger.error(f"Base64 decoding failed for {file_extension}: {error_msg_decode}")
            return f"Error: {error_msg_decode}"

        doc_info = self._create_document_info(file_bytes, file_extension)
        logger.info(f"Document: {doc_info.extension}, Size: {doc_info.size_kb:.1f} KB, MIME: {doc_info.mime_type}")

        try:
            if doc_info.extension == "pdf":
                direct_result = await self._extract_pdf_text_direct(doc_info)
                if direct_result.success and direct_result.text.strip():
                    logger.info("Direct PDF extraction successful with meaningful text.")
                    return direct_result.text
                
                logger.info(f"Direct PDF extraction found no meaningful text (or failed: {direct_result.error_message}). Attempting OCR. PyPDF page count: {direct_result.page_count}")
                ocr_result = await self._extract_pdf_text_ocr(doc_info, direct_result.page_count)
                
                if ocr_result.success and ocr_result.text.strip():
                    logger.info("OCR PDF extraction successful with meaningful text.")
                    result_text = ocr_result.text
                    if ocr_result.warnings: result_text += f"\n\n[OCR Warnings: {'; '.join(ocr_result.warnings)}]"
                    return result_text
                else:
                    final_error_message = ocr_result.error_message or "No text found after direct and OCR attempts for PDF."
                    if direct_result.error_message and direct_result.error_message not in final_error_message:
                         final_error_message = f"Direct: {direct_result.error_message}; OCR: {final_error_message}"
                    logger.warning(f"PDF processing failed after direct and OCR: {final_error_message}")
                    return f"Error: {final_error_message}" if not ocr_result.text.strip() else ocr_result.text

            elif doc_info.extension in self.SUPPORTED_IMAGE_EXTENSIONS:
                image_result = await self._extract_image_text(doc_info)
                if image_result.success and image_result.text.strip():
                    logger.info("Image OCR successful with meaningful text.")
                    return image_result.text
                elif image_result.success and not image_result.text.strip():
                    logger.info("Image OCR successful but no text found in image.")
                    return "Note: No text found in image by OCR."
                else:
                    logger.warning(f"Image OCR failed: {image_result.error_message}")
                    return f"Error: {image_result.error_message or 'Image OCR failed to extract text.'}"
            else:
                supported = ["pdf"] + sorted(list(self.SUPPORTED_IMAGE_EXTENSIONS))
                logger.warning(f"Unsupported file format: {file_extension}")
                return f"Error: Unsupported file format '{file_extension}'. Supported: {', '.join(supported)}"
        except Exception as e:
            logger.error(f"Unexpected error in extraction orchestration for {file_extension}: {e}", exc_info=True)
            return f"Error: Unexpected processing error - {str(e)}"

# --- Lifespan and Dependency Injection ---
_extractor_instance: Optional[DocumentTextExtractor] = None
_extractor_lock = asyncio.Lock()

@asynccontextmanager
async def lifespan(app):
    """FastAPI lifespan context manager to initialize/cleanup the extractor."""
    global _extractor_instance
    async with _extractor_lock:
        if _extractor_instance is None:
            _extractor_instance = DocumentTextExtractor()
            logger.info("DocumentTextExtractor singleton instance created via app lifespan.")
        else:
            logger.info("DocumentTextExtractor instance already existed when lifespan started.")
    
    yield
    
    logger.info("DocumentTextExtractor instance shutting down (if any cleanup actions were needed).")

async def get_text_extractor() -> DocumentTextExtractor:
    """Dependency injector to get the DocumentTextExtractor instance."""
    global _extractor_instance
    if _extractor_instance is None:
        async with _extractor_lock:
            if _extractor_instance is None:
                logger.warning(
                    "DocumentTextExtractor instance was None when requested. Creating new instance as fallback."
                )
                _extractor_instance = DocumentTextExtractor()
    return _extractor_instance

# --- Backward compatible function (if used by older modules directly) ---
async def extract_text_from_file(file_base64_data: str, file_extension: str) -> str:
    """
    Wrapper to use the DocumentTextExtractor instance.
    This is what was originally imported and used.
    """
    extractor = await get_text_extractor()
    return await extractor.extract_text_from_file_data(file_base64_data, file_extension)
