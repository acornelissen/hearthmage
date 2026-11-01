import pytest


from hearthmage.schedule import (
    Block,
    build_program_command,
    parse_program_day,
    weekday_bitmask,
)


def test_weekday_bitmask():
    assert weekday_bitmask([]) == 0
    assert weekday_bitmask([0]) == 1
    assert weekday_bitmask([0, 1, 2, 3, 4]) == 0b0011111  # 31
    assert weekday_bitmask([5, 6]) == 0b1100000  # 96


def test_build_program_command_one_block_pads_disabled():
    cmd = build_program_command(2, [Block(20, 390, 510)], [0])  # 06:30-08:30, Monday
    assert cmd == (
        "D#2#1#0#0*P#1#20#6#30#8#30"
        "#5#24#60#24#60#5#24#60#24#60#5#24#60#24#60#5#24#60#24#60#5#24#60#24#60/"
    )


def test_build_program_command_two_blocks_weekdays():
    cmd = build_program_command(
        3, [Block(20, 390, 510), Block(21, 1020, 1320)], [0, 1, 2, 3, 4]
    )
    assert cmd.startswith("D#3#1#0#0*P#31#20#6#30#8#30#21#17#0#22#0#5#24#60#24#60")
    assert cmd.endswith("/")
    assert cmd.count("#24#60#24#60") == 4  # four disabled blocks


_OFF = "5,24,60,24,60"


def _reply(day: int, *blocks: str) -> str:
    padded = list(blocks) + [_OFF] * (6 - len(blocks))
    return f"OK,P{day},11," + ",".join(padded)


def test_parse_program_day_all_disabled_is_empty():
    # Verbatim live reply from a radiator with no program (2026-11-01).
    live = "OK,P0,11,5,24,60,24,60,5,24,60,24,60,5,24,60,24,60,5,24,60,24,60,5,24,60,24,60,5,24,60,24,60"
    assert parse_program_day(live, 0) == []


def test_parse_program_day_reads_blocks_and_midnight():
    resp = _reply(2, "21,17,0,0,0", "20,7,0,9,0")  # out of order, second ends at 00:00
    assert parse_program_day(resp, 2) == [Block(20, 420, 540), Block(21, 1020, 1440)]


def test_parse_program_day_skips_empty_slots():
    resp = _reply(3, "253,0,0,0,0", "19,6,30,8,0")  # 253 = empty slot
    assert parse_program_day(resp, 3) == [Block(19, 390, 480)]


@pytest.mark.parametrize(
    "resp",
    [
        "ER,1",
        "OK",
        _reply(4, "20,7,0,9,0"),  # reply for another day
        "OK,P1,11,20,7,0",  # truncated
        _reply(1, "0,7,0,9,0"),  # an Off block: not representable here
        _reply(1, "40,7,0,9,0"),  # outside 5..30
        _reply(1, "20,7,0,9,0", "18,8,0,10,0"),  # overlapping
        _reply(1, "x,7,0,9,0"),
    ],
)
def test_parse_program_day_rejects(resp):
    with pytest.raises(ValueError):
        parse_program_day(resp, 1)


def test_block_ending_at_midnight_is_sent_as_hour_zero():
    # The vendor writer normalises hour 24 to 0, so 22:00-24:00 goes out as 22#0#0#0.
    cmd = build_program_command(1, [Block(20, 1320, 1440)], [0])
    assert cmd.startswith("D#1#1#0#0*P#1#20#22#0#0#0#5#24#60#24#60")


def test_validate_blocks_sorts_by_start():
    from hearthmage.schedule import validate_blocks

    blocks = [Block(21, 1020, 1320), Block(20, 420, 540)]
    assert validate_blocks(blocks) == [Block(20, 420, 540), Block(21, 1020, 1320)]


def test_validate_blocks_allows_touching_blocks():
    from hearthmage.schedule import validate_blocks

    assert validate_blocks([Block(20, 420, 540), Block(16, 540, 600)])


@pytest.mark.parametrize(
    "blocks",
    [
        [Block(20, 420, 600), Block(15, 480, 700)],  # overlap
        [Block(4, 420, 540)],  # below the 5 degree minimum
        [Block(31, 420, 540)],  # above the 30 degree maximum
        [Block(200, 420, 540)],  # would collide with the half-degree encoding
        [Block(20, 600, 600)],  # empty
        [Block(20, 600, 300)],  # backwards
        [Block(20, -10, 300)],  # before midnight
        [Block(20, 300, 1441)],  # past midnight
        [Block(20, i * 60, i * 60 + 30) for i in range(7)],  # too many
    ],
)
def test_validate_blocks_rejects(blocks):
    from hearthmage.schedule import validate_blocks

    with pytest.raises(ValueError):
        validate_blocks(blocks)


def test_validate_days():
    from hearthmage.schedule import validate_days

    assert validate_days([4, 0, 0]) == [0, 4]  # sorted, de-duplicated
    for bad in ([], [7], [-1]):
        with pytest.raises(ValueError):
            validate_days(bad)
