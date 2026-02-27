"""
core/risk.py - Position sizing based on risk (R) and SL distance.
"""


def calculate_lot_size(balance: float, risk_pct: float, sl_pips: float,
                       pip_value_per_lot: float = 10.0,
                       min_lot: float = 0.01, max_lot: float = 100.0,
                       lot_step: float = 0.01) -> float:
    """
    Calculate position size based on risk per trade.

    balance:           current account balance ($)
    risk_pct:          risk percentage (e.g. 1.5)
    sl_pips:           stop loss distance in pips
    pip_value_per_lot: pip value for 1 standard lot ($10 for EURUSD)
    """
    if sl_pips <= 0 or balance <= 0 or risk_pct <= 0:
        return 0.0

    risk_dollars = balance * risk_pct / 100.0
    lots = risk_dollars / (sl_pips * pip_value_per_lot)

    # Normalize to lot step
    if lot_step > 0:
        lots = int(lots / lot_step) * lot_step

    # Clamp
    lots = max(min_lot, min(max_lot, lots))
    return round(lots, 2)


def validate_sl_tp(entry: float, sl: float, tp: float, direction: int) -> bool:
    """Validate that SL and TP are on the correct side of entry."""
    if direction > 0:  # BUY
        return sl < entry < tp
    elif direction < 0:  # SELL
        return tp < entry < sl
    return False
