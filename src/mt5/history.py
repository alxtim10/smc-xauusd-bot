"""
src/mt5/history.py
──────────────────
Fetch historical OHLC bars from MetaTrader 5 with a transparent on-disk
Parquet cache.  Re-uses the cache for the unchanged portion of history and
only fetches the missing tail from the terminal on subsequent calls.

Architecture
────────────
  ┌──────────┐   miss / stale   ┌────────────┐   copy_rates   ┌─────────┐
  │  Caller  │ ───────────────► │ HistoryMgr │ ─────────────► │  MT5    │
  │          │ ◄─────────────── │            │ ◄───────────── │terminal │
  └──────────┘   DataFrame      │  + cache   │                └─────────┘
                                │   (Parquet)│
                                └────────────┘

Cache layout
────────────
    <cache_dir>/<SYMBOL>/<TIMEFRAME>.parquet

    The file stores every bar fetched to date; incremental fetches append
    only the new bars and re-save atomically via a temp file.

Usage
-----
    from src.mt5.history import HistoryManager, BarRequest
    from src.mt5.client  import MT5Client, Timeframe

    mgr = HistoryManager(cache_dir="data/history")

    with MT5Client() as client:
        request = BarRequest(symbol="XAUUSD", timeframe=Timeframe.H1, bars=2000)
        df = mgr.fetch(client, request)
        print(df.tail())
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import pandas as pd

from src.mt5.client import MT5DataError, Timeframe
from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.mt5.client import MT5Client

log = get_logger(__name__)

try:
    import MetaTrader5 as mt5   # type: ignore[import]
    _MT5_AVAILABLE = True
except ImportError:
    mt5 = None                  # type: ignore[assignment]
    _MT5_AVAILABLE = False

# ── Cache backend: prefer Parquet, fall back to CSV (always available) ────────
try:
    import pyarrow  # noqa: F401  type: ignore[import]
    _PARQUET_ENGINE: str | None = "pyarrow"
except ImportError:
    try:
        import fastparquet  # noqa: F401  type: ignore[import]
        _PARQUET_ENGINE = "fastparquet"
    except ImportError:
        _PARQUET_ENGINE = None

# _CACHE_FORMAT drives all read/write; "csv" is always available as a fallback
_CACHE_FORMAT = "parquet" if _PARQUET_ENGINE else "csv"

if _CACHE_FORMAT == "csv":
    log.warning(
        "pyarrow/fastparquet not installed — history cache will use CSV. "
        "Add \'pyarrow\' to requirements.txt for faster Parquet storage."
    )

_OHLC_DTYPES: dict[str, str] = {
    "open":        "float64",
    "high":        "float64",
    "low":         "float64",
    "close":       "float64",
    "tick_volume": "int64",
    "spread":      "int32",
    "real_volume": "int64",
}


# ─────────────────────────────────────────────────────────────────────────────
# Request / response models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BarRequest:
    """
    Parameters for a historical-bar fetch.

    Attributes
    ----------
    symbol:
        MT5 symbol name (e.g. ``"XAUUSD"``).
    timeframe:
        Timeframe enum value.
    bars:
        Total number of bars requested (including cached portion).
    from_date:
        If set, fetch bars starting from this UTC datetime instead of
        using a rolling *bars* count.  Takes precedence over *bars*.
    force_refresh:
        When ``True``, ignore the on-disk cache and re-fetch everything
        from MT5.
    """
    symbol:        str
    timeframe:     Timeframe
    bars:          int              = 500
    from_date:     Optional[datetime] = None
    force_refresh: bool             = False

    @property
    def cache_key(self) -> str:
        """Unique string key for this symbol+timeframe combination."""
        return f"{self.symbol}_{self.timeframe.value}"


@dataclass
class FetchResult:
    """
    Result returned by ``HistoryManager.fetch()``.

    Attributes
    ----------
    data:
        The full DataFrame (cached + new bars merged).
    symbol:
        Symbol name.
    timeframe:
        Timeframe used.
    total_bars:
        Total bar count in *data*.
    new_bars:
        Number of bars that were freshly fetched from MT5.
    from_cache:
        ``True`` when *all* data came from the on-disk cache.
    cache_path:
        Filesystem path to the Parquet cache file (or ``None`` if disabled).
    """
    data:       pd.DataFrame
    symbol:     str
    timeframe:  Timeframe
    total_bars: int
    new_bars:   int
    from_cache: bool
    cache_path: Optional[Path] = None


# ─────────────────────────────────────────────────────────────────────────────
# Manager
# ─────────────────────────────────────────────────────────────────────────────

class HistoryManager:
    """
    Fetch and cache historical OHLC bars from MT5.

    Parameters
    ----------
    cache_dir:
        Root directory for Parquet cache files.
        Defaults to ``"data/history"`` relative to CWD.
    max_bars_per_request:
        Hard ceiling on bars fetched in a single MT5 call (avoids timeouts).
    """

    def __init__(
        self,
        cache_dir:            str | Path = "data/history",
        max_bars_per_request: int        = 5_000,
    ) -> None:
        self._cache_dir           = Path(cache_dir)
        self._max_bars_per_request = max_bars_per_request
        self._lock                = threading.Lock()   # serialise cache writes

        self._cache_dir.mkdir(parents=True, exist_ok=True)
        log.info(
            "HistoryManager initialised | cache_dir={} parquet_engine={}",
            self._cache_dir, _PARQUET_ENGINE,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch(self, client: "MT5Client", request: BarRequest) -> FetchResult:
        """
        Return historical bars for *request*, merging cached and live data.

        Algorithm
        ---------
        1. Load the existing Parquet cache (if any and not force_refresh).
        2. Determine the earliest timestamp we need to cover.
        3. Fetch only the *missing* tail from MT5 (incremental refresh).
        4. Merge, deduplicate, sort, and persist the updated cache.
        5. Return the requested window.

        Parameters
        ----------
        client:
            A connected ``MT5Client``.
        request:
            Fetch parameters.

        Returns
        -------
        FetchResult
        """
        log.info(
            "HistoryManager.fetch | {} {} bars={} force={}",
            request.symbol, request.timeframe.value,
            request.bars, request.force_refresh,
        )

        cache_path = self._cache_path(request)
        cached_df  = self._load_cache(cache_path, request)

        # Decide how many bars to pull from MT5
        if cached_df is not None and not request.force_refresh:
            # Already have some data — only fetch bars newer than cache tail
            tail_ts      = cached_df.index[-1]
            bars_to_fetch = self._bars_missing(cached_df, request)
            log.debug(
                "Cache has {} bars; tail={}; fetching {} more",
                len(cached_df), tail_ts, bars_to_fetch,
            )
        else:
            bars_to_fetch = min(request.bars, self._max_bars_per_request)
            cached_df     = None

        # Fetch from MT5
        new_df   = self._fetch_from_mt5(client, request, bars_to_fetch)
        new_bars = len(new_df)

        # Merge
        merged = _merge(cached_df, new_df)

        # Trim to the requested window
        if request.from_date:
            from_utc = request.from_date.replace(tzinfo=timezone.utc) \
                if request.from_date.tzinfo is None else request.from_date
            merged = merged[merged.index >= pd.Timestamp(from_utc)]
        else:
            merged = merged.tail(request.bars)

        # Persist (always — CSV fallback is available even without pyarrow)
        self._save_cache(cache_path, merged)

        result = FetchResult(
            data       = merged,
            symbol     = request.symbol,
            timeframe  = request.timeframe,
            total_bars = len(merged),
            new_bars   = new_bars,
            from_cache = new_bars == 0,
            cache_path = cache_path,
        )
        log.info(
            "fetch complete | {} {} total={} new={} from_cache={}",
            request.symbol, request.timeframe.value,
            result.total_bars, result.new_bars, result.from_cache,
        )
        return result

    def invalidate(self, symbol: str, timeframe: Timeframe) -> bool:
        """
        Delete the on-disk cache for a specific symbol/timeframe.

        Returns
        -------
        bool
            ``True`` if a cache file was deleted, ``False`` if none existed.
        """
        request   = BarRequest(symbol=symbol, timeframe=timeframe)
        path      = self._cache_path(request)
        # Match either extension in case the format changed between runs
        for candidate in (path.parent / f"{path.stem}.parquet",
                          path.parent / f"{path.stem}.csv"):
            if candidate.exists():
                candidate.unlink()
                log.info("Cache invalidated | {}", candidate)
                return True
        log.debug("No cache to invalidate | {}", path)
        return False

    def invalidate_all(self) -> int:
        """Delete all cached Parquet files.  Returns the count removed."""
        count = 0
        for pattern in ("*.parquet", "*.csv"):
            for p in self._cache_dir.rglob(pattern):
                p.unlink()
                count += 1
        log.info("All caches invalidated | {} files removed", count)
        return count

    def cache_info(self) -> list[dict]:
        """
        Return a summary of all cached symbol/timeframe files.

        Returns
        -------
        list[dict]
            Each dict has keys: ``symbol``, ``timeframe``, ``bars``,
            ``from``, ``to``, ``size_kb``, ``path``.
        """
        info = []
        files = sorted(
            list(self._cache_dir.rglob("*.parquet")) +
            list(self._cache_dir.rglob("*.csv"))
        )
        for p in files:
            try:
                if p.suffix == ".parquet":
                    df = pd.read_parquet(p, engine=_PARQUET_ENGINE)
                else:
                    df = pd.read_csv(p, index_col="time", parse_dates=True)
                parts = p.stem.split("_", 1)  # SYMBOL_TIMEFRAME
                info.append({
                    "symbol":    parts[0] if parts else "?",
                    "timeframe": parts[1] if len(parts) > 1 else "?",
                    "bars":      len(df),
                    "from":      df.index[0]  if len(df) else None,
                    "to":        df.index[-1] if len(df) else None,
                    "size_kb":   round(p.stat().st_size / 1_024, 1),
                    "path":      str(p),
                })
            except Exception as exc:
                log.warning("Could not read cache file {}: {}", p, exc)
        return info

    # ── Internals ─────────────────────────────────────────────────────────────

    def _cache_path(self, request: BarRequest) -> Path:
        """Return the Parquet file path for a given request."""
        sub = self._cache_dir / request.symbol
        sub.mkdir(parents=True, exist_ok=True)
        ext = "parquet" if _CACHE_FORMAT == "parquet" else "csv"
        return sub / f"{request.timeframe.value}.{ext}"

    def _load_cache(
        self, path: Path, request: BarRequest
    ) -> Optional[pd.DataFrame]:
        """Load and validate a cache file (Parquet or CSV), or return None."""
        if request.force_refresh or not path.exists():
            return None
        try:
            if _CACHE_FORMAT == "parquet":
                df = pd.read_parquet(path, engine=_PARQUET_ENGINE)
            else:
                df = pd.read_csv(path, index_col="time", parse_dates=True)
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            df = df.sort_index()
            log.debug("Cache loaded | {} bars from {}", len(df), path)
            return df
        except Exception as exc:
            log.warning("Failed to load cache {}: {} — will re-fetch", path, exc)
            return None

    def _save_cache(self, path: Path, df: pd.DataFrame) -> None:
        """Atomically save *df* to *path* via a temp file (Parquet or CSV)."""
        with self._lock:
            suffix = ".parquet" if _CACHE_FORMAT == "parquet" else ".csv"
            tmp_path: str | None = None
            try:
                tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix, dir=path.parent)
                os.close(tmp_fd)
                if _CACHE_FORMAT == "parquet":
                    df.to_parquet(tmp_path, engine=_PARQUET_ENGINE)
                else:
                    df.to_csv(tmp_path, index=True)
                os.replace(tmp_path, path)
                log.debug("Cache saved | {} bars → {}", len(df), path)
            except Exception as exc:
                log.error("Failed to save cache {}: {}", path, exc)
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

    def _bars_missing(self, cached: pd.DataFrame, request: BarRequest) -> int:
        """Estimate how many new bars to fetch to top-up the cache."""
        if cached is None or len(cached) == 0:
            return min(request.bars, self._max_bars_per_request)
        shortfall = request.bars - len(cached)
        # Always fetch at least 2 bars to pick up the forming candle + 1 new
        return max(shortfall, 2)

    def _fetch_from_mt5(
        self,
        client:  "MT5Client",
        request: BarRequest,
        bars:    int,
    ) -> pd.DataFrame:
        """Fetch *bars* bars from MT5 for *request*.  Returns stub when offline."""
        if client._stub_mode:           # type: ignore[attr-defined]
            from src.mt5.client import _stub_ohlc   # noqa: PLC0415
            return _stub_ohlc(request.symbol, request.timeframe, bars)

        if request.from_date:
            from_utc = request.from_date.replace(tzinfo=timezone.utc) \
                if request.from_date.tzinfo is None else request.from_date
            rates = mt5.copy_rates_from(
                request.symbol,
                request.timeframe.mt5_constant,
                from_utc,
                bars,
            )
        else:
            rates = mt5.copy_rates_from_pos(
                request.symbol,
                request.timeframe.mt5_constant,
                0,
                bars,
            )

        if rates is None or len(rates) == 0:
            code, msg = mt5.last_error()
            raise MT5DataError(
                f"copy_rates for {request.symbol} {request.timeframe.value} "
                f"returned empty — code={code} msg={msg}",
                code=code,
            )

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("time").sort_index()

        log.debug(
            "Fetched from MT5 | {} {} bars={}",
            request.symbol, request.timeframe.value, len(df),
        )
        return df


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _merge(
    cached: Optional[pd.DataFrame],
    new:    pd.DataFrame,
) -> pd.DataFrame:
    """
    Merge *cached* and *new* DataFrames, deduplicate by timestamp index,
    and sort ascending.  The newer bar wins on timestamp conflicts.
    """
    if cached is None or len(cached) == 0:
        return new.sort_index()
    if len(new) == 0:
        return cached.sort_index()

    # new rows take precedence (later fetch = more accurate close)
    combined = pd.concat([cached, new])
    combined = combined[~combined.index.duplicated(keep="last")]
    return combined.sort_index()


def build_history_manager(cache_dir: str = "data/history") -> HistoryManager:
    """Factory function: create a ``HistoryManager`` with the standard cache path."""
    return HistoryManager(cache_dir=cache_dir)


__all__ = [
    "HistoryManager",
    "BarRequest",
    "FetchResult",
    "build_history_manager",
]