import logging
import sys
from . import config


def _setup() -> logging.Logger:
    logger = logging.getLogger("context-engine")
    if logger.handlers:
        return logger
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, config.LOG_LEVEL, logging.DEBUG))
    return logger


log = _setup()
