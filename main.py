# project_root/main.py
import uvicorn
import os
from dotenv import load_dotenv
import logging

load_dotenv() # Load environment variables from .env

# Basic logging configuration
log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level_str, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    host = os.getenv("API_HOST", "0.0.0.0")
    port_str = os.getenv("API_PORT", "443") # Ensure .env has API_PORT=443
    port = int(port_str)

    reload_flag = os.getenv("DEV_MODE", "false").lower() == "true"

    uvicorn_log_levels = ["critical", "error", "warning", "info", "debug", "trace"]
    uvicorn_log_level = log_level_str.lower()
    if uvicorn_log_level not in uvicorn_log_levels:
        logger.warning(f"LOG_LEVEL '{log_level_str}' not recognized by Uvicorn. Defaulting to 'info'.")
        uvicorn_log_level = "info"

    # --- SSL Configuration for Uvicorn ---
    ssl_keyfile_path = "/app/certs/isbcert.key"
    ssl_certfile_path = "/app/certs/isbcert.crt"

    ssl_params = {} # Changed variable name to avoid conflict if 'ssl_config' is used elsewhere
    if os.path.exists(ssl_keyfile_path) and os.path.exists(ssl_certfile_path):
        logger.info(f"SSL key and cert files found. Uvicorn will attempt to start with HTTPS.")
        ssl_params = {
            "ssl_keyfile": ssl_keyfile_path,
            "ssl_certfile": ssl_certfile_path
        }
    else:
        logger.error(f"SSL keyfile ('{ssl_keyfile_path}') or certfile ('{ssl_certfile_path}') not found. Cannot start HTTPS server.")
        exit(1) # Exit if certs are mandatory for HTTPS
    # --- End SSL Configuration ---

    if ssl_params: # Check if SSL parameters are set
        logger.info(f"Starting Uvicorn HTTPS server for 'app.main:app' on {host}:{port}")
        logger.info(f"Uvicorn log level: {uvicorn_log_level}, Reload: {reload_flag}")

        uvicorn.run(
            "app.main:app",  # Assuming your FastAPI app instance is 'app' in 'app/main.py'
            host=host,
            port=port,
            log_level=uvicorn_log_level,
            reload=reload_flag,
            **ssl_params # Pass the SSL parameters
        )
    else:
        logger.error("Uvicorn server not started due to missing SSL configuration.")