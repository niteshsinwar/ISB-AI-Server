# app/core/processing_utils.py
"""
Utility functions for processing optimization - skip logic for already-verified records.
"""
import logging
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


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
    Skip if:
    1. Confidence = 100, OR
    2. AVS is newer than BOTH child record AND document
    """
    # DEBUG: Log all input values
    logger.info(f"[SKIP_DEBUG] AVS={existing_avs}, record_date={record_last_modified}, doc_date={document_last_modified}")

    if not existing_avs:
        return False, "no_existing_avs"

    confidence = existing_avs.get('Percentage_Confidence__c')
    avs_date_str = existing_avs.get('LastModifiedDate')

    # Condition 1: confidence = 100
    if confidence == '100' or confidence == 100:
        return True, f"confidence_100%"

    # Condition 2: AVS newer than both record and doc
    avs_date = parse_sf_datetime(avs_date_str)
    if not avs_date:
        return False, "avs_date_missing"

    record_date = parse_sf_datetime(record_last_modified)
    doc_date = parse_sf_datetime(document_last_modified)

    # AVS must be newer than record (if record date exists)
    if record_date and record_date > avs_date:
        return False, f"record_modified_after_avs"

    # AVS must be newer than doc (if doc date exists)
    if doc_date and doc_date > avs_date:
        return False, f"doc_modified_after_avs"

    # AVS is newer than both - skip
    return True, f"avs_newer_than_record_and_doc"
