import json
import logging
import sys
import time
from contextvars import ContextVar
from . import config

# Per-request ID injected by middleware; empty string when no active request
request_id_var: ContextVar[str] = ContextVar("request_id", default="")

# TRACE sits below DEBUG (10) — use for raw content, per-token or per-file detail
TRACE = 5
logging.addLevelName(TRACE, "TRACE")
logging.TRACE = TRACE  # type: ignore[attr-defined]

# Keys present on every LogRecord — excluded from the structured "extra" block
_STANDARD_LOG_KEYS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "taskName",
})


class _Logger(logging.Logger):
    """Extends stdlib Logger with a trace() convenience method."""

    def trace(self, msg: object, *args: object, **kwargs: object) -> None:
        if self.isEnabledFor(TRACE):
            self._log(TRACE, msg, args, **kwargs)


# Must be called before getLogger() creates the instance
logging.setLoggerClass(_Logger)


class _JsonFormatter(logging.Formatter):
    converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        entry: dict = {
            "ts":     self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level":  record.levelname,
            "logger": record.name,
            "msg":    record.getMessage(),
        }
        rid = request_id_var.get("")
        if rid:
            entry["request_id"] = rid
        # caller location helps trace a line back to source when level <= DEBUG
        if record.levelno <= logging.DEBUG:
            entry["caller"] = f"{record.filename}:{record.lineno}:{record.funcName}"
        # structured extra fields — anything the caller passed via extra={"key": val}
        for key, val in record.__dict__.items():
            if key not in _STANDARD_LOG_KEYS and not key.startswith("_") and key not in entry:
                try:
                    json.dumps(val)
                    entry[key] = val
                except (TypeError, ValueError):
                    entry[key] = str(val)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            entry["stack"] = self.formatStack(record.stack_info)
        return json.dumps(entry)


def _setup() -> _Logger:
    logger: _Logger = logging.getLogger("context-engine")  # type: ignore[assignment]
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
    logger.propagate = False

    # Route uvicorn through our formatter so all output is consistent.
    # Silence uvicorn.access — _RequestLog middleware already logs every request.
    for name in ("uvicorn", "uvicorn.error"):
        uv = logging.getLogger(name)
        uv.handlers = [handler]
        uv.propagate = False
    logging.getLogger("uvicorn.access").handlers = [logging.NullHandler()]
    logging.getLogger("uvicorn.access").propagate = False

    return logger


log: _Logger = _setup()
