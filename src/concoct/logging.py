"""Logging configuration with mandatory secret redaction."""

from __future__ import annotations

import logging
from pathlib import Path

from concoct.secrets import redact

LOGGER_NAME = "concoct"


class RedactingFilter(logging.Filter):
    """Rewrites every record so registered secrets never reach a handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = redact(message)
        record.args = None
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)


def configure_logging(level: str = "WARNING", log_file: Path | None = None) -> None:
    """Configure the ``concoct`` logger. Safe to call more than once."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level.upper())
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers: list[logging.Handler] = []

    from rich.logging import RichHandler

    console_handler = RichHandler(show_path=False, markup=False, rich_tracebacks=False)
    console_handler.setLevel(level.upper())
    handlers.append(console_handler)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
        logger.setLevel(logging.DEBUG)

    for handler in handlers:
        handler.addFilter(RedactingFilter())
        logger.addHandler(handler)
    logger.propagate = False
