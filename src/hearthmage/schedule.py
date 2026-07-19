"""Nexho weekly-schedule ("program") encoding.

Reverse-engineered from the Farho vendor Android client. A heating schedule
is written to the hub with one device command per day-pattern:

    D#<zone>#<modules>#0#0*P#<weekdayBitmask>#<6 blocks>/

where each block is ``<cmd>#<startHour>#<startMin>#<endHour>#<endMin>`` and the
bitmask is ``sum(2**dayOfWeek)`` for the days the pattern applies to. Unused
blocks are the disabled sentinel ``5#24#60#24#60``. It reads back per day with:

    R#<zone>#1#0#0*OD<day>/  ->  OK,<6 blocks x 5 fields>,...,<status>  (35 fields)

Encoding confirmed by decompiling the app (f1/h.java writer, b1/j.java reader,
f1/o.java block model, ProgDaySelection day list); see docs/nexho-protocol.md:

- Day numbering (CONFIRMED): Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6;
  the wire field is sum(2**day).
- Disabled block sentinel (CONFIRMED): "5#24#60#24#60" for climate zones; an
  empty slot reads back as cmd 253.
- Block <cmd> temperature (CONFIRMED whole degrees): a live behavioural test
  wrote a block at cmd 14 covering the current time; the zone's setpoint changed
  to 14 C within ~4s, then reverted when cleared. Same encoding as *T.

Note: over the LAN this hub returns only a day-level summary from *OD (bare
``OK`` = that day is programmed, ``ER,1`` = not programmed / asleep), not the
35-field block detail the Android app parses (that reaches the iOS app via the
cloud). So this app owns schedule definitions (stored locally) and writes them
to the hub; it does not read block detail back. parse_day_schedule is kept for
completeness / other program types. See docs/nexho-protocol.md.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_BLOCKS = 6
DISABLED_CMD = 5  # climate "off"/disabled block sentinel (f1/h.java:287)
DISABLED_BLOCK = f"{DISABLED_CMD}#24#60#24#60"
_EMPTY_CMD_THRESHOLD = 240  # cmd > 240 (i.e. 253) marks an empty slot on read (b1/j.java:915)

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


def _hm(minute: int) -> tuple[int, int]:
    return minute // 60, minute % 60


def build_program_command(
    zone: int, blocks: list[Block], days: list[int], modules: int = 1
) -> str:
    """Build the ``D#...*P#`` command applying ``blocks`` to every day in ``days``."""
    if not days:
        raise ValueError("a schedule pattern needs at least one day")
    used = list(blocks)[:MAX_BLOCKS]
    parts = [f"D#{zone}#{modules}#0#0*P#{weekday_bitmask(days)}"]
    for block in used:
        sh, sm = _hm(block.start)
        eh, em = _hm(block.end)
        parts.append(f"{block.target}#{sh}#{sm}#{eh}#{em}")
    parts.extend([DISABLED_BLOCK] * (MAX_BLOCKS - len(used)))
    return "#".join(parts) + "/"


def parse_day_schedule(resp: str) -> list[Block]:
    """Parse an ``*OD`` reply into the day's active blocks (disabled ones dropped)."""
    if not resp.startswith(("OK,", "OPOK,")):
        raise ValueError(f"not a schedule response: {resp!r}")
    fields = resp.split(",")
    blocks: list[Block] = []
    for i in range(MAX_BLOCKS):
        seg = fields[i * 5 + 1 : i * 5 + 6]
        if len(seg) < 5:
            break
        try:
            cmd, sh, sm, eh, em = (int(x) for x in seg)
        except ValueError:
            continue
        if cmd > _EMPTY_CMD_THRESHOLD or (sh >= 24 and sm >= 60):
            continue  # empty or disabled block
        blocks.append(Block(cmd, sh * 60 + sm, eh * 60 + em))
    return blocks
