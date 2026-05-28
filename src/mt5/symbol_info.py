"""
src/mt5/symbol_info.py
──────────────────────
Retrieve and cache symbol specifications from MetaTrader 5.

Provides:
  • SymbolSpec  – frozen dataclass holding every useful symbol attribute
  • get_symbol_spec()  – fetch-once, cache-forever helper
  • refresh_symbol_spec()  – force cache invalidation
  • format_price()  – round a price to the symbol's digit precision

Usage
-----
    from src.mt5.symbol_info import get_symbol_spec

    spec = get_symbol_spec(client, "XAUUSD")
    print(spec.digits, spec.spread, spec.tick_value)
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

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


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SymbolSpec:
    """
    Immutable snapshot of a symbol's trading specifications.

    Attributes
    ----------
    name:
        Symbol name, e.g. ``"XAUUSD"``.
    digits:
        Decimal places for price display (e.g. 2 for XAUUSD).
    spread:
        Current spread in points.
    point:
        Minimum price change (e.g. 0.01 for XAUUSD).
    tick_size:
        Minimum price change in price units (often equal to point).
    tick_value:
        Monetary value of one tick move for 1 standard lot.
    contract_size:
        Number of base-currency units per 1 lot (e.g. 100 for XAUUSD).
    volume_min:
        Minimum lot size allowed.
    volume_max:
        Maximum lot size allowed.
    volume_step:
        Lot-size increment granularity.
    currency_base:
        Base currency of the symbol (e.g. ``"XAU"``).
    currency_profit:
        Profit currency (e.g. ``"USD"``).
    currency_margin:
        Margin currency (e.g. ``"USD"``).
    trade_mode:
        Integer trade mode (0 = disabled, 4 = full).
    swap_long:
        Daily swap charge for long positions (in profit currency per lot).
    swap_short:
        Daily swap charge for short positions.
    margin_initial:
        Initial margin requirement per 1 lot.
    """
    name:             str
    digits:           int
    spread:           int
    point:            float
    tick_size:        float
    tick_value:       float
    contract_size:    float
    volume_min:       float
    volume_max:       float
    volume_step:      float
    currency_base:    str
    currency_profit:  str
    currency_margin:  str
    trade_mode:       int
    swap_long:        float
    swap_short:       float
    margin_initial:   float

    # ── Derived helpers ───────────────────────────────────────────────────────

    @property
    def pip_value(self) -> float:
        """
        Monetary value of one pip (10 points) for 1 standard lot.
        For XAUUSD this equals 10 × tick_value.
        """
        return self.tick_value * 10

    @property
    def spread_cost(self) -> float:
        """Spread cost in profit currency for 1 standard lot."""
        return self.spread * self.tick_value

    @property
    def is_tradeable(self) -> bool:
        """True when the symbol is available for live trading."""
        return self.trade_mode == 4  # MT5 SYMBOL_TRADE_MODE_FULL

    def __str__(self) -> str:
        return (
            f"SymbolSpec({self.name} | digits={self.digits} spread={self.spread}pts "
            f"tick_value={self.tick_value:.4f} contract={self.contract_size} "
            f"vol=[{self.volume_min}–{self.volume_max}] step={self.volume_step})"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Cache
# ─────────────────────────────────────────────────────────────────────────────

_cache: dict[str, SymbolSpec] = {}
_cache_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def get_symbol_spec(client: "MT5Client", symbol: str = "XAUUSD") -> SymbolSpec:
    """
    Fetch symbol specifications, with in-process caching.

    The first call queries MT5; subsequent calls for the same symbol return
    the cached result immediately without hitting the terminal.

    Parameters
    ----------
    client:
        A connected ``MT5Client`` instance.
    symbol:
        MT5 symbol name (default ``"XAUUSD"``).

    Returns
    -------
    SymbolSpec
        Frozen specification snapshot.

    Raises
    ------
    ValueError
        When MT5 cannot find the symbol or returns null info.
    """
    with _cache_lock:
        if symbol in _cache:
            log.debug("SymbolSpec cache hit | {}", symbol)
            return _cache[symbol]

    log.info("Fetching symbol spec | {}", symbol)
    spec = _fetch_symbol_spec(client, symbol)

    with _cache_lock:
        _cache[symbol] = spec

    log.info("SymbolSpec cached | {}", spec)
    return spec


def refresh_symbol_spec(client: "MT5Client", symbol: str = "XAUUSD") -> SymbolSpec:
    """
    Force-refresh the cached spec for *symbol* (e.g. after swap-rate changes).

    Returns the newly fetched ``SymbolSpec``.
    """
    log.info("Refreshing symbol spec | {}", symbol)
    with _cache_lock:
        _cache.pop(symbol, None)
    return get_symbol_spec(client, symbol)


def clear_symbol_cache() -> None:
    """Evict all cached specs (used in tests or after broker reconnect)."""
    with _cache_lock:
        count = len(_cache)
        _cache.clear()
    log.debug("Symbol spec cache cleared | {} entries removed", count)


def format_price(price: float, spec: SymbolSpec) -> str:
    """
    Format *price* using the symbol's decimal precision.

    Parameters
    ----------
    price:
        Raw float price.
    spec:
        The corresponding ``SymbolSpec`` (provides ``digits``).

    Returns
    -------
    str
        Price string, e.g. ``"2345.67"`` for XAUUSD (digits=2).
    """
    return f"{price:.{spec.digits}f}"


def lot_size_for_risk(
    risk_amount: float,
    stop_loss_pts: float,
    spec: SymbolSpec,
) -> float:
    """
    Calculate the position size (in lots) that risks exactly *risk_amount* of
    account currency, given a *stop_loss_pts* stop-loss distance in points.

    Formula:  lots = risk_amount / (stop_loss_pts × tick_value)

    The result is clamped to [volume_min, volume_max] and rounded to the
    nearest volume_step.

    Parameters
    ----------
    risk_amount:
        Capital to risk in account currency (e.g. USD).
    stop_loss_pts:
        Stop-loss distance in points (not pips).
    spec:
        Symbol specification.

    Returns
    -------
    float
        Calculated lot size.
    """
    if stop_loss_pts <= 0 or spec.tick_value <= 0:
        log.warning(
            "lot_size_for_risk: invalid stop_loss_pts={} or tick_value={}",
            stop_loss_pts, spec.tick_value,
        )
        return spec.volume_min

    raw = risk_amount / (stop_loss_pts * spec.tick_value)
    # Use Decimal to avoid float floor-division imprecision.
    # e.g. 2.0 // 0.01 == 199.0 in float; Decimal gives the correct 200.
    from decimal import Decimal, ROUND_DOWN                      # noqa: PLC0415
    d_raw  = Decimal(str(raw))
    d_step = Decimal(str(spec.volume_step))
    lots   = float((d_raw / d_step).to_integral_value(rounding=ROUND_DOWN) * d_step)
    clamped = max(spec.volume_min, min(spec.volume_max, round(lots, 2)))

    log.debug(
        "lot_size_for_risk | risk={} sl_pts={} raw={:.4f} clamped={}",
        risk_amount, stop_loss_pts, raw, clamped,
    )
    return clamped


# ─────────────────────────────────────────────────────────────────────────────
# Internal fetch
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_symbol_spec(client: "MT5Client", symbol: str) -> SymbolSpec:
    """Query MT5 terminal for symbol info; returns a stub in offline mode."""
    # Import here to avoid circular dependency at module level
    from src.mt5.client import MT5NotConnectedError  # noqa: PLC0415

    if not client._connected:          # type: ignore[attr-defined]
        raise MT5NotConnectedError("Client must be connected to fetch symbol specs.")

    if client._stub_mode:              # type: ignore[attr-defined]
        return _stub_symbol_spec(symbol)

    # Ensure the symbol is visible in Market Watch
    if not mt5.symbol_select(symbol, True):
        code, msg = mt5.last_error()
        raise ValueError(
            f"symbol_select({symbol!r}) failed — code={code} msg={msg}"
        )

    info = mt5.symbol_info(symbol)
    if info is None:
        code, msg = mt5.last_error()
        raise ValueError(
            f"symbol_info({symbol!r}) returned None — code={code} msg={msg}"
        )

    return SymbolSpec(
        name             = info.name,
        digits           = info.digits,
        spread           = info.spread,
        point            = info.point,
        tick_size        = info.trade_tick_size,
        tick_value       = info.trade_tick_value,
        contract_size    = info.trade_contract_size,
        volume_min       = info.volume_min,
        volume_max       = info.volume_max,
        volume_step      = info.volume_step,
        currency_base    = info.currency_base,
        currency_profit  = info.currency_profit,
        currency_margin  = info.currency_margin,
        trade_mode       = info.trade_mode,
        swap_long        = info.swap_long,
        swap_short       = info.swap_short,
        margin_initial   = info.margin_initial,
    )


def _stub_symbol_spec(symbol: str) -> SymbolSpec:
    """Return hard-coded XAUUSD-like specs for stub / offline mode."""
    log.warning("STUB SymbolSpec returned for {}", symbol)
    return SymbolSpec(
        name             = symbol,
        digits           = 2,
        spread           = 30,       # 30 points ≈ $0.30
        point            = 0.01,
        tick_size        = 0.01,
        tick_value       = 1.0,      # $1 per tick per lot
        contract_size    = 100.0,    # 100 troy oz per lot
        volume_min       = 0.01,
        volume_max       = 50.0,
        volume_step      = 0.01,
        currency_base    = "XAU",
        currency_profit  = "USD",
        currency_margin  = "USD",
        trade_mode       = 4,        # SYMBOL_TRADE_MODE_FULL
        swap_long        = -6.50,
        swap_short       = -3.20,
        margin_initial   = 0.0,
    )


__all__ = [
    "SymbolSpec",
    "get_symbol_spec",
    "refresh_symbol_spec",
    "clear_symbol_cache",
    "format_price",
    "lot_size_for_risk",
]