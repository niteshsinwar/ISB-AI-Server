# project_root/main.py
import uvicorn
import os
from dotenv import load_dotenv
import logging

# Load .env before other imports that might need environment variables
load_dotenv()

# Initialize basic logging configuration early if needed by modules imported by app.main
# However, the main logging setup will be in app.core.logging_config and called by app.main
# This is just a fallback or initial setup.
log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
# Ensure logging is configured if this script is run directly and imports cause logging calls
# before app.main fully configures it.
logging.basicConfig(
    level=getattr(logging, log_level_str, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    host = os.getenv("API_HOST", "0.0.0.0")
    port_str = os.getenv("API_PORT", "15841")
    try:
        port = int(port_str)
    except ValueError:
        logger.warning(f"Invalid API_PORT '{port_str}'. Defaulting to 15841.")
        port = 15841

    reload_flag = os.getenv("DEV_MODE", "false").lower() == "true"

    # Uvicorn's log level should be derived from the application's log level
    # Ensure it's a valid Uvicorn log level string
    uvicorn_log_levels = ["critical", "error", "warning", "info", "debug", "trace"]
    uvicorn_log_level = log_level_str.lower()
    if uvicorn_log_level not in uvicorn_log_levels:
        logger.warning(f"LOG_LEVEL '{log_level_str}' not directly mappable to Uvicorn. Defaulting Uvicorn log level to 'info'.")
        uvicorn_log_level = "info"

    logger.info(f"Starting Uvicorn server for 'app.main:app' on {host}:{port}")
    logger.info(f"Uvicorn log level: {uvicorn_log_level}, Reload: {reload_flag}")

    uvicorn.run(
        "app.main:app",  # Points to the FastAPI app instance in app/main.py
        host=host,
        port=port,
        log_level=uvicorn_log_level,
        reload=reload_flag
    )
