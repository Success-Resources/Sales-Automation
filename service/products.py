"""Stripe product names used at MMI event sales.

MMI2609MAD | Quantum Leap 2027
MMI2610ROT | Never Work Again 2027
"""

from __future__ import annotations

import re

_NAME = re.compile(
    r"^\s*MMI\s*(\d{2})\s*(\d{2})\s*([A-Za-z]{3})\s*\|\s*(.+?)\s*$",
    re.I,
)


def parse_product_name(name: str) -> tuple[str, str] | None:
    """Return (event tab, programme) or None when the name is not an event product."""
    match = _NAME.match(name or "")
    if not match:
        return None
    year, month, city, programme = match.groups()
    return f"MMI{year}{month}{city.upper()}", programme.strip()
