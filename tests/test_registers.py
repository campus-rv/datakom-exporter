import pytest

from datakom_exporter import registers as R


def test_register_keys_unique():
    keys = [r.key for r in R.REGISTERS]
    assert len(keys) == len(set(keys))


def test_registers_do_not_overlap():
    seen = {}
    for r in R.REGISTERS:
        for a in r.addresses:
            assert a not in seen, f"{r.name} overlaps {seen[a]} at {a}"
            seen[a] = r.name


def test_read_plan_respects_16_register_limit_and_covers_everything():
    covered = set()
    for start, count in R.READ_PLAN:
        assert 1 <= count <= R.MAX_BLOCK
        covered.update(range(start, start + count))
    assert set(R.needed_addresses()) <= covered


def test_read_plan_never_touches_write_only_registers():
    covered = {a for s, c in R.READ_PLAN for a in range(s, s + c)}
    assert 8193 not in covered and 8210 not in covered


def test_plan_blocks_groups_within_window():
    assert R.plan_blocks([1, 2, 16, 17, 40]) == [(1, 16), (17, 1), (40, 1)]
    assert R.plan_blocks([5]) == [(5, 1)]
    assert R.plan_blocks([]) == []


@pytest.mark.parametrize("words,order,signed,expected", [
    ({0: 2317, 1: 0}, "low_first", False, 2317),
    ({0: 0, 1: 2317}, "high_first", False, 2317),
    ({0: 4464, 1: 1}, "low_first", False, 70000),
    ({0: 1, 1: 4464}, "high_first", False, 70000),
    ({0: 0xFF85, 1: 0xFFFF}, "low_first", True, -123),
    ({0: 0xFFFF, 1: 0xFF85}, "high_first", True, -123),
    ({0: 0xFFFF, 1: 0xFF85}, "high_first", False, 0xFFFFFF85),
])
def test_decode_32(words, order, signed, expected):
    assert R.decode(words, 0, 2, signed, order) == expected


def test_decode_16_and_missing():
    assert R.decode({7: 0xFCAE}, 7, 1, True, "low_first") == -850
    assert R.decode({7: 0xFCAE}, 7, 1, False, "low_first") == 0xFCAE
    assert R.decode({}, 7, 1, False, "low_first") is None
    assert R.decode({7: 1}, 7, 2, False, "low_first") is None


def test_alarm_bits():
    assert R.alarm_bits([0b1001, 0, 0x8000]) == [0, 3, 47]
    assert R.alarm_bits([0] * 16) == []


def test_model_name():
    assert R.model_name(0xD300) == "D-300"
    assert R.model_name(0xD509) == "unknown-0xd509"
    assert R.model_name(None) == "unknown"


def test_guess_word_order():
    assert R.guess_word_order({10246: 2317, 10247: 0, 10622: 17591, 10623: 0})[0] == "low_first"
    assert R.guess_word_order({10246: 0, 10247: 2317})[0] == "high_first"
    assert R.guess_word_order({10246: 0, 10247: 0})[0] is None
    # tie -> undecided
    assert R.guess_word_order({10246: 1, 10247: 0, 10240: 0, 10241: 1})[0] is None
