# project_root/app/core/app_instance.py
import logging
from typing import Optional, Dict, Any
from fastapi import FastAPI

logger = logging.getLogger(__name__)

# Global app instance storage
_app_instance_storage: Dict[str, Any] = {"app": None}

def set_app_instance(app: FastAPI):
    _app_instance_storage["app"] = app
    logger.info(f"FastAPI app instance set. Version: {app.version if hasattr(app, 'version') else 'N/A'}")

def get_app_instance() -> FastAPI:
    app_inst = _app_instance_storage["app"]
    if app_inst is None:
        logger.warning("FastAPI app instance was requested via get_app_instance() but not set. Returning a placeholder.")
        class DummyApp: # Simple placeholder
            version = "N/A (App instance not fully set/available)"
        return DummyApp()
    return app_inst