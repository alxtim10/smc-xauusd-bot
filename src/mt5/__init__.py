"""
src/mt5
───────
MetaTrader 5 integration package.

Public surface
──────────────
    from src.mt5 import MT5Client, Timeframe, Tick, ConnectionStatus
    from src.mt5 import get_symbol_spec, SymbolSpec
    from src.mt5 import HistoryManager, BarRequest, FetchResult
"""

from src.mt5.client import (
    MT5Client, MT5DataError, MT5Error, MT5NotConnectedError,
    Tick, ConnectionStatus, Timeframe, XAUUSD_TIMEFRAMES, mt5_client,
)
from src.mt5.symbol_info import (
    SymbolSpec, get_symbol_spec, refresh_symbol_spec,
    clear_symbol_cache, format_price, lot_size_for_risk,
)
from src.mt5.history import (
    HistoryManager, BarRequest, FetchResult, build_history_manager,
)

__all__ = [
    "MT5Client", "MT5Error", "MT5NotConnectedError", "MT5DataError",
    "Tick", "ConnectionStatus", "Timeframe", "XAUUSD_TIMEFRAMES", "mt5_client",
    "SymbolSpec", "get_symbol_spec", "refresh_symbol_spec",
    "clear_symbol_cache", "format_price", "lot_size_for_risk",
    "HistoryManager", "BarRequest", "FetchResult", "build_history_manager",
]
