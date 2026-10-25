"""Common uvicorn launcher so every service starts and shuts down the same way.

Uvicorn handles SIGTERM (sent by `docker compose stop/down`): it stops accepting new
connections, lets in-flight requests finish for up to `timeout_graceful_shutdown` seconds,
runs the app's lifespan shutdown, and exits with code 0.
"""
from __future__ import annotations

import sys
from collections.abc import Callable

import uvicorn
from fastapi import FastAPI

from setu_x.common.config import ConfigError, ServiceSettings
from setu_x.common.logging import configure_logging


def run(load_settings: Callable[[], ServiceSettings], build_app: Callable[[ServiceSettings], FastAPI]) -> None:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"FATAL {exc}", file=sys.stderr)
        sys.exit(2)
    configure_logging(settings.service_name, settings.instance, settings.env, settings.log_level)
    try:
        app = build_app(settings)
    except ConfigError as exc:
        print(f"FATAL {exc}", file=sys.stderr)
        sys.exit(2)
    uvicorn.run(
        app,
        host="0.0.0.0",  # inside the container; Compose publishes only to 127.0.0.1 on the host
        port=settings.port,
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=5,
    )
