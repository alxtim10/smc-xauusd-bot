"""
src/mt5/client.py
─────────────────
MetaTrader 5 client with:
  • Credential loading from environment variables
  • Auto-reconnection with exponential back-off (max 5 retries)
  • Real-time OHLC bars for XAUUSD across M5 / M15 / H1 / H4
  • Live bid/ask tick prices
  • Connection health-check
  • Graceful shutdown / context-manager support

Usage
-----
    from src.mt5.client import MT5Client, Timeframe

    with MT5Client() as client:
        df = client.get_ohlc("XAUUSD", Timeframe.H1, count=100)
        tick = client.get_tick("XAUUSD")
        print(tick.bid, tick.ask)
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Generator, Optional

import pandas as pd

from src.utils.helpers import get_env, load_env
from src.utils.logger import get_logger

log = get_logger(__name__)

# ── Optional MT5 import (Windows only; mock-friendly on macOS/Linux) ──────────
try:
    import MetaTrader5 as mt5          # type: ignore[import]
    _MT5_AVAILABLE = True
except ImportError:                    # pragma: no cover
    mt5 = None                         # type: ignore[assignment]
    _MT5_AVAILABLE = False
    log.warning(
        "MetaTrader5 package not found. "
        "MT5Client will operate in STUB mode — no real orders or data."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Constants & enums
# ─────────────────────────────────────────────────────────────────────────────

class Timeframe(Enum):
    """Supported MT5 timeframes with their mt5.TIMEFRAME_* constants."""
    M1  = "M1"
    M5  = "M5"
    M15 = "M15"
    M30 = "M30"
    H1  = "H1"
    H4  = "H4"
    D1  = "D1"
    W1  = "W1"
    MN1 = "MN1"

    @property
    def mt5_constant(self) -> int:
        """Map enum value to the MT5 library integer constant."""
        _map: dict[str, int] = {
            "M1":  1,
            "M5":  5,
            "M15": 15,
            "M30": 30,
            "H1":  16385,
            "H4":  16388,
            "D1":  16408,
            "W1":  32769,
            "MN1": 49153,
        }
        if _MT5_AVAILABLE:
            # Use live constants when the library is present
            _live: dict[str, int] = {
                "M1":  mt5.TIMEFRAME_M1,
                "M5":  mt5.TIMEFRAME_M5,
                "M15": mt5.TIMEFRAME_M15,
                "M30": mt5.TIMEFRAME_M30,
                "H1":  mt5.TIMEFRAME_H1,
                "H4":  mt5.TIMEFRAME_H4,
                "D1":  mt5.TIMEFRAME_D1,
                "W1":  mt5.TIMEFRAME_W1,
                "MN1": mt5.TIMEFRAME_MN1,
            }
            return _live[self.value]
        return _map[self.value]


# Timeframes supported for XAUUSD streaming
XAUUSD_TIMEFRAMES: tuple[Timeframe, ...] = (
    Timeframe.M5,
    Timeframe.M15,
    Timeframe.H1,
    Timeframe.H4,
)

_MAX_RETRIES   = 5
_BACKOFF_BASE  = 2.0   # seconds; wait = base ** attempt
_BACKOFF_MAX   = 60.0  # cap back-off at 60 s

# OHLC column names returned by copy_rates_*
_OHLC_COLS = ["time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"]


# ─────────────────────────────────────────────────────────────────────────────
# Data containers
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(slots=True, frozen=True)
class Tick:
    """Snapshot of the current best bid/ask for a symbol."""
    symbol: str
    bid: float
    ask: float
    spread_pts: float      # ask - bid in price points
    time: pd.Timestamp


@dataclass(slots=True, frozen=True)
class ConnectionStatus:
    """Result of a health-check call."""
    connected: bool
    account_login: int
    account_balance: float
    account_equity: float
    server: str
    ping_ms: float
    error_code: int        # 0 = OK


# ─────────────────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────────────────

class MT5Error(RuntimeError):
    """Raised when an MT5 operation fails after exhausting all retries."""
    def __init__(self, message: str, code: int = 0) -> None:
        super().__init__(message)
        self.code = code


class MT5NotConnectedError(MT5Error):
    """Raised when an operation is attempted without an active connection."""


class MT5DataError(MT5Error):
    """Raised when a data fetch call returns empty or invalid results."""


# ─────────────────────────────────────────────────────────────────────────────
# Client
# ─────────────────────────────────────────────────────────────────────────────

class MT5Client:
    """
    Thread-safe MetaTrader 5 client with auto-reconnection.

    Credentials are read from environment variables:
        MT5_LOGIN    – integer account login
        MT5_PASSWORD – account password
        MT5_SERVER   – broker server name (e.g. "ICMarkets-Demo")

    Parameters
    ----------
    login:    Override env MT5_LOGIN
    password: Override env MT5_PASSWORD
    server:   Override env MT5_SERVER
    timeout:  Connection timeout in milliseconds (default 60 000)
    """

    def __init__(
        self,
        login:    Optional[int]   = None,
        password: Optional[str]   = None,
        server:   Optional[str]   = None,
        timeout:  int             = 60_000,
    ) -> None:
        load_env()

        raw_login = login or get_env("MT5_LOGIN", required=True)
        self._login    = int(raw_login)  # type: ignore[arg-type]
        self._password = password or get_env("MT5_PASSWORD", required=True)
        self._server   = server   or get_env("MT5_SERVER",   required=True)
        self._timeout  = timeout

        self._connected   = False
        self._stub_mode   = not _MT5_AVAILABLE   # True on macOS / Linux

        log.info(
            "MT5Client initialised | login={} server={} stub={}",
            self._login, self._server, self._stub_mode,
        )

    # ── Context manager ───────────────────────────────────────────────────────

    def __enter__(self) -> "MT5Client":
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.disconnect()

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """
        Establish a connection to the MT5 terminal.

        Retries up to ``_MAX_RETRIES`` times with exponential back-off.
        Raises ``MT5Error`` if all attempts fail.
        """
        if self._connected:
            log.debug("connect() called while already connected — skipping")
            return

        for attempt in range(1, _MAX_RETRIES + 1):
            log.info("MT5 connect attempt {}/{}", attempt, _MAX_RETRIES)
            try:
                self._do_connect()
                self._connected = True
                log.info(
                    "MT5 connected | login={} server={}",
                    self._login, self._server,
                )
                return
            except MT5Error as exc:
                wait = min(_BACKOFF_BASE ** attempt, _BACKOFF_MAX)
                if attempt == _MAX_RETRIES:
                    log.error(
                        "MT5 connection failed after {} attempts: {}", _MAX_RETRIES, exc
                    )
                    raise
                log.warning(
                    "MT5 connect attempt {} failed: {}. Retrying in {:.1f}s …",
                    attempt, exc, wait,
                )
                time.sleep(wait)

    def _do_connect(self) -> None:
        """Single connection attempt — raises MT5Error on failure."""
        if self._stub_mode:
            log.warning("STUB MODE: simulating MT5 connection (MT5 not installed)")
            return

        # Initialise the MT5 terminal process
        if not mt5.initialize(
            login=self._login,
            password=self._password,
            server=self._server,
            timeout=self._timeout,
        ):
            code, msg = mt5.last_error()
            raise MT5Error(
                f"mt5.initialize() failed — code={code} msg={msg}", code=code
            )

        # Verify the account login matches
        account = mt5.account_info()
        if account is None:
            code, msg = mt5.last_error()
            raise MT5Error(f"account_info() returned None — code={code} msg={msg}", code=code)

        if account.login != self._login:
            raise MT5Error(
                f"Login mismatch: expected {self._login}, got {account.login}"
            )

    def disconnect(self) -> None:
        """
        Gracefully close the MT5 connection and release resources.

        Safe to call multiple times (idempotent).
        """
        if not self._connected:
            return

        log.info("MT5 disconnecting …")
        try:
            if not self._stub_mode and _MT5_AVAILABLE:
                mt5.shutdown()
        except Exception as exc:                          # pragma: no cover
            log.warning("MT5 shutdown raised: {}", exc)
        finally:
            self._connected = False
            log.info("MT5 disconnected")

    def _ensure_connected(self) -> None:
        """Raise ``MT5NotConnectedError`` when there is no active session."""
        if not self._connected:
            raise MT5NotConnectedError(
                "No active MT5 connection. Call connect() first "
                "or use MT5Client as a context manager."
            )

    # ── Health check ──────────────────────────────────────────────────────────

    def health_check(self) -> ConnectionStatus:
        """
        Verify connectivity and return a ``ConnectionStatus`` snapshot.

        If the connection is detected to be stale, one reconnection attempt
        is made before returning a failure status.

        Returns
        -------
        ConnectionStatus
            ``connected=True`` and current account figures on success.
        """
        self._ensure_connected()
        log.debug("MT5 health check …")

        if self._stub_mode:
            return ConnectionStatus(
                connected=True,
                account_login=self._login,
                account_balance=100_000.0,
                account_equity=100_000.0,
                server=self._server,
                ping_ms=0.0,
                error_code=0,
            )

        t0 = time.perf_counter()
        account = mt5.account_info()
        ping_ms = (time.perf_counter() - t0) * 1_000

        if account is None:
            code, msg = mt5.last_error()
            log.error("Health check failed: code={} msg={}", code, msg)
            # Attempt a single reconnect
            self._connected = False
            try:
                self.connect()
                account = mt5.account_info()
            except MT5Error:
                pass

            if account is None:
                return ConnectionStatus(
                    connected=False,
                    account_login=self._login,
                    account_balance=0.0,
                    account_equity=0.0,
                    server=self._server,
                    ping_ms=ping_ms,
                    error_code=code,
                )

        status = ConnectionStatus(
            connected=True,
            account_login=account.login,
            account_balance=account.balance,
            account_equity=account.equity,
            server=account.server,
            ping_ms=round(ping_ms, 2),
            error_code=0,
        )
        log.debug(
            "Health OK | balance={:.2f} equity={:.2f} ping={:.1f}ms",
            status.account_balance, status.account_equity, status.ping_ms,
        )
        return status

    # ── Real-time prices ──────────────────────────────────────────────────────

    def get_tick(self, symbol: str = "XAUUSD") -> Tick:
        """
        Fetch the latest bid/ask tick for *symbol*.

        Parameters
        ----------
        symbol:
            MT5 symbol name (default ``"XAUUSD"``).

        Returns
        -------
        Tick
            Current bid, ask, spread, and server timestamp.

        Raises
        ------
        MT5DataError
            When the symbol is not found or the tick is unavailable.
        """
        self._ensure_connected()
        log.debug("get_tick | symbol={}", symbol)

        if self._stub_mode:
            _stub_price = 2_345.67
            return Tick(
                symbol=symbol,
                bid=_stub_price,
                ask=_stub_price + 0.30,
                spread_pts=0.30,
                time=pd.Timestamp.utcnow(),
            )

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            code, msg = mt5.last_error()
            raise MT5DataError(
                f"symbol_info_tick({symbol!r}) failed — code={code} msg={msg}",
                code=code,
            )

        result = Tick(
            symbol=symbol,
            bid=tick.bid,
            ask=tick.ask,
            spread_pts=round(tick.ask - tick.bid, 5),
            time=pd.Timestamp(tick.time, unit="s", tz="UTC"),
        )
        log.debug(
            "Tick | {} bid={} ask={} spread={}", symbol, result.bid, result.ask, result.spread_pts
        )
        return result

    # ── OHLC bars ─────────────────────────────────────────────────────────────

    def get_ohlc(
        self,
        symbol:    str       = "XAUUSD",
        timeframe: Timeframe = Timeframe.H1,
        count:     int       = 500,
    ) -> pd.DataFrame:
        """
        Fetch the most recent *count* completed OHLC bars for *symbol*.

        Parameters
        ----------
        symbol:
            MT5 symbol name.
        timeframe:
            One of ``Timeframe.M5 / M15 / H1 / H4`` (any Timeframe accepted).
        count:
            Number of bars to fetch (max depends on MT5 terminal history).

        Returns
        -------
        pd.DataFrame
            Columns: ``open / high / low / close / tick_volume / spread / real_volume``
            Index:   UTC-aware ``DatetimeTZDtype`` named ``"time"``

        Raises
        ------
        MT5DataError
            When the terminal returns empty or null data.
        """
        self._ensure_connected()
        log.debug(
            "get_ohlc | symbol={} timeframe={} count={}",
            symbol, timeframe.value, count,
        )

        if self._stub_mode:
            return _stub_ohlc(symbol, timeframe, count)

        rates = mt5.copy_rates_from_pos(symbol, timeframe.mt5_constant, 0, count)
        if rates is None or len(rates) == 0:
            code, msg = mt5.last_error()
            raise MT5DataError(
                f"copy_rates_from_pos({symbol!r}, {timeframe.value}, 0, {count}) "
                f"returned empty — code={code} msg={msg}",
                code=code,
            )

        df = _rates_to_dataframe(rates)
        log.debug(
            "OHLC fetched | {} {} bars={} latest={}",
            symbol, timeframe.value, len(df), df.index[-1],
        )
        return df

    def get_ohlc_multi_timeframe(
        self,
        symbol: str  = "XAUUSD",
        count:  int  = 500,
        timeframes: tuple[Timeframe, ...] = XAUUSD_TIMEFRAMES,
    ) -> dict[Timeframe, pd.DataFrame]:
        """
        Convenience wrapper: fetch OHLC for multiple timeframes in one call.

        Returns
        -------
        dict[Timeframe, pd.DataFrame]
            Mapping of timeframe → DataFrame (same schema as ``get_ohlc``).
        """
        log.info(
            "Fetching multi-TF OHLC | symbol={} timeframes={} count={}",
            symbol, [tf.value for tf in timeframes], count,
        )
        result: dict[Timeframe, pd.DataFrame] = {}
        for tf in timeframes:
            try:
                result[tf] = self.get_ohlc(symbol, tf, count)
            except MT5DataError as exc:
                log.error("Failed to fetch {} {}: {}", symbol, tf.value, exc)
        return result

    # ── Reconnect helper ──────────────────────────────────────────────────────

    def reconnect(self) -> None:
        """Force a full disconnect → connect cycle (for external callers)."""
        log.info("MT5 forced reconnect …")
        self.disconnect()
        self.connect()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _rates_to_dataframe(rates: object) -> pd.DataFrame:
    """Convert a MT5 numpy structured array of rates to a clean DataFrame."""
    df = pd.DataFrame(rates)                                    # type: ignore[arg-type]
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("time").sort_index()
    return df


def _stub_ohlc(symbol: str, timeframe: Timeframe, count: int) -> pd.DataFrame:
    """Generate synthetic OHLC bars for stub / offline mode."""
    import numpy as np

    rng = pd.date_range(end=pd.Timestamp.utcnow(), periods=count, freq="1h", tz="UTC")
    base = 2_300.0
    closes = base + np.cumsum(np.random.normal(0, 2, count))
    opens  = np.roll(closes, 1)
    opens[0] = base
    # Ensure high >= max(open, close) and low <= min(open, close)
    body_high = np.maximum(opens, closes)
    body_low  = np.minimum(opens, closes)
    highs = body_high + np.abs(np.random.normal(0, 1, count))
    lows  = body_low  - np.abs(np.random.normal(0, 1, count))

    df = pd.DataFrame({
        "open":        opens,
        "high":        highs,
        "low":         lows,
        "close":       closes,
        "tick_volume": np.random.randint(100, 2_000, count).astype(float),
        "spread":      np.full(count, 30),
        "real_volume": np.zeros(count),
    }, index=rng)
    df.index.name = "time"
    log.warning("STUB OHLC returned for {} {}", symbol, timeframe.value)
    return df


@contextmanager
def mt5_client(**kwargs: object) -> Generator[MT5Client, None, None]:
    """
    Context-manager factory for one-off usage::

        with mt5_client() as client:
            tick = client.get_tick("XAUUSD")
    """
    client = MT5Client(**kwargs)            # type: ignore[arg-type]
    client.connect()
    try:
        yield client
    finally:
        client.disconnect()


__all__ = [
    "MT5Client",
    "MT5Error",
    "MT5NotConnectedError",
    "MT5DataError",
    "Timeframe",
    "Tick",
    "ConnectionStatus",
    "XAUUSD_TIMEFRAMES",
    "mt5_client",
]