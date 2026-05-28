"""
tests/test_mt5.py
─────────────────
Verification script for the MT5 client module.

Runs in two modes automatically:
  • LIVE   — when MT5_LOGIN / MT5_PASSWORD / MT5_SERVER are real credentials
              and the MetaTrader5 package is installed (Windows only).
  • STUB   — on macOS / Linux, or when MetaTrader5 is not installed.
              All tests pass against the built-in stub implementation so the
              full CI pipeline runs cross-platform without a broker connection.

Usage
-----
    # normal pytest run (stub mode on macOS/Linux, live on Windows with MT5)
    pytest tests/test_mt5.py -v

    # force verbose output + show print statements
    pytest tests/test_mt5.py -v -s

    # run only a specific test class
    pytest tests/test_mt5.py::TestConnection -v

Sample .env (copy to project root as .env):
---
MT5_LOGIN=12345678
MT5_PASSWORD=DummyPassword123
MT5_SERVER=ICMarkets-Demo
APP_ENV=development
---
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Generator
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# ── Inject dummy credentials before any module import ────────────────────────
os.environ.setdefault("MT5_LOGIN",    "12345678")
os.environ.setdefault("MT5_PASSWORD", "DummyPassword123")
os.environ.setdefault("MT5_SERVER",   "ICMarkets-Demo")
os.environ.setdefault("APP_ENV",      "development")

from src.mt5.client import (
    MT5Client,
    MT5DataError,
    MT5Error,
    MT5NotConnectedError,
    ConnectionStatus,
    Tick,
    Timeframe,
    XAUUSD_TIMEFRAMES,
)
from src.mt5.history import BarRequest, FetchResult, HistoryManager
from src.mt5.symbol_info import (
    SymbolSpec,
    _stub_symbol_spec,
    clear_symbol_cache,
    format_price,
    get_symbol_spec,
    lot_size_for_risk,
    refresh_symbol_spec,
)


# ═════════════════════════════════════════════════════════════════════════════
# Shared fixtures
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def client() -> Generator[MT5Client, None, None]:
    """
    Module-scoped connected MT5Client.

    Uses real credentials when available (live mode); otherwise operates in
    stub mode.  A single connection is reused across the entire test module
    to avoid repeated terminal handshakes.
    """
    c = MT5Client()
    c.connect()
    yield c
    c.disconnect()


@pytest.fixture(scope="module")
def symbol_spec(client: MT5Client) -> SymbolSpec:
    """XAUUSD SymbolSpec, fetched once for the whole module."""
    clear_symbol_cache()
    return get_symbol_spec(client, "XAUUSD")


@pytest.fixture
def history_mgr(tmp_path) -> HistoryManager:
    """Fresh HistoryManager backed by a per-test temp directory."""
    return HistoryManager(cache_dir=tmp_path / "mt5_history")


# ═════════════════════════════════════════════════════════════════════════════
# TC-01  Connection
# ═════════════════════════════════════════════════════════════════════════════

class TestConnection:
    """TC-01 — Connect / disconnect lifecycle."""

    def test_01_connect_succeeds(self, client: MT5Client):
        """Client must be connected after fixture setup."""
        assert client._connected is True

    def test_02_is_stub_or_live(self, client: MT5Client):
        """
        On macOS/Linux without MetaTrader5, stub_mode must be True.
        On Windows with the package installed, stub_mode must be False.
        """
        try:
            import MetaTrader5  # type: ignore[import]
            expected_stub = False
        except ImportError:
            expected_stub = True
        assert client._stub_mode is expected_stub, (
            f"stub_mode={client._stub_mode!r} but expected {expected_stub!r} "
            f"for this platform"
        )

    def test_03_credentials_loaded_from_env(self):
        """Credentials must be read from environment variables."""
        c = MT5Client()
        assert c._login    == int(os.environ["MT5_LOGIN"])
        assert c._password == os.environ["MT5_PASSWORD"]
        assert c._server   == os.environ["MT5_SERVER"]

    def test_04_context_manager_auto_disconnects(self):
        """Context manager must disconnect on exit even if body raises."""
        with MT5Client() as c:
            assert c._connected is True
        assert c._connected is False

    def test_05_double_connect_is_idempotent(self):
        """Calling connect() twice must not raise."""
        c = MT5Client()
        c.connect()
        c.connect()          # second call is a no-op
        assert c._connected is True
        c.disconnect()

    def test_06_disconnect_when_not_connected_is_safe(self):
        """disconnect() must be safe to call before connect()."""
        c = MT5Client()
        c.disconnect()       # must not raise

    def test_07_raises_without_login_env_var(self, monkeypatch):
        """Missing MT5_LOGIN must raise KeyError immediately.

        We also patch load_env() to a no-op so that the .env file (if present)
        cannot restore the variable that monkeypatch just deleted.
        """
        monkeypatch.delenv("MT5_LOGIN", raising=False)
        monkeypatch.setattr("src.mt5.client.load_env", lambda *a, **kw: None)
        with pytest.raises(KeyError, match="MT5_LOGIN"):
            MT5Client(login=None)

    def test_08_raises_without_password_env_var(self, monkeypatch):
        """Missing MT5_PASSWORD must raise KeyError immediately."""
        monkeypatch.delenv("MT5_PASSWORD", raising=False)
        monkeypatch.setattr("src.mt5.client.load_env", lambda *a, **kw: None)
        with pytest.raises(KeyError, match="MT5_PASSWORD"):
            MT5Client(password=None)

    def test_09_raises_without_server_env_var(self, monkeypatch):
        """Missing MT5_SERVER must raise KeyError immediately."""
        monkeypatch.delenv("MT5_SERVER", raising=False)
        monkeypatch.setattr("src.mt5.client.load_env", lambda *a, **kw: None)
        with pytest.raises(KeyError, match="MT5_SERVER"):
            MT5Client(server=None)

    def test_10_explicit_credentials_override_env(self):
        """Keyword args must take precedence over environment variables."""
        c = MT5Client(login=99999999, password="override", server="Override-Server")
        assert c._login    == 99999999
        assert c._password == "override"
        assert c._server   == "Override-Server"


# ═════════════════════════════════════════════════════════════════════════════
# TC-02  Symbol info
# ═════════════════════════════════════════════════════════════════════════════

class TestSymbolInfo:
    """TC-02 — Fetch XAUUSD symbol specifications."""

    def test_01_returns_symbol_spec_instance(self, symbol_spec: SymbolSpec):
        assert isinstance(symbol_spec, SymbolSpec)

    def test_02_symbol_name_is_xauusd(self, symbol_spec: SymbolSpec):
        assert symbol_spec.name == "XAUUSD"

    # ── Numeric field types ───────────────────────────────────────────────────

    def test_03_digits_is_int(self, symbol_spec: SymbolSpec):
        assert isinstance(symbol_spec.digits, int)

    def test_04_digits_in_valid_range(self, symbol_spec: SymbolSpec):
        """XAUUSD is always quoted to 2 decimal places."""
        assert symbol_spec.digits == 2

    def test_05_spread_is_non_negative_int(self, symbol_spec: SymbolSpec):
        assert isinstance(symbol_spec.spread, int)
        assert symbol_spec.spread >= 0

    def test_06_point_is_positive_float(self, symbol_spec: SymbolSpec):
        assert isinstance(symbol_spec.point, float)
        assert symbol_spec.point > 0

    def test_07_tick_value_is_positive_float(self, symbol_spec: SymbolSpec):
        assert isinstance(symbol_spec.tick_value, float)
        assert symbol_spec.tick_value > 0

    def test_08_contract_size_is_100(self, symbol_spec: SymbolSpec):
        """1 XAU lot = 100 troy ounces."""
        assert symbol_spec.contract_size == 100.0

    # ── Volume constraints ────────────────────────────────────────────────────

    def test_09_volume_min_lte_max(self, symbol_spec: SymbolSpec):
        assert symbol_spec.volume_min <= symbol_spec.volume_max

    def test_10_volume_step_positive(self, symbol_spec: SymbolSpec):
        assert symbol_spec.volume_step > 0

    def test_11_volume_min_gte_step(self, symbol_spec: SymbolSpec):
        assert symbol_spec.volume_min >= symbol_spec.volume_step

    # ── Currency strings ──────────────────────────────────────────────────────

    def test_12_currency_base_is_xau(self, symbol_spec: SymbolSpec):
        assert symbol_spec.currency_base == "XAU"

    def test_13_currency_profit_is_usd(self, symbol_spec: SymbolSpec):
        assert symbol_spec.currency_profit == "USD"

    # ── Derived properties ────────────────────────────────────────────────────

    def test_14_pip_value_equals_10x_tick_value(self, symbol_spec: SymbolSpec):
        assert symbol_spec.pip_value == pytest.approx(symbol_spec.tick_value * 10)

    def test_15_is_tradeable(self, symbol_spec: SymbolSpec):
        assert symbol_spec.is_tradeable is True

    def test_16_spread_cost_is_positive(self, symbol_spec: SymbolSpec):
        assert symbol_spec.spread_cost >= 0

    # ── Caching ───────────────────────────────────────────────────────────────

    def test_17_spec_is_cached_after_first_fetch(self, client: MT5Client):
        clear_symbol_cache()
        s1 = get_symbol_spec(client, "XAUUSD")
        s2 = get_symbol_spec(client, "XAUUSD")
        assert s1 is s2, "Expected same object from cache on second call"

    def test_18_refresh_returns_new_object(self, client: MT5Client):
        s1 = get_symbol_spec(client, "XAUUSD")
        s2 = refresh_symbol_spec(client, "XAUUSD")
        # Values must match even though it's a fresh fetch
        assert s1.name == s2.name
        assert s1.digits == s2.digits

    # ── format_price ──────────────────────────────────────────────────────────

    def test_19_format_price_uses_digits(self, symbol_spec: SymbolSpec):
        result = format_price(2345.6789, symbol_spec)
        assert result == "2345.68"

    def test_20_format_price_returns_string(self, symbol_spec: SymbolSpec):
        assert isinstance(format_price(2000.0, symbol_spec), str)

    # ── lot_size_for_risk ─────────────────────────────────────────────────────

    def test_21_lot_size_basic_calculation(self, symbol_spec: SymbolSpec):
        """$100 risk, 50-point SL, $1 tick value → 2.0 lots."""
        spec = _stub_symbol_spec("XAUUSD")
        lots = lot_size_for_risk(100.0, 50, spec)
        assert lots == pytest.approx(2.0, rel=1e-6)

    def test_22_lot_size_floors_to_step(self, symbol_spec: SymbolSpec):
        """Result must be a multiple of volume_step (floor, not round)."""
        spec = _stub_symbol_spec("XAUUSD")
        lots = lot_size_for_risk(149.85, 50, spec)  # raw = 2.997
        assert lots == pytest.approx(2.99, rel=1e-6)

    def test_23_lot_size_clamps_to_volume_min(self):
        spec = _stub_symbol_spec("XAUUSD")
        lots = lot_size_for_risk(0.001, 100, spec)
        assert lots >= spec.volume_min

    def test_24_lot_size_clamps_to_volume_max(self):
        spec = _stub_symbol_spec("XAUUSD")
        lots = lot_size_for_risk(10_000_000, 1, spec)
        assert lots <= spec.volume_max

    def test_25_lot_size_zero_sl_returns_min(self):
        spec = _stub_symbol_spec("XAUUSD")
        lots = lot_size_for_risk(1_000, 0, spec)
        assert lots == spec.volume_min

    def test_26_lot_size_result_is_float(self):
        spec = _stub_symbol_spec("XAUUSD")
        lots = lot_size_for_risk(500.0, 100, spec)
        assert isinstance(lots, float)


# ═════════════════════════════════════════════════════════════════════════════
# TC-03  Current price (tick)
# ═════════════════════════════════════════════════════════════════════════════

class TestCurrentPrice:
    """TC-03 — Get real-time bid/ask for XAUUSD."""

    def test_01_returns_tick_instance(self, client: MT5Client):
        tick = client.get_tick("XAUUSD")
        assert isinstance(tick, Tick)

    def test_02_symbol_field_matches_request(self, client: MT5Client):
        tick = client.get_tick("XAUUSD")
        assert tick.symbol == "XAUUSD"

    def test_03_bid_is_positive_float(self, client: MT5Client):
        tick = client.get_tick("XAUUSD")
        assert isinstance(tick.bid, float)
        assert tick.bid > 0

    def test_04_ask_is_positive_float(self, client: MT5Client):
        tick = client.get_tick("XAUUSD")
        assert isinstance(tick.ask, float)
        assert tick.ask > 0

    def test_05_ask_gt_bid(self, client: MT5Client):
        """Ask must always be strictly greater than bid (no negative spread)."""
        tick = client.get_tick("XAUUSD")
        assert tick.ask > tick.bid

    def test_06_spread_equals_ask_minus_bid(self, client: MT5Client):
        tick = client.get_tick("XAUUSD")
        assert tick.spread_pts == pytest.approx(tick.ask - tick.bid, rel=1e-5)

    def test_07_spread_is_non_negative(self, client: MT5Client):
        tick = client.get_tick("XAUUSD")
        assert tick.spread_pts >= 0

    def test_08_time_is_utc_timestamp(self, client: MT5Client):
        tick = client.get_tick("XAUUSD")
        assert isinstance(tick.time, pd.Timestamp)
        assert tick.time.tzinfo is not None, "Tick.time must be timezone-aware"

    def test_09_tick_time_is_recent(self, client: MT5Client):
        """Tick timestamp must be within the last 24 hours (stub uses utcnow)."""
        tick = client.get_tick("XAUUSD")
        age  = pd.Timestamp.utcnow() - tick.time.tz_convert("UTC")
        assert age.total_seconds() < 86_400, f"Tick is {age} old — stale or wrong tz"

    def test_10_bid_in_plausible_xauusd_range(self, client: MT5Client):
        """Gold bid price should be between $1,000 and $5,000 per ounce."""
        tick = client.get_tick("XAUUSD")
        assert 1_000 < tick.bid < 5_000, f"Suspicious bid: {tick.bid}"

    def test_11_raises_when_not_connected(self):
        """get_tick() on an unconnected client must raise."""
        c = MT5Client()
        with pytest.raises(MT5NotConnectedError):
            c.get_tick("XAUUSD")

    def test_12_tick_is_immutable(self, client: MT5Client):
        """Tick is a frozen dataclass — attribute assignment must raise."""
        tick = client.get_tick("XAUUSD")
        with pytest.raises((AttributeError, TypeError)):
            tick.bid = 9999.0       # type: ignore[misc]

    def test_13_successive_ticks_are_independent(self, client: MT5Client):
        """Two successive calls must return distinct objects."""
        t1 = client.get_tick("XAUUSD")
        t2 = client.get_tick("XAUUSD")
        assert t1 is not t2


# ═════════════════════════════════════════════════════════════════════════════
# TC-04  OHLC — last 100 M15 candles
# ═════════════════════════════════════════════════════════════════════════════

class TestOHLC:
    """TC-04 — Fetch last 100 candles on M15 and validate structure."""

    @pytest.fixture(scope="class")
    def m15_df(self, client: MT5Client) -> pd.DataFrame:
        """Fetch once, reuse across all tests in this class."""
        return client.get_ohlc("XAUUSD", Timeframe.M15, count=100)

    # ── Shape ────────────────────────────────────────────────────────────────

    def test_01_returns_dataframe(self, m15_df: pd.DataFrame):
        assert isinstance(m15_df, pd.DataFrame)

    def test_02_exactly_100_rows(self, m15_df: pd.DataFrame):
        assert len(m15_df) == 100, f"Expected 100 rows, got {len(m15_df)}"

    def test_03_required_columns_present(self, m15_df: pd.DataFrame):
        required = {"open", "high", "low", "close", "tick_volume"}
        missing  = required - set(m15_df.columns)
        assert not missing, f"Missing columns: {missing}"

    # ── Index ────────────────────────────────────────────────────────────────

    def test_04_index_name_is_time(self, m15_df: pd.DataFrame):
        assert m15_df.index.name == "time"

    def test_05_index_is_datetime(self, m15_df: pd.DataFrame):
        assert isinstance(m15_df.index, pd.DatetimeIndex)

    def test_06_index_is_utc_aware(self, m15_df: pd.DataFrame):
        assert m15_df.index.tz is not None, "Index must be timezone-aware (UTC)"
        assert str(m15_df.index.tz) == "UTC"

    def test_07_index_is_sorted_ascending(self, m15_df: pd.DataFrame):
        assert m15_df.index.is_monotonic_increasing

    def test_08_no_duplicate_timestamps(self, m15_df: pd.DataFrame):
        dupes = m15_df.index.duplicated().sum()
        assert dupes == 0, f"{dupes} duplicate timestamps found"

    # ── OHLC price integrity ──────────────────────────────────────────────────

    def test_09_high_gte_open_and_close(self, m15_df: pd.DataFrame):
        """high must be >= both open AND close (the true OHLC invariant).

        Note: high >= open alone is NOT guaranteed — open is the prior close
        carried forward and can exceed the high of the new bar in noisy stub
        data (and theoretically in real data at gap opens).  The invariant
        high >= max(open, close) holds by definition.
        """
        assert (m15_df["high"] >= m15_df[["open", "close"]].max(axis=1)).all()

    def test_10_high_gte_close(self, m15_df: pd.DataFrame):
        assert (m15_df["high"] >= m15_df["close"]).all()

    def test_11_low_lte_open_and_close(self, m15_df: pd.DataFrame):
        """low must be <= both open AND close.

        Same reasoning: low <= open is not guaranteed at gap-down opens.
        The real invariant is low <= min(open, close).
        """
        assert (m15_df["low"] <= m15_df[["open", "close"]].min(axis=1)).all()

    def test_12_low_lte_close(self, m15_df: pd.DataFrame):
        assert (m15_df["low"] <= m15_df["close"]).all()

    def test_13_high_gte_low(self, m15_df: pd.DataFrame):
        assert (m15_df["high"] >= m15_df["low"]).all()

    def test_14_no_zero_prices(self, m15_df: pd.DataFrame):
        for col in ("open", "high", "low", "close"):
            zeros = (m15_df[col] == 0).sum()
            assert zeros == 0, f"{col} has {zeros} zero values"

    def test_15_no_negative_prices(self, m15_df: pd.DataFrame):
        for col in ("open", "high", "low", "close"):
            negs = (m15_df[col] < 0).sum()
            assert negs == 0, f"{col} has {negs} negative values"

    def test_16_prices_in_plausible_xauusd_range(self, m15_df: pd.DataFrame):
        assert m15_df["low"].min()  > 1_000, "Gold price below $1,000 — suspicious"
        assert m15_df["high"].max() < 5_000, "Gold price above $5,000 — suspicious"

    def test_17_tick_volume_non_negative(self, m15_df: pd.DataFrame):
        assert (m15_df["tick_volume"] >= 0).all()

    # ── Data types ────────────────────────────────────────────────────────────

    def test_18_ohlc_columns_are_float64(self, m15_df: pd.DataFrame):
        for col in ("open", "high", "low", "close"):
            assert m15_df[col].dtype == "float64", f"{col} dtype={m15_df[col].dtype}"

    def test_19_tick_volume_is_numeric(self, m15_df: pd.DataFrame):
        assert pd.api.types.is_numeric_dtype(m15_df["tick_volume"])

    def test_20_no_null_values(self, m15_df: pd.DataFrame):
        nulls = m15_df[["open", "high", "low", "close", "tick_volume"]].isnull().sum()
        assert nulls.sum() == 0, f"Null values found:\n{nulls[nulls > 0]}"

    # ── All supported timeframes ──────────────────────────────────────────────

    @pytest.mark.parametrize("tf", XAUUSD_TIMEFRAMES)
    def test_21_all_xauusd_timeframes_return_data(
        self, client: MT5Client, tf: Timeframe
    ):
        df = client.get_ohlc("XAUUSD", tf, count=50)
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 50
        assert "close" in df.columns

    def test_22_raises_when_not_connected(self):
        c = MT5Client()
        with pytest.raises(MT5NotConnectedError):
            c.get_ohlc("XAUUSD", Timeframe.M15, count=100)

    def test_23_multi_timeframe_returns_all_keys(self, client: MT5Client):
        result = client.get_ohlc_multi_timeframe("XAUUSD", count=20)
        assert set(result.keys()) == set(XAUUSD_TIMEFRAMES)
        for tf, df in result.items():
            assert len(df) == 20, f"{tf.value}: expected 20 rows, got {len(df)}"


# ═════════════════════════════════════════════════════════════════════════════
# TC-05  Reconnection
# ═════════════════════════════════════════════════════════════════════════════

class TestReconnection:
    """TC-05 — Simulate disconnect and verify automatic / manual recovery."""

    def test_01_manual_reconnect_restores_connection(self):
        """reconnect() must leave the client in a connected state."""
        with MT5Client() as c:
            assert c._connected is True
            c.reconnect()
            assert c._connected is True

    def test_02_operations_succeed_after_reconnect(self):
        """get_tick() and get_ohlc() must work normally after reconnect()."""
        with MT5Client() as c:
            c.reconnect()
            tick = c.get_tick("XAUUSD")
            df   = c.get_ohlc("XAUUSD", Timeframe.M15, count=10)
            assert isinstance(tick, Tick)
            assert len(df) == 10

    def test_03_simulated_disconnect_then_reconnect(self):
        """
        Force _connected = False (simulates a dropped socket) and verify
        that calling reconnect() repairs the session.
        """
        with MT5Client() as c:
            # Simulate an unexpected drop
            c._connected = False
            assert c._connected is False

            # Manual reconnect
            c.reconnect()
            assert c._connected is True

    def test_04_raises_when_operating_after_forced_disconnect(self):
        """
        After a forced disconnect (without reconnect), all data methods
        must raise MT5NotConnectedError.
        """
        c = MT5Client()
        c.connect()
        c._connected = False   # simulate drop without calling reconnect()

        with pytest.raises(MT5NotConnectedError):
            c.get_tick("XAUUSD")

        with pytest.raises(MT5NotConnectedError):
            c.get_ohlc("XAUUSD", Timeframe.M15, count=5)

        with pytest.raises(MT5NotConnectedError):
            c.health_check()

    def test_05_exponential_backoff_timing(self, monkeypatch):
        """
        Back-off waits must grow exponentially and honour _BACKOFF_MAX.
        We monkeypatch sleep to capture wait durations without real delays.
        """
        import src.mt5.client as client_mod

        sleep_calls: list[float] = []
        monkeypatch.setattr(client_mod, "time", MagicMock(sleep=lambda s: sleep_calls.append(s)))
        monkeypatch.setattr(client_mod, "_BACKOFF_BASE", 2.0)
        monkeypatch.setattr(client_mod, "_BACKOFF_MAX",  8.0)

        attempt_count = 0

        def flaky(self_inner):
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 4:
                raise MT5Error("simulated", code=999)

        monkeypatch.setattr(MT5Client, "_do_connect", flaky)

        c = MT5Client()
        c.connect()  # succeeds on attempt 4

        assert attempt_count == 4
        # Waits: 2^1=2, 2^2=4, 2^3=8 (capped at 8)
        assert sleep_calls == [2.0, 4.0, 8.0]

    def test_06_gives_up_after_max_retries(self, monkeypatch):
        """After _MAX_RETRIES failures, MT5Error must be raised."""
        import src.mt5.client as client_mod
        monkeypatch.setattr(client_mod, "time", MagicMock(sleep=lambda _: None))

        monkeypatch.setattr(
            MT5Client, "_do_connect",
            lambda self_inner: (_ for _ in ()).throw(MT5Error("always fails", code=5)),
        )

        c = MT5Client()
        with pytest.raises(MT5Error):
            c.connect()

    def test_07_health_check_reconnects_on_stale_session(self, monkeypatch):
        """
        health_check() should attempt one silent reconnect when account_info()
        returns None (stale connection), and then report the recovered status.
        """
        c = MT5Client()
        c.connect()

        if c._stub_mode:
            # Stub always returns OK — just verify the happy path
            status = c.health_check()
            assert status.connected is True
            return

        # Live path: monkey-patch mt5.account_info to fail once then succeed
        import MetaTrader5 as mt5_lib  # type: ignore[import]
        call_count = [0]
        original   = mt5_lib.account_info

        def flaky_account_info():
            call_count[0] += 1
            if call_count[0] == 1:
                return None   # simulate stale session
            return original()

        monkeypatch.setattr(mt5_lib, "account_info", flaky_account_info)
        monkeypatch.setattr(mt5_lib, "last_error", lambda: (0, "OK"))

        status = c.health_check()
        assert call_count[0] >= 2   # at least one retry happened
        c.disconnect()


# ═════════════════════════════════════════════════════════════════════════════
# TC-06  Health check
# ═════════════════════════════════════════════════════════════════════════════

class TestHealthCheck:
    """TC-06 — Connection health-check method."""

    def test_01_returns_connection_status_instance(self, client: MT5Client):
        status = client.health_check()
        assert isinstance(status, ConnectionStatus)

    def test_02_connected_is_true(self, client: MT5Client):
        status = client.health_check()
        assert status.connected is True

    def test_03_account_login_matches_env(self, client: MT5Client):
        status = client.health_check()
        assert status.account_login == int(os.environ["MT5_LOGIN"])

    def test_04_account_balance_non_negative(self, client: MT5Client):
        status = client.health_check()
        assert status.account_balance >= 0

    def test_05_account_equity_non_negative(self, client: MT5Client):
        status = client.health_check()
        assert status.account_equity >= 0

    def test_06_server_name_is_string(self, client: MT5Client):
        status = client.health_check()
        assert isinstance(status.server, str)
        assert len(status.server) > 0

    def test_07_error_code_is_zero_on_success(self, client: MT5Client):
        status = client.health_check()
        assert status.error_code == 0

    def test_08_ping_ms_is_non_negative(self, client: MT5Client):
        status = client.health_check()
        assert status.ping_ms >= 0

    def test_09_raises_when_not_connected(self):
        c = MT5Client()
        with pytest.raises(MT5NotConnectedError):
            c.health_check()

    def test_10_status_is_immutable(self, client: MT5Client):
        """ConnectionStatus is a frozen dataclass."""
        status = client.health_check()
        with pytest.raises((AttributeError, TypeError)):
            status.connected = False   # type: ignore[misc]


# ═════════════════════════════════════════════════════════════════════════════
# TC-07  Historical data (HistoryManager)
# ═════════════════════════════════════════════════════════════════════════════

class TestHistoricalData:
    """TC-07 — Fetch, cache, and validate historical bar data."""

    def test_01_fetch_returns_fetch_result(
        self, client: MT5Client, history_mgr: HistoryManager
    ):
        req    = BarRequest("XAUUSD", Timeframe.M15, bars=100)
        result = history_mgr.fetch(client, req)
        assert isinstance(result, FetchResult)

    def test_02_data_is_dataframe(
        self, client: MT5Client, history_mgr: HistoryManager
    ):
        req    = BarRequest("XAUUSD", Timeframe.M15, bars=100)
        result = history_mgr.fetch(client, req)
        assert isinstance(result.data, pd.DataFrame)

    def test_03_100_bars_returned(
        self, client: MT5Client, history_mgr: HistoryManager
    ):
        req    = BarRequest("XAUUSD", Timeframe.M15, bars=100)
        result = history_mgr.fetch(client, req)
        assert result.total_bars == 100

    def test_04_cache_file_is_created(
        self, client: MT5Client, history_mgr: HistoryManager
    ):
        req    = BarRequest("XAUUSD", Timeframe.H1, bars=50)
        result = history_mgr.fetch(client, req)
        assert result.cache_path is not None
        assert result.cache_path.exists()

    def test_05_second_fetch_reads_from_cache(
        self, client: MT5Client, history_mgr: HistoryManager
    ):
        req = BarRequest("XAUUSD", Timeframe.H1, bars=50)
        history_mgr.fetch(client, req)   # prime
        r2 = history_mgr.fetch(client, req)
        # New bars should be ≤ the initial count (cache covered most)
        assert r2.new_bars <= 50

    def test_06_force_refresh_bypasses_cache(
        self, client: MT5Client, history_mgr: HistoryManager
    ):
        req_warm    = BarRequest("XAUUSD", Timeframe.H4, bars=30)
        req_refresh = BarRequest("XAUUSD", Timeframe.H4, bars=30, force_refresh=True)
        history_mgr.fetch(client, req_warm)
        result = history_mgr.fetch(client, req_refresh)
        assert result.new_bars > 0

    def test_07_from_date_filters_results(
        self, client: MT5Client, history_mgr: HistoryManager
    ):
        from_dt = datetime(2024, 1, 1, tzinfo=timezone.utc)
        req     = BarRequest("XAUUSD", Timeframe.H1, bars=500, from_date=from_dt)
        result  = history_mgr.fetch(client, req)
        assert (result.data.index >= pd.Timestamp(from_dt)).all()

    def test_08_invalidate_removes_cache(
        self, client: MT5Client, history_mgr: HistoryManager
    ):
        req = BarRequest("XAUUSD", Timeframe.H4, bars=20)
        history_mgr.fetch(client, req)
        removed = history_mgr.invalidate("XAUUSD", Timeframe.H4)
        assert removed is True

    def test_09_invalidate_nonexistent_returns_false(
        self, history_mgr: HistoryManager
    ):
        removed = history_mgr.invalidate("XAUUSD", Timeframe.W1)
        assert removed is False

    def test_10_invalidate_all_clears_everything(
        self, client: MT5Client, history_mgr: HistoryManager
    ):
        for tf in (Timeframe.M5, Timeframe.M15, Timeframe.H1):
            history_mgr.fetch(client, BarRequest("XAUUSD", tf, 20))
        count = history_mgr.invalidate_all()
        assert count >= 3

    def test_11_cache_info_returns_list(
        self, client: MT5Client, history_mgr: HistoryManager
    ):
        history_mgr.fetch(client, BarRequest("XAUUSD", Timeframe.H1, 20))
        info = history_mgr.cache_info()
        assert isinstance(info, list)
        assert len(info) >= 1

    def test_12_cache_info_fields_present(
        self, client: MT5Client, history_mgr: HistoryManager
    ):
        history_mgr.fetch(client, BarRequest("XAUUSD", Timeframe.H1, 20))
        entry = history_mgr.cache_info()[0]
        for key in ("symbol", "timeframe", "bars", "from", "to", "size_kb", "path"):
            assert key in entry, f"Missing key: {key!r}"

    def test_13_data_index_is_utc(
        self, client: MT5Client, history_mgr: HistoryManager
    ):
        req    = BarRequest("XAUUSD", Timeframe.M15, bars=30)
        result = history_mgr.fetch(client, req)
        assert result.data.index.tz is not None
        assert str(result.data.index.tz) == "UTC"

    def test_14_data_is_sorted_ascending(
        self, client: MT5Client, history_mgr: HistoryManager
    ):
        req    = BarRequest("XAUUSD", Timeframe.M15, bars=30)
        result = history_mgr.fetch(client, req)
        assert result.data.index.is_monotonic_increasing


# ═════════════════════════════════════════════════════════════════════════════
# TC-08  Data type & formatting validation (cross-cutting)
# ═════════════════════════════════════════════════════════════════════════════

class TestDataTypesAndFormatting:
    """TC-08 — Verify Python types, pandas dtypes, and string formatting."""

    def test_01_tick_bid_ask_are_python_floats(self, client: MT5Client):
        tick = client.get_tick("XAUUSD")
        assert type(tick.bid) is float
        assert type(tick.ask) is float

    def test_02_tick_spread_pts_is_python_float(self, client: MT5Client):
        tick = client.get_tick("XAUUSD")
        assert type(tick.spread_pts) is float

    def test_03_tick_time_tz_is_utc(self, client: MT5Client):
        tick = client.get_tick("XAUUSD")
        assert str(tick.time.tz) == "UTC"

    def test_04_ohlc_float_columns_are_float64(self, client: MT5Client):
        df = client.get_ohlc("XAUUSD", Timeframe.M15, count=10)
        for col in ("open", "high", "low", "close"):
            assert df[col].dtype == "float64", f"{col}: {df[col].dtype}"

    def test_05_tick_volume_is_numeric(self, client: MT5Client):
        df = client.get_ohlc("XAUUSD", Timeframe.M15, count=10)
        assert pd.api.types.is_numeric_dtype(df["tick_volume"])

    def test_06_symbol_spec_string_fields_are_str(self, symbol_spec: SymbolSpec):
        for field in ("name", "currency_base", "currency_profit", "currency_margin"):
            val = getattr(symbol_spec, field)
            assert isinstance(val, str), f"{field} should be str, got {type(val)}"

    def test_07_symbol_spec_int_fields_are_int(self, symbol_spec: SymbolSpec):
        for field in ("digits", "spread", "trade_mode"):
            val = getattr(symbol_spec, field)
            assert isinstance(val, int), f"{field} should be int, got {type(val)}"

    def test_08_symbol_spec_float_fields_are_float(self, symbol_spec: SymbolSpec):
        for field in ("point", "tick_size", "tick_value", "contract_size",
                      "volume_min", "volume_max", "volume_step"):
            val = getattr(symbol_spec, field)
            assert isinstance(val, float), f"{field} should be float, got {type(val)}"

    def test_09_format_price_decimal_places_match_digits(self, symbol_spec: SymbolSpec):
        """Formatted price must have exactly `digits` decimal places."""
        formatted = format_price(2345.6789, symbol_spec)
        _, decimals = formatted.split(".")
        assert len(decimals) == symbol_spec.digits

    def test_10_fetch_result_metadata_types(
        self, client: MT5Client, history_mgr: HistoryManager
    ):
        req    = BarRequest("XAUUSD", Timeframe.H1, bars=20)
        result = history_mgr.fetch(client, req)
        assert isinstance(result.symbol,     str)
        assert isinstance(result.timeframe,  Timeframe)
        assert isinstance(result.total_bars, int)
        assert isinstance(result.new_bars,   int)
        assert isinstance(result.from_cache, bool)

    def test_11_timeframe_enum_value_is_string(self):
        for tf in Timeframe:
            assert isinstance(tf.value, str)

    def test_12_timeframe_mt5_constant_is_int(self):
        for tf in Timeframe:
            assert isinstance(tf.mt5_constant, int)

    def test_13_connection_status_types(self, client: MT5Client):
        s = client.health_check()
        assert isinstance(s.connected,        bool)
        assert isinstance(s.account_login,    int)
        assert isinstance(s.account_balance,  float)
        assert isinstance(s.account_equity,   float)
        assert isinstance(s.server,           str)
        assert isinstance(s.ping_ms,          float)
        assert isinstance(s.error_code,       int)