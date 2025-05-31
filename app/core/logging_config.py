# project_root/app/core/logging_config.py
import logging
import sys
import os

def setup_logging():
    """
    Configures logging for the application.
    It ensures that logging is set up only once.
    """
    log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)

    # Get the root logger
    root_logger = logging.getLogger()

    # Check if handlers are already configured to prevent duplicate logs
    if not root_logger.hasHandlers() or not root_logger.handlers:
        # If no handlers, configure them
        formatter = logging.Formatter(
            fmt="%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        
        # Console Handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        
        root_logger.addHandler(console_handler)
        root_logger.setLevel(log_level)
        
        # Set levels for other loggers if needed (e.g., uvicorn, sqlalchemy)
        logging.getLogger("uvicorn.error").setLevel(log_level)
        logging.getLogger("uvicorn.access").setLevel(log_level)
        # Add more specific logger configurations if necessary

        logging.info(f"Root logging configured with level: {log_level_str}")
    else:
        # If handlers exist, just ensure the root logger's level is set
        # This can happen if another module (like uvicorn itself) sets up basicConfig
        current_level = logging.getLevelName(root_logger.getEffectiveLevel())
        if current_level != log_level_str:
             root_logger.setLevel(log_level)
             logging.info(f"Root logger level updated to: {log_level_str} (was {current_level})")
        else:
             logging.debug(f"Logging already configured. Current level: {current_level}")


# Call setup_logging when this module is imported,
# or ensure it's called early in your application's lifecycle (e.g., in app/main.py).
# For this structure, app/main.py will call it.
