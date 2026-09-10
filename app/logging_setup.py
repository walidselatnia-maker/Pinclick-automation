"""Rotating file logging (PRD Gap 6)."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .paths import LOGS_DIR, ensure_dirs

_CONFIGURED = False


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    global _CONFIGURED
    logger = logging.getLogger("pinclicks")
    if _CONFIGURED:
        return logger

    ensure_dirs()
    logger.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 5 files x 2 MB is plenty for a local tool and keeps the folder tidy.
    file_handler = RotatingFileHandler(
        LOGS_DIR / "pinclicks.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    logger.propagate = False
    _CONFIGURED = True
    return logger


def get_logger(name: str = "") -> logging.Logger:
    base = setup_logging()
    return base.getChild(name) if name else base
