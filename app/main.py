# project_root/app/main.py
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from dotenv import load_dotenv

load_dotenv()

from app.core.logging_config import setup_logging
from app.endpoints.main_router import main_api_router
from app.services.document_extraction_service import lifespan as text_extractor_lifespan
from app.config import APP_TITLE, APP_DESCRIPTION, APP_VERSION
from app.core.app_instance import set_app_instance
# --- MODIFIED IMPORT ---
from app.core.rate_limit_state import initialize_processing_semaphore

import logging
logger = logging.getLogger(__name__)
setup_logging()


@asynccontextmanager
async def lifespan(app_lifespan: FastAPI):
    logger.info("Application lifespan startup...")
    set_app_instance(app_lifespan)
    
    # --- ADDED: Initialize the semaphore on startup ---
    initialize_processing_semaphore()

    async with text_extractor_lifespan(app_lifespan):
        logger.info("Text extractor initialized via application lifespan.")
        yield
    
    logger.info("Application lifespan shutdown.")

app = FastAPI(
    title=APP_TITLE,
    description=APP_DESCRIPTION,
    version=APP_VERSION,
    lifespan=lifespan
)

app.include_router(main_api_router)

logger.info(f"{APP_TITLE} - Version {APP_VERSION} initialized.")
logger.info(f"Log level set to: {os.getenv('LOG_LEVEL', 'INFO').upper()}")
logger.info(f"Salesforce Auth Mode: {os.getenv('SALESFORCE_AUTH_MODE', 'password')}")
