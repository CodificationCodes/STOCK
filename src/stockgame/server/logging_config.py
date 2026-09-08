"""Logging setup.

Two formats: a readable console format for development, and single-line JSON
for production, where logs are usually being shipped somewhere that wants
structured fields.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)-22s %(message)s"
DATE_FORMAT = "%H:%M:%S"

#: Loggers whose records are the server's audit trail.
AUDIT_LOGGERS = ("stockgame.auth", "stockgame.trading", "stockgame.ws")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(
    level: str = "INFO", *, json_output: bool = False, log_file: Path | None = None
) -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter: logging.Formatter = (
        JsonFormatter() if json_output else logging.Formatter(LOG_FORMAT, DATE_FORMAT)
    )

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_file is not None:
        log_file = Path(log_file).expanduser()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        rotating.setFormatter(JsonFormatter() if json_output else logging.Formatter(LOG_FORMAT))
        root.addHandler(rotating)

    # Uvicorn's access log duplicates what we already record and is noisy at
    # a 2-second tick; keep its errors, drop its chatter.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
