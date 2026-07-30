#!/usr/bin/env python3
"""
logging_utils.py
One place to configure logging for every module in the pipeline.
Logs to stdout (for GitHub Actions) and to a rotating file under logs/.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler

from config import LOG_DIR

_CONFIGURED = False


def get_logger(name: str) -> logging.Logger:
    global _CONFIGURED
    root = logging.getLogger()

    if not _CONFIGURED:
        root.setLevel(logging.INFO)
        fmt = logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(fmt)
        root.addHandler(stream)

        try:
            file_handler = RotatingFileHandler(
                LOG_DIR / "pipeline.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
            )
            file_handler.setFormatter(fmt)
            root.addHandler(file_handler)
        except OSError:
            # Read-only filesystem etc — stdout logging still works.
            pass

        _CONFIGURED = True

    return logging.getLogger(name)
