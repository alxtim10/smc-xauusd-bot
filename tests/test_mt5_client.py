"""
tests/test_mt5_client.py
────────────────────────
Unit tests for src/mt5/client.py, symbol_info.py, and history.py.

All tests run fully offline (no MT5 terminal required).  The MT5Client
operates in stub mode when the MetaTrader5 package is absent, which is the
case on macOS / Linux CI.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# ── Ensure env vars exist before importing the module ────────────────────────
os.environ.setdefault("MT5_LOGIN",    "12345678")
os.environ.setdefault("MT5_PASSWORD", "test_password")
os.environ.setdefault("MT5_SERVER",   "TestBroker-Demo")

from src.mt5.client import (
    MT5Client,
    MT5Error,
    MT5NotConnectedError,
    Tick,
    Timeframe,
    ConnectionStatus,
    XAUUSD_TIMEFRAMES,
)
from src.mt5.symbol_info import (
    SymbolSpec,
    _stub_symbol_spec,
    clear_symbol_cache,
    format_price,
    get_symbol_spec,
    lot_size_for_risk,
)
from src.mt5.history import BarRequest, FetchResult, HistoryManager, _merge


# ═════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def stub_client() -> MT5Client:
    """Return a connected stub-mode MT5Client (no real MT5 needed)."""
    client = MT5Client()
    client.connect()
    yield client
    client.disconnect()


@pytest.fixture
def history_mgr(tmp_path: Path) -> HistoryManager:
    """Return a HistoryManager backed by a temp directory."""
    return HistoryManager(cache_dir=tmp_path / "history")


# ═════════════════════════════════════════════════════════════════════════════
# Timeframe
# ═════════════════════════════════════════════════════════════════════════════

class TestTimeframe:
    def test_members_exist(self):
        for tf in (Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.H4):
            assert isinstance(tf, Timeframe)

    def test_mt5_constant_returns_int_in_stub_mode(self):
        """mt5_constant must return an int even when MetaTrader5 is absent."""
        for tf in Timeframe:
            assert isinstance(tf.mt5_constant, int)

    def test_xauusd_timeframes_tuple(self):
        assert Timeframe.M5  in XAUUSD_TIMEFRAMES
        assert Timeframe.M15 in XAUUSD_TIMEFRAMES
        assert Timeframe.H1  in XAUUSD_TIMEFRAMES
        assert Timeframe.H4  in XAUUSD_TIMEFRAMES


# ═════════════════════════════════════════════════════════════════════════════
# MT5Client — connection
# ═════════════════════════════════════════════════════════════════════════════

class TestMT5ClientConnection:
    def test_connect_in_stub_mode(self):
        client = MT5Client()
        client.connect()
        assert client._connected is True
        client.disconnect()

    def test_context_manager(self):
        with MT5Client() as client:
            assert client._connected is True
        assert client._connected is False

    def test_double_connect_is_idempotent(self):
        client = MT5Client()
        client.connect()
        client.connect()          # should not raise
        assert client._connected is True
        client.disconnect()

    def test_disconnect_when_not_connected_is_safe(self):
        client = MT5Client()
        client.disconnect()       # no error expected

    def test_reconnect(self, stub_client: MT5Client):
        stub_client.reconnect()
        assert stub_client._connected is True

    def test_raises_when_mt5_login_missing(self, monkeypatch):
        monkeypatch.delenv("MT5_LOGIN", raising=False)
        with pytest.raises(KeyError, match="MT5_LOGIN"):
            MT5Client(login=None)

    def test_exponential_backoff_on_failure(self, monkeypatch):
        """_do_connect raises MT5Error 3 times then succeeds; check back-off."""
        calls = []

        original_do_connect = MT5Client._do_connect

        def flaky_connect(self_inner):
            calls.append(time.monotonic())
            if len(calls) < 3:
                raise MT5Error("simulated failure", code=999)
            # 3rd call succeeds
            self_inner._connected = False  # let the outer loop set it

        client = MT5Client()
        monkeypatch.setattr(MT5Client, "_do_connect", flaky_connect)
        monkeypatch.setattr("src.mt5.client._BACKOFF_BASE", 0.05)  # speed up
        monkeypatch.setattr("src.mt5.client._BACKOFF_MAX",  1.0)

        client.connect()
        assert len(calls) == 3

    def test_raises_after_max_retries(self, monkeypatch):
        def always_fail(self_inner):
            raise MT5Error("always fails", code=5)

        client = MT5Client()
        monkeypatch.setattr(MT5Client, "_do_connect", always_fail)
        monkeypatch.setattr("src.mt5.client._BACKOFF_BASE", 0.01)
        monkeypatch.setattr("src.mt5.client._BACKOFF_MAX",  0.05)

        with pytest.raises(MT5Error):
            client.connect()


# ═════════════════════════════════════════════════════════════════════════════
# MT5Client — health check
# ═════════════════════════════════════════════════════════════════════════════

class TestHealthCheck:
    def test_health_check_stub_returns_connected(self, stub_client: MT5Client):
        status = stub_client.health_check()
        assert isinstance(status, ConnectionStatus)
        assert status.connected is True
        assert status.account_login == stub_client._login
        assert status.error_code == 0

    def test_health_check_raises_when_not_connected(self):
        client = MT5Client()
        with pytest.raises(MT5NotConnectedError):
            client.health_check()


# ═════════════════════════════════════════════════════════════════════════════
# MT5Client — tick prices
# ═════════════════════════════════════════════════════════════════════════════

class TestGetTick:
    def test_returns_tick_dataclass(self, stub_client: MT5Client):
        tick = stub_client.get_tick("XAUUSD")
        assert isinstance(tick, Tick)
        assert tick.symbol == "XAUUSD"
        assert tick.bid > 0
        assert tick.ask > tick.bid
        assert tick.spread_pts > 0
        assert isinstance(tick.time, pd.Timestamp)

    def test_raises_when_not_connected(self):
        client = MT5Client()
        with pytest.raises(MT5NotConnectedError):
            client.get_tick()


# ═════════════════════════════════════════════════════════════════════════════
# MT5Client — OHLC
# ═════════════════════════════════════════════════════════════════════════════

class TestGetOHLC:
    @pytest.mark.parametrize("tf", XAUUSD_TIMEFRAMES)
    def test_returns_dataframe_for_all_timeframes(
        self, stub_client: MT5Client, tf: Timeframe
    ):
        df = stub_client.get_ohlc("XAUUSD", tf, count=50)
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 50
        for col in ("open", "high", "low", "close", "tick_volume"):
            assert col in df.columns
        assert df.index.tz is not None  # UTC-aware

    def test_high_gte_close_gte_low(self, stub_client: MT5Client):
        df = stub_client.get_ohlc("XAUUSD", Timeframe.H1, count=100)
        assert (df["high"] >= df["close"]).all()
        assert (df["close"] >= df["low"]).all()

    def test_multi_timeframe(self, stub_client: MT5Client):
        result = stub_client.get_ohlc_multi_timeframe("XAUUSD", count=30)
        assert set(result.keys()) == set(XAUUSD_TIMEFRAMES)
        for tf, df in result.items():
            assert len(df) == 30

    def test_raises_when_not_connected(self):
        client = MT5Client()
        with pytest.raises(MT5NotConnectedError):
            client.get_ohlc()


# ═════════════════════════════════════════════════════════════════════════════
# SymbolSpec
# ═════════════════════════════════════════════════════════════════════════════

class TestSymbolSpec:
    def test_stub_spec_has_correct_types(self):
        spec = _stub_symbol_spec("XAUUSD")
        assert spec.name == "XAUUSD"
        assert isinstance(spec.digits, int)
        assert isinstance(spec.tick_value, float)
        assert spec.contract_size == 100.0

    def test_is_tradeable(self):
        spec = _stub_symbol_spec("XAUUSD")
        assert spec.is_tradeable is True

    def test_pip_value(self):
        spec = _stub_symbol_spec("XAUUSD")
        assert spec.pip_value == spec.tick_value * 10

    def test_get_symbol_spec_caches(self, stub_client: MT5Client):
        clear_symbol_cache()
        s1 = get_symbol_spec(stub_client, "XAUUSD")
        s2 = get_symbol_spec(stub_client, "XAUUSD")
        assert s1 is s2  # same object → cache hit

    def test_format_price(self):
        spec = _stub_symbol_spec("XAUUSD")   # digits=2
        assert format_price(2345.6789, spec) == "2345.68"

    def test_lot_size_for_risk_basic(self):
        spec = _stub_symbol_spec("XAUUSD")   # tick_value=1.0, step=0.01
        lots = lot_size_for_risk(
            risk_amount=100.0,
            stop_loss_pts=50,
            spec=spec,
        )
        # raw = 100 / (50 * 1.0) = exactly 2.0 lots after Decimal rounding
        assert lots == pytest.approx(2.0, rel=1e-6)

    def test_lot_size_for_risk_fractional(self):
        """Verify floor (not round) rounding: 2.997 → 2.99 not 3.00."""
        spec = _stub_symbol_spec("XAUUSD")   # step=0.01
        lots = lot_size_for_risk(
            risk_amount=149.85,              # raw = 149.85 / (50*1.0) = 2.997
            stop_loss_pts=50,
            spec=spec,
        )
        assert lots == pytest.approx(2.99, rel=1e-6)

    def test_lot_size_clamped_to_min(self):
        spec = _stub_symbol_spec("XAUUSD")
        lots = lot_size_for_risk(risk_amount=0.001, stop_loss_pts=100, spec=spec)
        assert lots >= spec.volume_min

    def test_lot_size_zero_stop_loss_returns_min(self):
        spec = _stub_symbol_spec("XAUUSD")
        lots = lot_size_for_risk(risk_amount=1_000, stop_loss_pts=0, spec=spec)
        assert lots == spec.volume_min


# ═════════════════════════════════════════════════════════════════════════════
# HistoryManager
# ═════════════════════════════════════════════════════════════════════════════

class TestHistoryManager:
    def test_fetch_returns_fetch_result(
        self, stub_client: MT5Client, history_mgr: HistoryManager
    ):
        req    = BarRequest(symbol="XAUUSD", timeframe=Timeframe.H1, bars=100)
        result = history_mgr.fetch(stub_client, req)
        assert isinstance(result, FetchResult)
        assert result.total_bars == 100
        assert isinstance(result.data, pd.DataFrame)

    def test_dataframe_index_is_utc(
        self, stub_client: MT5Client, history_mgr: HistoryManager
    ):
        req = BarRequest(symbol="XAUUSD", timeframe=Timeframe.M15, bars=50)
        result = history_mgr.fetch(stub_client, req)
        assert result.data.index.tz is not None

    def test_second_fetch_uses_cache(
        self, stub_client: MT5Client, history_mgr: HistoryManager
    ):
        req = BarRequest(symbol="XAUUSD", timeframe=Timeframe.H1, bars=80)
        r1  = history_mgr.fetch(stub_client, req)
        r2  = history_mgr.fetch(stub_client, req)
        # Second fetch should report fewer new bars (cache covers most)
        assert r2.new_bars <= r1.new_bars

    def test_force_refresh_ignores_cache(
        self, stub_client: MT5Client, history_mgr: HistoryManager
    ):
        req_cached  = BarRequest("XAUUSD", Timeframe.H1, bars=50)
        req_refresh = BarRequest("XAUUSD", Timeframe.H1, bars=50, force_refresh=True)
        history_mgr.fetch(stub_client, req_cached)
        result = history_mgr.fetch(stub_client, req_refresh)
        assert result.new_bars > 0

    def test_invalidate_removes_cache_file(
        self, stub_client: MT5Client, history_mgr: HistoryManager
    ):
        req = BarRequest("XAUUSD", Timeframe.H4, bars=30)
        history_mgr.fetch(stub_client, req)
        removed = history_mgr.invalidate("XAUUSD", Timeframe.H4)
        assert removed is True
        # Second invalidate → nothing to remove
        assert history_mgr.invalidate("XAUUSD", Timeframe.H4) is False

    def test_invalidate_all(
        self, stub_client: MT5Client, history_mgr: HistoryManager
    ):
        for tf in (Timeframe.H1, Timeframe.M5):
            history_mgr.fetch(stub_client, BarRequest("XAUUSD", tf, 20))
        count = history_mgr.invalidate_all()
        assert count >= 2

    def test_from_date_filter(
        self, stub_client: MT5Client, history_mgr: HistoryManager
    ):
        from_dt = datetime(2024, 1, 1, tzinfo=timezone.utc)
        req     = BarRequest("XAUUSD", Timeframe.H1, bars=500, from_date=from_dt)
        result  = history_mgr.fetch(stub_client, req)
        assert (result.data.index >= pd.Timestamp(from_dt)).all()


# ═════════════════════════════════════════════════════════════════════════════
# _merge helper
# ═════════════════════════════════════════════════════════════════════════════

class TestMerge:
    def _make_df(self, start: str, periods: int) -> pd.DataFrame:
        idx = pd.date_range(start, periods=periods, freq="1h", tz="UTC")
        return pd.DataFrame({"close": range(periods)}, index=idx)

    def test_merge_none_cached_returns_new(self):
        new = self._make_df("2024-01-01", 5)
        merged = _merge(None, new)
        assert len(merged) == 5

    def test_merge_empty_new_returns_cached(self):
        cached = self._make_df("2024-01-01", 5)
        new    = pd.DataFrame()
        merged = _merge(cached, new)
        assert len(merged) == 5

    def test_merge_deduplicates(self):
        a = self._make_df("2024-01-01", 5)
        b = self._make_df("2024-01-01", 5)   # same timestamps
        merged = _merge(a, b)
        assert len(merged) == 5

    def test_merge_extends_history(self):
        a = self._make_df("2024-01-01", 10)
        b = self._make_df("2024-01-10", 5)   # new tail
        merged = _merge(a, b)
        assert len(merged) == 15
        assert merged.index.is_monotonic_increasing