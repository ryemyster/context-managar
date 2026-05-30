import json
import logging
import sys
import time
from . import config


class _JsonFormatter(logging.Formatter):
    converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        entry: dict = {
            "ts":     self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level":  record.levelname,
            "msg":    record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def _setup() -> logging.Logger:
    logger = logging.getLogger("context-engine")
    if logger.handlers:
        return logger

    if config.LOG_FORMAT == "json":
        formatter: logging.Formatter = _JsonFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)-8s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        formatter.converter = time.gmtime  # type: ignore[assignment]

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
    return logger


log = _setup()
