"""Structured JSON logging to stdout (one JSON object per line)."""
from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

_STD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime", "color_message"}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str, instance: str, env: str) -> None:
        super().__init__()
        self.static = {"service": service, "instance": instance, "env": env}

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            **self.static,
            "event": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(service: str, instance: str, env: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service, instance, env))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # uvicorn's own loggers propagate to root; its access log is replaced by our middleware log.
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("httpx").setLevel("WARNING")
