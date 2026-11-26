"""Energy consumption decoding for Nexho climate zones.

A radiator reports its consumption as run-time counters, one per day (``*?UD``,
8 buckets including today) or per month (``*?UM``, up to 7 buckets), prefixed by
its rated wattage. Each counter unit is a 6-minute interval the element was on,
so::

    kWh = counter * (6 / 60) * (rated_watts / 1000)

and cost is ``kWh * price_per_kWh``. The framing (``OK,<watts>,<counters...>``)
is inferred from the vendor app and remains **unconfirmed**: a live attempt
across all zones got ``ER,1`` from every ``*?UD``/``*?UM`` read, so on this
hardware the counters may not be retrievable over the LAN at all. (``*UDEL``,
which answered ``OK``, is the vendor app's consumption reset, not a probe;
this app sent it before each read until 2026-11-01.) The decode below is guarded so an ``ER`` reply
is simply "no reading". See docs/nexho-protocol.md.
"""

from __future__ import annotations

COUNTER_HOURS = 6 / 60  # each counter unit is 6 minutes of element-on time
DAILY_BUCKETS = 8  # *?UD: today and the seven days before (inferred)
MONTHLY_BUCKETS_MAX = 7  # *?UM: up to seven months (inferred)


def parse_energy(resp: str) -> tuple[int | None, list[int]]:
    """Parse an ``*?UD`` / ``*?UM`` reply into (rated_watts, counters).

    A non-OK reply (``ER``/timeout) yields ``(None, [])``, and so does one
    with a field that is not a number: that is the reply to another query.
    Trailing empty fields are ignored.
    """
    if not resp.startswith(("OK,", "OPOK,")):
        return None, []
    parts = resp.split(",")[1:]  # drop the OK marker
    if not parts:
        return None, []
    try:
        watts = int(parts[0])
    except ValueError:
        return None, []
    fields = [field.strip() for field in parts[1:]]
    while fields and not fields[-1]:
        fields.pop()
    if not all(field.lstrip("-").isdigit() for field in fields):
        return None, []
    return watts, [int(field) for field in fields]


def counter_to_kwh(counter: int, watts: int | None) -> float:
    """kWh for one counter of run-time at ``watts`` (0.0 if wattage unknown)."""
    if not watts:
        return 0.0
    return counter * COUNTER_HOURS * (watts / 1000.0)


def daily_kwh(watts: int | None, counters: list[int]) -> list[float]:
    """kWh per bucket for a list of counters."""
    return [counter_to_kwh(c, watts) for c in counters]


def cost(kwh: float, price_per_kwh: float) -> float:
    """Money cost of ``kwh`` at ``price_per_kwh`` (same currency as the price)."""
    return kwh * price_per_kwh
