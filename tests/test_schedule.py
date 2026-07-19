from hearthmage.schedule import (
    Block,
    build_program_command,
    parse_day_schedule,
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


def test_parse_day_schedule_drops_disabled_and_empty():
    resp = "OK,20,6,30,8,30,5,24,60,24,60,253,0,0,0,0,5,24,60,24,60,5,24,60,24,60,5,24,60,24,60,0,0,0,132"
    blocks = parse_day_schedule(resp)
    assert blocks == [Block(20, 390, 510)]


def test_parse_day_schedule_two_blocks():
    resp = "OPOK,18,0,0,7,0,21,17,0,23,30,5,24,60,24,60,5,24,60,24,60,5,24,60,24,60,5,24,60,24,60,0,0,0,132"
    assert parse_day_schedule(resp) == [Block(18, 0, 420), Block(21, 1020, 1410)]


def test_parse_day_schedule_rejects_non_schedule():
    import pytest

    with pytest.raises(ValueError):
        parse_day_schedule("ER,1")
