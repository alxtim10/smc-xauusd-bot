"""
src/utils/logger.py
───────────────────
Centralised logging configuration built on loguru.

Usage
-----
    from src.utils.logger import get_logger

    log = get_logger(__name__)
    log.info("Bot started")
    log.bind(symbol="AAPL", order_id="abc123").info("Order submitted")
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Union

from loguru import logger

from src.utils.helpers import load_settings

# ── Internal state ────────────────────────────────────────────────────────────
_configured = False


def _ensure_configured() -> None:
    """Configure loguru sinks exactly once (idempotent)."""
    global _configured
    if _configured:
        return

    settings = load_settings()
    log_cfg = settings.get("logging", {})

    level: str = log_cfg.get("level", "INFO").upper()
    rotation: str = log_cfg.get("rotation", "00:00")
    retention: str = log_cfg.get("retention", "30 days")
    fmt: str = log_cfg.get(
        "format",
        "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{line} | {message}",
    )
    log_dir: Path = Path(log_cfg.get("log_dir", "logs"))
    serialize: bool = log_cfg.get("serialize", False)   # JSON mode

    app_name: str = settings.get("app", {}).get("name", "trading-bot")

    # Remove the default loguru handler
    logger.remove()

    # ── Sink 1: stdout (colourised, human-readable) ───────────────────────────
    logger.add(
        sys.stdout,
        level=level,
        format=fmt,
        colorize=True,
        backtrace=True,
        diagnose=True,
    )

    # ── Sink 2: rotating daily log file ──────────────────────────────────────
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{app_name}.log"

    logger.add(
        str(log_file),
        level=level,
        format=fmt,
        rotation=rotation,        # rotate at midnight
        retention=retention,      # keep 30 days
        compression="gz",         # compress rotated files
        backtrace=True,
        diagnose=True,
        serialize=serialize,
        enqueue=True,             # thread-safe async writes
    )

    # ── Sink 3: separate error-only log ──────────────────────────────────────
    error_file = log_dir / f"{app_name}.error.log"
    logger.add(
        str(error_file),
        level="ERROR",
        format=fmt,
        rotation=rotation,
        retention=retention,
        compression="gz",
        backtrace=True,
        diagnose=True,
        serialize=serialize,
        enqueue=True,
    )

    logger.debug(
        "Logging configured | level={} | log_dir={} | serialize={}",
        level,
        log_dir,
        serialize,
    )
    _configured = True


def get_logger(name: str = "trading-bot") -> "logger":  # type: ignore[name-defined]
    """
    Return a loguru logger bound to *name* (typically ``__name__``).

    The logger is configured on first call; subsequent calls are cheap.

    Parameters
    ----------
    name:
        Module or component identifier shown in every log record.

    Returns
    -------
    loguru.Logger
        A context-bound logger instance.
    """
    _ensure_configured()
    return logger.bind(module=name)


# ── Convenience re-export ─────────────────────────────────────────────────────
__all__ = ["get_logger"]
