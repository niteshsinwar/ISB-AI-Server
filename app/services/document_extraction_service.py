# document_extraction_service.py - Gemini OCR for all images and PDF pages
# Enhanced with smart context-aware extraction and page batching

import base64
import io
import os
import asyncio
import logging
from typing import Optional, List, Tuple, Dict, Any
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
import time
import json

import fitz  # PyMuPDF
from PIL import Image, ImageOps
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage

from app.config import (
    DOC_GOOGLE_API_KEY, MODEL_TEXT_EXTRACTION,
    RAW_OCR_PROMPT, DATA_STRUCTURING_PROMPT
)

# --- Smart Extraction Prompts by Record Type ---
# These prompts focus extraction on only the relevant fields for each record type
# Output format: LLM-friendly plain text (not JSON) for efficient LLM-to-LLM communication
SMART_EXTRACTION_PROMPTS = {
    "education": """
You are an intelligent document analyzer extracting data from an EDUCATION document (degree certificate, transcript, marksheet).

FIELDS TO FIND:
{fields_to_find}

EXPECTED VALUES (use to locate correct record if multiple students/degrees exist):
{field_hints}

EXTRACTION GUIDELINES:
- Expected values are location hints only. Never copy them into the output, use them to fill missing data, or use them as a basis for inference.
- STUDENT NAME: Extract full name as printed on document
- INSTITUTION: Identify if it's a COLLEGE or UNIVERSITY (critical distinction). Note full institutional hierarchy.
- DEGREE: Extract the exact degree name and level.
- FIELD OF STUDY: Extract the academic unit, faculty, broad discipline, or field exactly as printed. Do not copy the expected applicant value. Do not include text labeled branch, major, minor, discipline/specialization, or specialization in this field.
- SPECIALIZATION: Separately extract the exact branch, major, discipline, or specialization printed on the document.
- Keep Field of Study and Specialization separate. Do not infer or merge them during extraction; inference is performed by the comparison stage.
- DATES: Extract only dates or partial dates explicitly printed with their labels. Do not infer missing dates or manufacture month/day values.
- GPA/CGPA/PERCENTAGE: Extract an explicitly printed final/overall value only. Do not calculate an aggregate during extraction.
- If no final/overall GPA or percentage is printed, return the requested score field as NOT_FOUND and add `ACADEMIC_SCORE_EVIDENCE` listing every printed semester/year total, maximum, GPA, CGPA, CPI, or percentage exactly as shown.

IGNORE: Logos, letterheads, terms & conditions, signatures, stamps, irrelevant pages.

OUTPUT FORMAT (plain text, one field per line):
For each field, write: FIELD_NAME: extracted_value
  → Context: quote the 1-2 lines where this value appears

Example:
Institution_Name__c: Indian School of Business
  → Context: "This degree is awarded by Indian School of Business, Hyderabad campus"

hed__Start_Date__c: June 2019
  → Context: "Program Duration: June 2019 to May 2020"

If a field is not found, write: FIELD_NAME: NOT_FOUND
""",

    "employment": """
You are an intelligent document analyzer extracting data from an EMPLOYMENT document (offer letter, payslip, experience letter, salary slip).

FIELDS TO FIND:
{fields_to_find}

EXPECTED VALUES (use to locate correct employee if multiple mentioned):
{field_hints}

EXTRACTION GUIDELINES:
- EMPLOYEE NAME: Must match the applicant
- COMPANY NAME: Note parent company or subsidiaries if mentioned
- JOB TITLE/DESIGNATION: Extract exact title
- DATES: Start date, End date (or "Present" for current)
- SALARY: Extract amount with currency (INR/USD/EUR) and frequency (monthly/annual/CTC). If monthly, also note annual equivalent.

IGNORE: Company policies, benefits details, terms & conditions, leave policies, HR guidelines.

OUTPUT FORMAT (plain text, one field per line):
For each field, write: FIELD_NAME: extracted_value
  → Context: quote the 1-2 lines where this value appears

Example:
Company_Name__c: Tata Consultancy Services
  → Context: "This is to certify that Mr. John worked at Tata Consultancy Services Limited"

Annual_Salary__c: 1200000 INR
  → Context: "Your CTC will be Rs. 12,00,000 per annum effective from joining date"

If a field is not found, write: FIELD_NAME: NOT_FOUND
""",

    "test_score": """
You are an intelligent document analyzer extracting data from a TEST SCORE document (GMAT, GRE score report).

FIELDS TO FIND:
{fields_to_find}

EXPECTED VALUES (use to locate correct test record):
{field_hints}

EXTRACTION GUIDELINES:
- Extract ONLY fields explicitly listed under FIELDS TO FIND. Do not introduce additional score fields.
- CANDIDATE NAME: Full name as printed
- TEST DATE: When the test was taken
- SCORES: Extract the listed score and percentile fields exactly as printed. Note the scale when present.
- GRE: Never calculate, infer, extract, or output a combined total score or total percentile. GRE uses Verbal, Quantitative, and Analytical Writing results independently.
- GMAT/GMAT Focus: Extract a total score or total percentile only when that field is listed and explicitly printed.
- Also extract AWA, IR if present
- REGISTRATION/TEST ID: Any identification numbers
- DO NOT CALCULATE scores - extract only explicitly printed values

IGNORE: Instructions, disclaimers, general information about the test.

OUTPUT FORMAT (plain text, one field per line):
For each field, write: FIELD_NAME: extracted_value
  → Context: quote the 1-2 lines where this value appears

Example:
Verbal_Score__c: 38
  → Context: "Verbal Reasoning: 38 (85th percentile)"

If a field is not found, write: FIELD_NAME: NOT_FOUND
""",

    "application": """
You are an intelligent document analyzer extracting data from a PERSONAL IDENTITY document (passport, Aadhar, ID card).

FIELDS TO FIND:
{fields_to_find}

EXPECTED VALUES (use to locate correct person):
{field_hints}

EXTRACTION GUIDELINES:
- FULL NAME: As printed on document
- ID NUMBER: Passport number, Aadhar number (may be masked XXXX-XXXX-1234)
- BIRTHDATE: Date of birth in any format
- GENDER: M/F/Male/Female
- NATIONALITY: Country of citizenship
- EXPIRY DATE: For passports/IDs
- DOCUMENT TYPE: Passport, Aadhar, Driving License, etc.

OUTPUT FORMAT (plain text, one field per line):
For each field, write: FIELD_NAME: extracted_value
  → Context: quote the line or label where this value appears

Example:
FirstName: Rahul
  → Context: "Given Name: RAHUL"

Date_of_Birth__c: 15/03/1990
  → Context: "Date of Birth: 15/03/1990"

If a field is not found, write: FIELD_NAME: NOT_FOUND
""",

    "resume": """
You are an intelligent document analyzer scanning a RESUME for CONTACT INFORMATION and ACADEMIC SCORES.

SCAN FOR:
- Phone numbers (any format)
- Email addresses (contains @)
- LinkedIn URLs or handles
- CGPA or Percentage scores

OUTPUT FORMAT (plain text):
For each item found, write the value and quote where it appears:

PHONE: +91-9876543210
  → Context: "Contact: +91-9876543210 | email@domain.com"

EMAIL: john.doe@gmail.com
  → Context: "Contact: +91-9876543210 | john.doe@gmail.com"

LINKEDIN: linkedin.com/in/johndoe
  → Context: "LinkedIn: linkedin.com/in/johndoe"

CGPA: 8.5
  → Context: "CGPA: 8.5/10 from XYZ University"

If not found, write: ITEM: NOT_FOUND
"""
}

# Fields to exclude from extraction (integration/system fields)
INTEGRATION_FIELDS = {
    'Id', 'recordId', 'Task_Id', 'triggeringLogId', 'DocumentchecklistItem_Id',
    'Applicant__c', 'Contact', 'Application__c', 'type', 'attributes',
    'CreatedDate', 'CreatedById', 'LastModifiedDate', 'LastModifiedById', 'SystemModstamp',
    'IsDeleted', 'OwnerId'
}

# Set reasonable limit for PIL
Image.MAX_IMAGE_PIXELS = 178956970

logger = logging.getLogger(__name__)

# --- Custom Exception ---
class DocumentExtractionError(Exception):
    """Custom exception for errors during the document extraction process."""
    pass

# --- Optimized Gemini Processor ---
class OptimizedGeminiProcessor:
    """Single-pass Gemini processor for all OCR tasks."""

    def __init__(self):
        self.llm = ChatGoogleGenerativeAI(
            model=MODEL_TEXT_EXTRACTION,
            google_api_key=DOC_GOOGLE_API_KEY,
            temperature=0.1,
            request_timeout=30.0,
            max_retries=2,
            transport="rest"  # Use REST transport for network interceptor to capture usage
        )
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix=f"ocr-{id(self)}")
        self._shutdown = False

    def shutdown(self):
        """Explicit shutdown method"""
        if not self._shutdown:
            try:
                if hasattr(self, 'executor'):
                    self.executor.shutdown(wait=True)
                if hasattr(self, 'llm') and hasattr(self.llm, '_client'):
                    # Clean up any persistent connections
                    if hasattr(self.llm._client, 'close'):
                        self.llm._client.close()
                self._shutdown = True
            except Exception as e:
                logger.warning(f"Error during OCR processor shutdown: {e}")

    def __del__(self):
        """Fallback cleanup"""
        if not self._shutdown:
            self.shutdown()
    
    def process_document_single_pass(self, image_bytes: bytes, context: dict) -> str:
        """Single-pass OCR with built-in structuring - used for ALL content (legacy mode)"""
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

    def process_document_smart_extraction(
        self,
        image_bytes_list: List[bytes],
        record_type: str,
        fields_to_find: List[str],
        field_hints: Dict[str, Any],
        context: dict
    ) -> str:
        """
        Smart extraction: Process multiple page images at once with context-aware prompts.
        Extracts only the fields relevant to the record type.

        Args:
            image_bytes_list: List of image bytes (1-2 pages at a time)
            record_type: Type of record (education, employment, test_score, application, resume)
            fields_to_find: List of field names to extract
            field_hints: Dict of expected field values to help locate correct data
            context: Logging context
        """
        try:
            # Get the smart extraction prompt for this record type
            base_prompt = SMART_EXTRACTION_PROMPTS.get(record_type, SMART_EXTRACTION_PROMPTS.get("application"))

            # Format the prompt with fields and hints
            extraction_prompt = base_prompt.format(
                fields_to_find="\n".join(f"- {field}" for field in fields_to_find),
                field_hints="\n".join(f"- {k}: {v}" for k, v in field_hints.items() if v)
            )

            # Build message content with all images
            content = [{"type": "text", "text": extraction_prompt}]

            for idx, image_bytes in enumerate(image_bytes_list):
                encoded_image = base64.b64encode(image_bytes).decode('utf-8')
                image_url_content = f"data:image/jpeg;base64,{encoded_image}"
                content.append({
                    "type": "image_url",
                    "image_url": {"url": image_url_content}
                })

            messages = [HumanMessage(content=content)]

            start_time = time.time()
            response = self.llm.invoke(messages)
            processing_time = time.time() - start_time

            num_pages = len(image_bytes_list)
            logger.info(f"Context: {context} - Smart extraction ({record_type}, {num_pages} pages) completed in {processing_time:.2f}s")
            return response.content

        except Exception as e:
            logger.error(f"Context: {context} - Error in smart extraction: {e}", exc_info=True)
            raise DocumentExtractionError(f"Smart extraction failed: {e}")

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
        self._shutdown = False

    def shutdown(self):
        """Explicit cleanup of all resources"""
        if not self._shutdown:
            try:
                if hasattr(self, 'ocr_processor'):
                    self.ocr_processor.shutdown()
                # Clear any cached data
                if hasattr(self, '_cached_data'):
                    delattr(self, '_cached_data')
                self._shutdown = True
            except Exception as e:
                logger.warning(f"Error during document extractor shutdown: {e}")

    def __del__(self):
        """Fallback cleanup"""
        if not self._shutdown:
            self.shutdown()

    def _decode_base64(self, b64_data: str) -> bytes:
        """Decode base64 data"""
        try:
            if ',' in b64_data:
                b64_data = b64_data.split(',', 1)[1]
            return base64.b64decode(b64_data, validate=True)
        except Exception as e:
            raise DocumentExtractionError(f"Invalid Base64 input: {e}")

    async def _process_pdf_with_gemini(
        self,
        pdf_bytes: bytes,
        log_context: dict,
        record_type: str = None,
        fields_to_find: List[str] = None,
        field_hints: Dict[str, Any] = None
    ) -> str:
        """
        Process PDF by converting pages to images and using Gemini OCR.

        Page Batching Strategy (to reduce API calls):
        - 1 page: send 1 page
        - 2 pages: send 2 pages together
        - 3 pages: send 2 pages, then 1 page
        - 4 pages: send 2 pages, then 2 pages
        - N pages: send in batches of 2

        If record_type is provided, uses smart extraction mode.
        Otherwise, falls back to legacy full-text extraction.
        """
        all_pages_text = []
        doc = None
        use_smart_extraction = record_type is not None and fields_to_find is not None

        try:
            # Open PDF with PyMuPDF
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")

            # Check if password protected
            if doc.is_encrypted and doc.needs_pass:
                return "## Document Processing Error\n\nThis PDF is password protected and cannot be processed."

            num_pages = len(doc)
            logger.info(f"Context: {log_context} - PDF with {num_pages} pages. Smart extraction: {use_smart_extraction}")

            if use_smart_extraction:
                # Smart extraction with page batching (2 pages at a time)
                result = await self._process_pdf_smart_batched(
                    doc, num_pages, log_context, record_type, fields_to_find, field_hints or {}
                )
                return result
            else:
                # Legacy mode: process each page separately
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

    async def _process_pdf_smart_batched(
        self,
        doc: fitz.Document,
        num_pages: int,
        log_context: dict,
        record_type: str,
        fields_to_find: List[str],
        field_hints: Dict[str, Any]
    ) -> str:
        """
        Process PDF pages in batches of 2 using smart extraction.

        Batching: [0,1], [2,3], [4,5], ... or [0,1], [2] for odd pages
        """
        BATCH_SIZE = 2
        all_extracted_data = []

        # Create batches: [[0,1], [2,3], ...] or [[0,1], [2]] for 3 pages
        batches = []
        for i in range(0, num_pages, BATCH_SIZE):
            batch = list(range(i, min(i + BATCH_SIZE, num_pages)))
            batches.append(batch)

        logger.info(f"Context: {log_context} - Processing {num_pages} pages in {len(batches)} batches")

        for batch_idx, page_indices in enumerate(batches):
            batch_context = {**log_context, "batch": f"{batch_idx + 1}/{len(batches)}", "pages": page_indices}

            try:
                # Convert all pages in this batch to images
                batch_images = []
                for page_num in page_indices:
                    page_context = {**batch_context, "page": page_num + 1}

                    image_bytes = await asyncio.to_thread(
                        self.pdf_handler.convert_pdf_page_to_image, doc, page_num, page_context
                    )
                    enhanced_bytes = await asyncio.to_thread(
                        self.image_processor.quick_enhance, image_bytes, page_context
                    )
                    batch_images.append(enhanced_bytes)

                # Send batch to smart extraction
                batch_result = await asyncio.to_thread(
                    self.ocr_processor.process_document_smart_extraction,
                    batch_images,
                    record_type,
                    fields_to_find,
                    field_hints,
                    batch_context
                )

                all_extracted_data.append(f"## Pages {page_indices[0]+1}-{page_indices[-1]+1}\n\n{batch_result}")

            except Exception as batch_error:
                logger.error(f"Context: {batch_context} - Batch processing error: {batch_error}")
                all_extracted_data.append(
                    f"## Pages {page_indices[0]+1}-{page_indices[-1]+1}\n\n"
                    f"*Error: Batch could not be processed. Reason: {str(batch_error)[:200]}*"
                )

        # Combine all batch results
        if len(all_extracted_data) == 1:
            return all_extracted_data[0]
        else:
            return "\n\n---\n\n".join(all_extracted_data)

    async def extract_text_from_document(
        self,
        file_base64_data: str,
        file_extension: str,
        context_log_id: str,
        record_type: str = None,
        fields_to_find: List[str] = None,
        field_hints: Dict[str, Any] = None
    ) -> str:
        """
        Extract text from document using Gemini OCR.

        Args:
            file_base64_data: Base64 encoded file content
            file_extension: File extension (pdf, png, jpg, etc.)
            context_log_id: ID for logging context
            record_type: Type of record (education, employment, etc.) for smart extraction
            fields_to_find: List of field NAMES to extract (not values)
            field_hints: Dict of field values as hints to locate correct data

        If record_type and fields_to_find are provided, uses smart extraction.
        Otherwise, falls back to legacy full-text extraction.
        """
        start_time = time.time()
        log_context = {"file_ext": file_extension, "id": context_log_id}
        use_smart = record_type is not None and fields_to_find is not None

        try:
            file_bytes = self._decode_base64(file_base64_data)

            if file_extension.lower() == 'pdf':
                # Process PDF with page batching
                result = await self._process_pdf_with_gemini(
                    file_bytes, log_context, record_type, fields_to_find, field_hints
                )

            elif file_extension.lower() in {"png", "jpg", "jpeg", "webp", "bmp", "tiff"}:
                # Process image directly
                logger.info(f"Context: {log_context} - Processing image file. Smart: {use_smart}")

                # Enhance image
                enhanced_bytes = await asyncio.to_thread(
                    self.image_processor.quick_enhance, file_bytes, log_context
                )

                if use_smart:
                    # Smart extraction for single image
                    result = await asyncio.to_thread(
                        self.ocr_processor.process_document_smart_extraction,
                        [enhanced_bytes],  # Single image as list
                        record_type,
                        fields_to_find,
                        field_hints or {},
                        log_context
                    )
                else:
                    # Legacy OCR
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

# --- Per-Job Instance Creation ---
def create_text_extractor() -> FastDocumentExtractor:
    """Create a new document extractor instance per job."""
    try:
        extractor = FastDocumentExtractor()
        logger.info("FastDocumentExtractor created for job")
        return extractor
    except Exception as e:
        logger.error(f"Failed to create FastDocumentExtractor: {e}", exc_info=True)
        raise RuntimeError(f"FastDocumentExtractor creation failed: {e}")

@asynccontextmanager
async def lifespan(app):
    """Application lifespan manager - no global instance"""
    try:
        logger.info("Document extraction service ready for per-job instances")
        yield
    except Exception as e:
        logger.critical(f"Document extraction service startup error: {e}", exc_info=True)
        raise

# --- Field Classification Helper ---
def classify_record_fields(record_data: Dict[str, Any]) -> Tuple[List[str], Dict[str, Any]]:
    """
    Separate record data into verification fields and hints.

    Args:
        record_data: Full record data from Salesforce

    Returns:
        Tuple of:
        - fields_to_find: List of field NAMES for extraction (excluding integration fields)
        - field_hints: Dict of field name -> value (as hints to locate correct data)
    """
    fields_to_find = []
    field_hints = {}

    for field_name, field_value in record_data.items():
        # Skip integration/system fields
        if field_name in INTEGRATION_FIELDS:
            continue

        # Skip fields with None/empty values (no hint to provide)
        if field_value is None or field_value == "":
            fields_to_find.append(field_name)
            continue

        # Skip nested objects/dicts (relationships)
        if isinstance(field_value, dict):
            continue

        # Add to verification fields
        fields_to_find.append(field_name)

        # Add value as hint (only non-empty values)
        if field_value:
            field_hints[field_name] = field_value

    return fields_to_find, field_hints


def get_record_type_from_object(object_api_name: str) -> str:
    """
    Map Salesforce object API name to record type for smart extraction.
    """
    mapping = {
        "ISB_Education_Log__c": "education",
        "ISB_Employment_Log__c": "employment",
        "hed__Test__c": "test_score",
        "hed__Application__c": "application",
        "DocumentChecklistItem": "resume",
    }
    return mapping.get(object_api_name, "application")


# Update the public API function
async def extract_text_from_file(
    file_base64_data: str,
    file_extension: str,
    record_id: str,
    extractor: FastDocumentExtractor = None,
    record_type: str = None,
    record_data: Dict[str, Any] = None,
) -> str:
    """
    Extract text from file using Gemini OCR.

    Args:
        file_base64_data: Base64 encoded file content
        file_extension: File extension (pdf, png, jpg, etc.)
        record_id: ID for logging context
        extractor: Optional pre-created extractor instance
        record_type: Type of record (education, employment, etc.) for smart extraction
        record_data: Full record data - will be classified into fields_to_find and hints

    If record_type and record_data are provided, uses smart context-aware extraction.
    Otherwise, falls back to legacy full-text extraction.
    """
    if extractor is None:
        extractor = create_text_extractor()

    # If record_data provided, classify fields for smart extraction
    fields_to_find = None
    field_hints = None

    if record_type and record_data:
        fields_to_find, field_hints = classify_record_fields(record_data)
        logger.info(f"Smart extraction for {record_id}: type={record_type}, fields={len(fields_to_find)}")

    return await extractor.extract_text_from_document(
        file_base64_data,
        file_extension,
        context_log_id=record_id,
        record_type=record_type,
        fields_to_find=fields_to_find,
        field_hints=field_hints
    )
