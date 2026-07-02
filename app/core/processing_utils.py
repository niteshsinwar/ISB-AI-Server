# app/core/processing_utils.py
"""
Utility functions for processing optimization - skip logic for already-verified records.
"""
import logging
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Sentinel prefixes emitted by document_extraction_service when a document could
# not actually be read. These strings must never be fed to a comparator LLM as
# if they were document content — they represent extraction failures.
_EXTRACTION_FAILURE_MARKERS = (
    "## Document Processing Error",
    "No content could be extracted from this PDF.",
)


def is_valid_salesforce_id(value: Any) -> bool:
    """Strict Salesforce ID shape check: 15 or 18 alphanumeric characters.

    Length alone is not enough — quoted/injected strings of the right length
    must never reach SOQL interpolation or sObject payloads.
    """
    import re
    return isinstance(value, str) and bool(re.fullmatch(r"[a-zA-Z0-9]{15}(?:[a-zA-Z0-9]{3})?", value))


def detect_extraction_failure(document_text: Optional[str]) -> Optional[str]:
    """Return a human-readable failure reason when the extracted "text" is
    actually an extraction-failure sentinel, else None."""
    text = (document_text or "").strip()
    if not text:
        return "Uploaded document contains no readable text or is missing."
    for marker in _EXTRACTION_FAILURE_MARKERS:
        if text.startswith(marker):
            return (
                "Document could not be processed (corrupted, unsupported, or "
                "unreadable file). Extraction detail: " + text[:400]
            )
    return None


def parse_sf_datetime(dt_string: Optional[str]) -> Optional[datetime]:
    """Parse Salesforce datetime string to Python datetime."""
    if not dt_string:
        return None
    try:
        # Salesforce format: 2024-01-15T10:30:00.000+0000 or 2024-01-15T10:30:00.000Z
        dt_string = dt_string.replace('Z', '+0000')
        if '.' in dt_string:
            return datetime.strptime(dt_string[:23], '%Y-%m-%dT%H:%M:%S.%f')
        return datetime.strptime(dt_string[:19], '%Y-%m-%dT%H:%M:%S')
    except (ValueError, TypeError) as e:
        logger.warning(f"Could not parse datetime '{dt_string}': {e}")
        return None


def should_skip_processing(
    existing_avs: Optional[Dict[str, Any]],
    record_last_modified: Optional[str],
    document_last_modified: Optional[str],
) -> tuple[bool, str]:
    """
    Skip ONLY if the existing AVS is newer than BOTH the child record AND document.
    This means the current state of the data has already been verified by the AI.
    """
    # DEBUG: Log all input values
    logger.info(f"[SKIP_DEBUG] AVS={existing_avs}, record_date={record_last_modified}, doc_date={document_last_modified}")

    if not existing_avs:
        return False, "no_existing_avs"

    avs_date_str = existing_avs.get('LastModifiedDate')
    avs_date = parse_sf_datetime(avs_date_str)
    
    if not avs_date:
        return False, "avs_date_missing"

    record_date = parse_sf_datetime(record_last_modified)
    doc_date = parse_sf_datetime(document_last_modified)

    # If the record was updated AFTER the AVS was generated -> re-verify
    if record_date and record_date > avs_date:
        return False, f"record_modified_after_avs"

    # If the document was updated AFTER the AVS was generated -> re-verify
    if doc_date and doc_date > avs_date:
        return False, f"doc_modified_after_avs"

    # AVS is newer than both -> data hasn't changed since last verification, skip.
    return True, f"avs_newer_than_record_and_doc"
