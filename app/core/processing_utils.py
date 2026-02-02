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
    Determine if processing should be skipped based on AVS status and modification dates.

    Skip conditions (OR logic):
    1. Confidence = 100% (already fully verified)
    2. OR both record and document were modified BEFORE AVS date (no changes since last analysis)

    Args:
        existing_avs: Dict with 'LastModifiedDate' and 'Percentage_Confidence__c' from AVS query
        record_last_modified: LastModifiedDate of the child record (from Apex response)
        document_last_modified: LastModifiedDate of the document/ContentVersion (from Apex response)

    Returns:
        Tuple of (should_skip: bool, reason: str)
    """
    # No existing AVS - must process
    if not existing_avs:
        return False, "no_existing_avs"

    avs_date_str = existing_avs.get('LastModifiedDate')
    confidence = existing_avs.get('Percentage_Confidence__c')

    # Condition 1: Skip if confidence is 100% (already fully verified)
    if confidence == '100' or confidence == 100:
        return True, f"confidence_100% (avs_date: {avs_date_str})"

    # Condition 2: Skip if no changes since last analysis (both dates < AVS date)
    avs_date = parse_sf_datetime(avs_date_str)
    if not avs_date:
        return False, "avs_date_missing"

    record_date = parse_sf_datetime(record_last_modified)
    doc_date = parse_sf_datetime(document_last_modified)

    # If we don't have both dates, we can't safely compare - must process
    # (Missing date info means we can't confirm nothing changed)
    if record_date is None and doc_date is None:
        return False, "missing_date_info_for_comparison"

    # Check if record was modified after AVS
    # If record_date is None but doc_date exists, only check doc
    # If record_date exists, it must be <= avs_date to be "unchanged"
    record_unchanged = (record_date is None) or (record_date <= avs_date)

    # Check if document was modified after AVS
    # If doc_date is None but record_date exists, only check record
    # If doc_date exists, it must be <= avs_date to be "unchanged"
    doc_unchanged = (doc_date is None) or (doc_date <= avs_date)

    if record_unchanged and doc_unchanged:
        return True, f"no_changes_since_last_analysis (confidence: {confidence}, avs_date: {avs_date_str})"

    # Changes detected - must reprocess
    if not record_unchanged:
        return False, f"record_modified_after_avs (record: {record_last_modified}, avs: {avs_date_str})"
    if not doc_unchanged:
        return False, f"document_modified_after_avs (doc: {document_last_modified}, avs: {avs_date_str})"

    return False, "unknown"
