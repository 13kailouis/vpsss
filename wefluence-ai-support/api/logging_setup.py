"""
LOG TERSTRUKTUR
===============

Berkas lama menulis `print(f"Error: {e}")` dan punya tiga blok `except: pass`.
Akibatnya: kalau AI support diam-diam gagal, tidak ada apa pun di `docker compose
logs` yang bisa dicari. Di sini semua log satu baris JSON supaya bisa di-grep:

    docker compose logs ai-support | grep '"event":"llm.failed"'
"""

import json
import logging
import sys
import time

from . import config

_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
}


class _JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup():
    root = logging.getLogger()
    if getattr(root, "_wefluence_configured", False):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root.handlers = [handler]
    root.setLevel(getattr(logging, config.LOG_LEVEL.upper(), logging.INFO))
    # Log akses gunicorn/werkzeug tidak berguna di belakang nginx yang sudah
    # mencatat semuanya, dan malah menenggelamkan log kita.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    root._wefluence_configured = True


def get(name):
    setup()
    return logging.getLogger(name)


def short_uid(uid):
    """UID untuk log. Dipotong supaya log bukan daftar identitas lengkap."""
    if not uid:
        return "-"
    return uid[:6] + "~"
