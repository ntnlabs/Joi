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

def _make_json_formatter():
    """Build CustomJsonFormatter lazily so pythonjsonlogger is only imported when needed."""
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

    return CustomJsonFormatter


class CustomTextFormatter(logging.Formatter):
    """Text formatter that includes extra fields inline."""

    # Fields that are part of standard LogRecord, not user-provided extras
    _BUILTIN_ATTRS = {
        'name', 'msg', 'args', 'created', 'filename', 'funcName', 'levelname',
        'levelno', 'lineno', 'module', 'msecs', 'pathname', 'process',
        'processName', 'relativeCreated', 'stack_info', 'exc_info', 'exc_text',
        'thread', 'threadName', 'taskName', 'message',
    }

    def format(self, record):
        # Get the base formatted message
        base = super().format(record)

        # Extract extra fields (anything not in standard LogRecord)
        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in self._BUILTIN_ATTRS and not k.startswith('_')
        }

        if extras:
            # Format extras as key=value pairs
            extra_str = ' '.join(f"{k}={v}" for k, v in extras.items())
            return f"{base} [{extra_str}]"
        return base


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
        formatter = _make_json_formatter()(
            '%(timestamp)s %(level)s %(logger)s %(message)s'
        )
    else:
        formatter = CustomTextFormatter(
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
