"""Nexho weekly-schedule ("program") encoding.

Reverse-engineered from the Farho vendor Android client. A heating schedule
is written to the hub with one device command per day-pattern:

    D#<zone>#<modules>#0#0*P#<weekdayBitmask>#<6 blocks>/

where each block is ``<cmd>#<startHour>#<startMin>#<endHour>#<endMin>`` and the
bitmask is ``sum(2**dayOfWeek)`` for the days the pattern applies to. Unused
blocks are the disabled sentinel ``5#24#60#24#60``. It reads back per day with:

    R#<zone>#1#0#0*?P<day>/  ->  OK,P<day>,11,<6 blocks x 5 fields>

Encoding confirmed by decompiling the app (f1/h.java writer, b1/j.java reader,
f1/o.java block model, ProgDaySelection day list); see docs/nexho-protocol.md:

- Day numbering (CONFIRMED): Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6;
  the wire field is sum(2**day).
- Disabled block sentinel (CONFIRMED): "5#24#60#24#60" for climate zones; an
  empty slot reads back as cmd 253.
- Block <cmd> temperature (CONFIRMED whole degrees): a live behavioural test
  wrote a block at cmd 14 covering the current time; the zone's setpoint changed
  to 14 C within ~4s, then reverted when cleared. Same encoding as *T.

Over the LAN, ``*OD`` returns only a bare ``OK``/``ER,1`` on this hub, but
``R#<zone>#1#0#0*?P<day>/`` returns the day's six blocks (``OK,P<day>,11,`` then
the blocks), confirmed live 2026-11-01; see docs/nexho-protocol.md. This app
still owns schedule definitions and does not import them yet.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_BLOCKS = 6
MIN_TARGET = 5  # same range as the manual setpoint (nexho_client _MIN_TEMP/_MAX_TEMP)
MAX_TARGET = 30
DAY_MINUTES = 24 * 60
DISABLED_CMD = 5  # climate "off"/disabled block sentinel (f1/h.java:287)
DISABLED_BLOCK = f"{DISABLED_CMD}#24#60#24#60"
_EMPTY_CMD_THRESHOLD = 240  # cmd > 240 (e.g. 253) marks an empty slot on read

# Weekday -> bit index, confirmed from the day-list order in ProgDaySelection.
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass(frozen=True)
class Block:
    """One heating window: hold ``target`` degrees from ``start`` to ``end``
    (both minutes-of-day, 0..1440)."""

    target: int
    start: int
    end: int


def weekday_bitmask(days: list[int]) -> int:
    """Bitmask for a set of weekday indexes (0..6): sum of 2**day."""
    mask = 0
    for day in days:
        mask |= 1 << day
    return mask


def validate_blocks(blocks: list[Block]) -> list[Block]:
    """Return ``blocks`` sorted by start, or raise ValueError if they cannot be
    written: more than six, a target outside 5..30, times outside the day, an
    empty or backwards window, or two windows that overlap (touching is fine)."""
    if len(blocks) > MAX_BLOCKS:
        raise ValueError(f"a day can hold at most {MAX_BLOCKS} time frames")
    ordered = sorted(blocks, key=lambda b: b.start)
    for b in ordered:
        if not (MIN_TARGET <= b.target <= MAX_TARGET):
            raise ValueError(f"temperature must be {MIN_TARGET}-{MAX_TARGET} degrees")
        if not (0 <= b.start < b.end <= DAY_MINUTES):
            raise ValueError(f"invalid block times: start={b.start} end={b.end}")
    for prev, nxt in zip(ordered, ordered[1:]):
        if nxt.start < prev.end:
            raise ValueError("time frames overlap")
    return ordered


def validate_days(days: list[int]) -> list[int]:
    """Return the weekday indexes sorted and de-duplicated; each must be 0..6."""
    if not days:
        raise ValueError("a schedule pattern needs at least one day")
    for day in days:
        if not (0 <= day <= 6):
            raise ValueError(f"invalid weekday: {day}")
    return sorted(set(days))


def _hm(minute: int) -> tuple[int, int]:
    # The vendor writer normalises hour 24 to 0, so a block ending at midnight
    # goes out as 0:00 (inferred from the decompiled app, not live-tested).
    return (minute // 60) % 24, minute % 60


def build_program_command(
    zone: int, blocks: list[Block], days: list[int], modules: int = 1
) -> str:
    """Build the ``D#...*P#`` command applying ``blocks`` to every day in ``days``."""
    used = validate_blocks(list(blocks))
    parts = [f"D#{zone}#{modules}#0#0*P#{weekday_bitmask(validate_days(days))}"]
    for block in used:
        sh, sm = _hm(block.start)
        eh, em = _hm(block.end)
        parts.append(f"{block.target}#{sh}#{sm}#{eh}#{em}")
    parts.extend([DISABLED_BLOCK] * (MAX_BLOCKS - len(used)))
    return "#".join(parts) + "/"


def parse_program_day(resp: str, day: int) -> list[Block]:
    """Parse a ``*?P<day>`` reply (``OK,P<day>,<x>,`` then six blocks of
    ``cmd,startH,startM,endH,endM``) into that day's blocks, sorted.

    Disabled and empty slots (cmd > 240 or start hour > 23, as the vendor app
    reads them) are dropped; an end of 0:00 means midnight. Raises ValueError
    for anything else this app cannot represent, e.g. an Off (cmd 0) block."""
    fields = resp.split(",")
    if fields[:2] != ["OK", f"P{day}"] or len(fields) < 3 + MAX_BLOCKS * 5:
        raise ValueError(f"not a program reply for day {day}: {resp!r}")
    blocks: list[Block] = []
    for i in range(MAX_BLOCKS):
        cmd, sh, sm, eh, em = (int(x) for x in fields[3 + i * 5 : 8 + i * 5])
        if cmd > _EMPTY_CMD_THRESHOLD or sh > 23:
            continue  # disabled (5#24#60#24#60) or empty (253) slot
        if cmd == 0:
            raise ValueError("the radiator holds an Off time frame, which this app can't edit yet")
        end = eh * 60 + em
        blocks.append(Block(cmd, sh * 60 + sm, end or DAY_MINUTES))
    return validate_blocks(blocks)
