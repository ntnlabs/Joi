"""
Centralized logging configuration for Mesh Proxy.

Provides JSON-formatted logs for production (machine-parseable) with
fallback to text format for development.

Environment variables:
    MESH_LOG_JSON: "1" for JSON format, "0" for text (default)
    MESH_LOG_LEVEL: Log level (default: INFO)
"""

import logging
import os
import sys
from datetime import datetime, timezone

from pythonjsonlogger import jsonlogger


class CustomJsonFormatter(jsonlogger.JsonFormatter):
    """Custom JSON formatter with standard fields."""

    def add_fields(self, log_record, record, message_dict):
        super().add_fields(log_record, record, message_dict)
        log_record['level'] = record.levelname
        log_record['logger'] = record.name
        log_record['timestamp'] = datetime.now(timezone.utc).isoformat()

        # Add source location for errors
        if record.levelno >= logging.WARNING:
            log_record['source'] = f"{record.filename}:{record.lineno}"


def configure_logging(level: str = None, use_json: bool = None):
    """
    Configure logging for Mesh Proxy.

    Args:
        level: Log level (default: from MESH_LOG_LEVEL env or INFO)
        use_json: Use JSON format (default: from MESH_LOG_JSON env or False)
    """
    if level is None:
        level = os.getenv("MESH_LOG_LEVEL", "INFO")
    if use_json is None:
        use_json = os.getenv("MESH_LOG_JSON", "0") == "1"

    handler = logging.StreamHandler(sys.stdout)

    if use_json:
        formatter = CustomJsonFormatter(
            '%(timestamp)s %(level)s %(logger)s %(message)s'
        )
    else:
        formatter = logging.Formatter(
            '%(asctime)s %(levelname)s %(name)s: %(message)s'
        )

    handler.setFormatter(formatter)

    # Configure root logger
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Quiet noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
