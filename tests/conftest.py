"""
Shared pytest fixtures for the ISB-AI-Server test suite.
"""
import os
import sys
import pytest
import asyncio

# Ensure project root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set env vars before any app imports
os.environ.setdefault("DEV_SALESFORCE_CLIENT_ID", "test_client_id")
os.environ.setdefault("DEV_SALESFORCE_CLIENT_SECRET", "test_client_secret")
os.environ.setdefault("DEV_SALESFORCE_TOKEN_URL", "https://test.salesforce.com/services/oauth2/token")
os.environ.setdefault("UAT_SALESFORCE_CLIENT_ID", "test_uat_id")
os.environ.setdefault("UAT_SALESFORCE_CLIENT_SECRET", "test_uat_secret")
os.environ.setdefault("UAT_SALESFORCE_TOKEN_URL", "https://test-uat.salesforce.com/services/oauth2/token")
os.environ.setdefault("CREW_GOOGLE_API_KEY", "test_gemini_key_123456789")
os.environ.setdefault("DOC_GOOGLE_API_KEY", "test_doc_key_123456789")
os.environ.setdefault("LOG_LEVEL", "WARNING")


@pytest.fixture(scope="session")
def event_loop():
    """Create a single event loop for the entire test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
