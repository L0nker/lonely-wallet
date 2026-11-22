"""Pure portfolio-return helpers shared by the API and regression tests."""
from __future__ import annotations

from datetime import date
from math import isfinite


def calculate_position_performance(
    quantity: float,
    average_price: float,
    current_price: float,
    paid_dividends: float = 0.0,
    *,
    quote_available: bool = True,
) -> dict[str, float | None]:
    """Calculate price variation and total return from the average purchase price.

    Only the caller-provided paid-dividend total is included. The function does
    not consult announced/future income or Pluggy return percentages.
    """
    try:
        qty = float(quantity)
        avg = float(average_price)
        price = float(current_price)
        dividends = float(paid_dividends or 0.0)
    except (TypeError, ValueError):
        return {
            "invested": None,
            "variation_value": None,
            "variation_pct": None,
            "profitability_value": None,
            "profitability_pct": None,
        }

    if not all(isfinite(value) for value in (qty, avg, price, dividends)):
        return {
            "invested": None,
            "variation_value": None,
            "variation_pct": None,
            "profitability_value": None,
            "profitability_pct": None,
        }
    if qty <= 0 or avg <= 0 or not quote_available or price <= 0:
        return {
            "invested": round(max(0.0, qty) * max(0.0, avg), 2),
            "variation_value": None,
            "variation_pct": None,
            "profitability_value": None,
            "profitability_pct": None,
        }

    invested = qty * avg
    variation_value = qty * (price - avg)
    variation_pct = variation_value / invested * 100.0
    profitability_value = variation_value + max(0.0, dividends)
    profitability_pct = profitability_value / invested * 100.0
    return {
        "invested": round(invested, 2),
        "variation_value": round(variation_value, 2),
        "variation_pct": round(variation_pct, 2),
        "profitability_value": round(profitability_value, 2),
        "profitability_pct": round(profitability_pct, 2),
    }


def is_paid_provento(status: str, payment_date: str, today: date) -> bool:
    """True only for events explicitly marked Paid with a payment date not future."""
    if str(status or "").strip().casefold() != "pago":
        return False
    raw = str(payment_date or "").strip()[:10]
    try:
        return date.fromisoformat(raw) <= today
    except (TypeError, ValueError):
        return False
