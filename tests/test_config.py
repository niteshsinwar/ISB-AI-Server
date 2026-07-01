"""Unit tests for app/config.py"""
import os
import pytest


class TestConfig:
    def test_salesforce_orgs_structure(self):
        from app.config import SALESFORCE_ORGS
        assert isinstance(SALESFORCE_ORGS, dict)
        assert "dev" in SALESFORCE_ORGS
        assert "uat" in SALESFORCE_ORGS
        for alias, org in SALESFORCE_ORGS.items():
            assert "client_id" in org
            assert "client_secret" in org
            assert "token_url" in org

    def test_apex_endpoint_paths(self):
        from app.config import APEX_ENDPOINT_PATHS
        assert isinstance(APEX_ENDPOINT_PATHS, dict)
        assert len(APEX_ENDPOINT_PATHS) >= 3
        # Should be keyed by SObject API name
        for key in APEX_ENDPOINT_PATHS:
            assert "__c" in key or "__" in key  # custom or managed objects

    def test_llm_field_exclusions(self):
        from app.config import LLM_FIELD_EXCLUSIONS
        assert isinstance(LLM_FIELD_EXCLUSIONS, list)
        assert "Id" in LLM_FIELD_EXCLUSIONS
        assert "LastModifiedDate" in LLM_FIELD_EXCLUSIONS
        assert "CreatedDate" in LLM_FIELD_EXCLUSIONS
        assert "IsDeleted" in LLM_FIELD_EXCLUSIONS
        # Internal routing fields should be excluded
        assert "type" in LLM_FIELD_EXCLUSIONS
        assert "recordId" in LLM_FIELD_EXCLUSIONS

    def test_related_record_processing_config(self):
        from app.config import RELATED_RECORD_PROCESSING_CONFIG
        assert isinstance(RELATED_RECORD_PROCESSING_CONFIG, list)
        assert len(RELATED_RECORD_PROCESSING_CONFIG) >= 4
        for entry in RELATED_RECORD_PROCESSING_CONFIG:
            assert "target_record_type" in entry
            assert "processor_module" in entry
            assert "processor_function_name" in entry

    def test_eedl_record_processing_config(self):
        from app.config import EEDL_RECORD_PROCESSING_CONFIG
        assert isinstance(EEDL_RECORD_PROCESSING_CONFIG, list)
        assert len(EEDL_RECORD_PROCESSING_CONFIG) >= 2
        target_types = [c["target_record_type"] for c in EEDL_RECORD_PROCESSING_CONFIG]
        assert "ID_Document" in target_types
        assert "Education__c" in target_types

    def test_model_names_are_strings(self):
        from app.config import MODEL_STANDARD_VERIFICATION, MODEL_HTML_SYNTHESIS, MODEL_TEXT_EXTRACTION
        assert isinstance(MODEL_STANDARD_VERIFICATION, str)
        assert isinstance(MODEL_HTML_SYNTHESIS, str)
        assert isinstance(MODEL_TEXT_EXTRACTION, str)

    def test_numeric_config_values(self):
        from app.config import (
            MAX_CONCURRENT_PROCESSING_SLOTS,
            MAX_SALESFORCE_REPORT_LENGTH,
            MIN_REQUEST_INTERVAL_SECONDS,
            JOB_TIMEOUT_SECONDS,
        )
        assert isinstance(MAX_CONCURRENT_PROCESSING_SLOTS, int) and MAX_CONCURRENT_PROCESSING_SLOTS > 0
        assert isinstance(MAX_SALESFORCE_REPORT_LENGTH, int) and MAX_SALESFORCE_REPORT_LENGTH > 0
        assert isinstance(MIN_REQUEST_INTERVAL_SECONDS, (int, float)) and MIN_REQUEST_INTERVAL_SECONDS >= 0
        assert isinstance(JOB_TIMEOUT_SECONDS, int) and JOB_TIMEOUT_SECONDS > 0

    def test_dead_vars_removed(self):
        """Ensure previously dead variables are no longer defined."""
        import app.config as cfg
        assert not hasattr(cfg, "LOG_LEVEL")
        assert not hasattr(cfg, "MULTIMODAL_TOKEN_CONFIG")
        assert not hasattr(cfg, "CONFIDENCE_PICKLIST_RANGES")
        assert not hasattr(cfg, "MAX_CONCURRENT_OCR_PAGES")

    def test_test_score_not_in_apex_paths(self):
        """Test score endpoint was migrated to Python."""
        from app.config import APEX_ENDPOINT_PATHS
        for key in APEX_ENDPOINT_PATHS:
            assert "test" not in key.lower()

    def test_dci_parent_lookup_field_used_in_config(self):
        """DCI_PARENT_LOOKUP_FIELD feeds into RELATED_RECORD_PROCESSING_CONFIG."""
        from app.config import DCI_PARENT_LOOKUP_FIELD, RELATED_RECORD_PROCESSING_CONFIG
        # Find the DCI entry
        dci_entries = [e for e in RELATED_RECORD_PROCESSING_CONFIG if e["target_record_type"] == "DocumentChecklistItem"]
        assert len(dci_entries) == 1
        assert dci_entries[0]["lookup_on_child_to_parent"] == DCI_PARENT_LOOKUP_FIELD
