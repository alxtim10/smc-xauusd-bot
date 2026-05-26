"""
src/utils/helpers.py
────────────────────
Shared utility functions:
  • load_env()       – load .env into os.environ
  • load_settings()  – parse config/settings.yaml (with ${VAR} interpolation)
  • retry()          – exponential-back-off decorator
  • utc_now()        – timezone-aware UTC timestamp
  • round_to_tick()  – price rounding for order submission
  • pct_change()     – percentage change helper
  • flatten_dict()   – nested dict → dot-notation dict
"""

from __future__ import annotations

import functools
import os
import re
import time
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

import yaml
from dotenv import load_dotenv

# ── Types ─────────────────────────────────────────────────────────────────────
F = TypeVar("F", bound=Callable[..., Any])

# ── Internal cache ────────────────────────────────────────────────────────────
_settings_cache: dict[str, Any] | None = None

# ── Paths ─────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"
SETTINGS_FILE = PROJECT_ROOT / "config" / "settings.yaml"


# ─────────────────────────────────────────────────────────────────────────────
# Environment
# ─────────────────────────────────────────────────────────────────────────────

def load_env(env_path: Path | str | None = None) -> None:
    """
    Load environment variables from a .env file.

    Parameters
    ----------
    env_path:
        Explicit path to the .env file.  Defaults to ``<project_root>/.env``.
    """
    path = Path(env_path) if env_path else ENV_FILE
    if path.exists():
        load_dotenv(dotenv_path=path, override=False)
    else:
        # Warn without loguru to avoid circular imports
        print(f"[helpers] WARNING: .env file not found at {path}. "
              "Copy .env.example → .env and fill in your secrets.")


def get_env(key: str, default: str | None = None, required: bool = False) -> str | None:
    """
    Retrieve an environment variable.

    Parameters
    ----------
    key:
        Variable name.
    default:
        Fallback value when the variable is absent.
    required:
        Raise ``KeyError`` if the variable is absent and no default is given.
    """
    value = os.environ.get(key, default)
    if required and value is None:
        raise KeyError(f"Required environment variable '{key}' is not set.")
    return value


# ─────────────────────────────────────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────────────────────────────────────

_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _interpolate(value: Any) -> Any:
    """Recursively replace ``${VAR}`` placeholders with env values."""
    if isinstance(value, str):
        def _replace(match: re.Match) -> str:
            var = match.group(1)
            return os.environ.get(var, match.group(0))  # keep placeholder if unset
        return _ENV_PATTERN.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


def load_settings(
    settings_path: Path | str | None = None,
    *,
    reload: bool = False,
) -> dict[str, Any]:
    """
    Parse ``config/settings.yaml`` and interpolate ``${ENV_VAR}`` placeholders.

    Results are cached; pass ``reload=True`` to force a fresh read.

    Parameters
    ----------
    settings_path:
        Override the default settings file location.
    reload:
        Discard cached settings and re-read from disk.

    Returns
    -------
    dict
        Fully resolved settings dictionary.
    """
    global _settings_cache

    if _settings_cache is not None and not reload:
        return _settings_cache

    load_env()   # ensure env is populated before interpolation

    path = Path(settings_path) if settings_path else SETTINGS_FILE
    if not path.exists():
        raise FileNotFoundError(
            f"Settings file not found: {path}. "
            "Did you copy config/settings.yaml from the template?"
        )

    with path.open("r", encoding="utf-8") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}

    _settings_cache = _interpolate(raw)
    return _settings_cache


def get_setting(key_path: str, default: Any = None) -> Any:
    """
    Retrieve a nested setting using dot notation.

    Example
    -------
    >>> get_setting("risk.stop_loss_pct")
    0.02
    """
    settings = load_settings()
    parts = key_path.split(".")
    node: Any = settings
    for part in parts:
        if not isinstance(node, dict):
            return default
        node = node.get(part)
        if node is None:
            return default
    return node


# ─────────────────────────────────────────────────────────────────────────────
# Retry decorator
# ─────────────────────────────────────────────────────────────────────────────

def retry(
    max_attempts: int = 3,
    exceptions: tuple[type[Exception], ...] = (Exception,),
    backoff_factor: float = 1.5,
    initial_wait: float = 1.0,
) -> Callable[[F], F]:
    """
    Exponential-back-off retry decorator.

    Parameters
    ----------
    max_attempts:
        Total number of attempts (including the first).
    exceptions:
        Exception types that trigger a retry.
    backoff_factor:
        Multiplier applied to the wait time after each failure.
    initial_wait:
        Seconds to wait before the first retry.

    Example
    -------
    >>> @retry(max_attempts=5, exceptions=(ConnectionError,))
    ... def fetch_price(symbol: str) -> float: ...
    """
    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            wait = initial_wait
            last_exc: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt == max_attempts:
                        break
                    print(
                        f"[retry] {func.__name__} failed (attempt {attempt}/{max_attempts}): "
                        f"{exc!r}. Retrying in {wait:.1f}s …"
                    )
                    time.sleep(wait)
                    wait *= backoff_factor
            raise RuntimeError(
                f"{func.__name__} failed after {max_attempts} attempts."
            ) from last_exc
        return wrapper  # type: ignore[return-value]
    return decorator


# ─────────────────────────────────────────────────────────────────────────────
# Date / time
# ─────────────────────────────────────────────────────────────────────────────

def utc_now() -> datetime:
    """Return the current UTC datetime (timezone-aware)."""
    return datetime.now(tz=timezone.utc)


def timestamp_ms() -> int:
    """Return the current UTC time as milliseconds since epoch (int)."""
    return int(utc_now().timestamp() * 1_000)


# ─────────────────────────────────────────────────────────────────────────────
# Numeric helpers
# ─────────────────────────────────────────────────────────────────────────────

def round_to_tick(price: float, tick_size: float = 0.01) -> float:
    """
    Round *price* to the nearest *tick_size*.

    Parameters
    ----------
    price:
        Raw price float.
    tick_size:
        Minimum price increment (e.g. 0.01 for equities, 0.10 for some futures).

    Returns
    -------
    float
        Price rounded to the nearest tick.
    """
    tick = Decimal(str(tick_size))
    rounded = (Decimal(str(price)) / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick
    return float(rounded)


def pct_change(old: float, new: float) -> float:
    """
    Calculate percentage change from *old* to *new*.

    Returns
    -------
    float
        E.g. 0.05 for a +5 % move.  Returns 0.0 when *old* is zero.
    """
    if old == 0:
        return 0.0
    return (new - old) / abs(old)


def clamp(value: float, lo: float, hi: float) -> float:
    """Clamp *value* to the inclusive range [*lo*, *hi*]."""
    return max(lo, min(hi, value))


# ─────────────────────────────────────────────────────────────────────────────
# Dict utilities
# ─────────────────────────────────────────────────────────────────────────────

def flatten_dict(
    d: dict[str, Any],
    parent_key: str = "",
    sep: str = ".",
) -> dict[str, Any]:
    """
    Flatten a nested dictionary to a single-level dict with dot-notation keys.

    Example
    -------
    >>> flatten_dict({"a": {"b": 1, "c": {"d": 2}}})
    {'a.b': 1, 'a.c.d': 2}
    """
    items: list[tuple[str, Any]] = []
    for key, value in d.items():
        new_key = f"{parent_key}{sep}{key}" if parent_key else key
        if isinstance(value, dict):
            items.extend(flatten_dict(value, new_key, sep=sep).items())
        else:
            items.append((new_key, value))
    return dict(items)


# ─────────────────────────────────────────────────────────────────────────────
# Exports
# ─────────────────────────────────────────────────────────────────────────────

__all__ = [
    "load_env",
    "get_env",
    "load_settings",
    "get_setting",
    "retry",
    "utc_now",
    "timestamp_ms",
    "round_to_tick",
    "pct_change",
    "clamp",
    "flatten_dict",
    "PROJECT_ROOT",
]
