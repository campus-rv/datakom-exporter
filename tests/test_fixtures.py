"""End-to-end: fixture registers -> pymodbus TCP server -> exporter -> Prometheus exposition.

Every dump from a real controller under tests/fixtures/recorded/ is picked up automatically,
once as recorded (low_first) and once with the 32-bit words swapped (high_first).
"""

import pytest
from conftest import all_fixture_files, fixture_words, load_fixture, scrape

from datakom_exporter.registers import READ_PLAN, REGISTERS

FILES = all_fixture_files()


@pytest.fixture(params=FILES, ids=[f.stem for f in FILES])
def fixture(request):
    return load_fixture(request.param)


def test_fixture_scrape_matches_expected(fixture, make_controller, make_collector):
    sim = make_controller(fixture_words(fixture), unit_id=fixture.get("unit_id", 1))
    col = make_collector(sim.port, word_order=fixture.get("word_order", "low_first"),
                         unit_id=fixture.get("unit_id", 1))
    got = scrape(col)

    assert got["genset_modbus_up"] == 1
    expected = dict(fixture.get("expected", {}))
    if "genset_modbus_failed_blocks" in expected:
        # a dump holds the blocks of the exporter version that recorded it; blocks added to the
        # register map later are absent and fail like on a controller without them
        words = fixture_words(fixture)
        expected["genset_modbus_failed_blocks"] += sum(
            1 for start, count in READ_PLAN if any(a not in words for a in range(start, start + count)))
    for key, want in expected.items():
        assert key in got, f"missing {key}"
        assert got[key] == pytest.approx(want), key
    for name in fixture.get("expected_absent", []):
        assert not any(k == name or k.startswith(name + "{") for k in got), f"{name} exported"



def swap_words(words: dict[int, int]) -> dict[int, int]:
    """The same registers as a high-word-first controller would hold them."""
    out = dict(words)
    for r in REGISTERS:
        lo, hi = r.address, r.address + 1
        if r.words == 2 and lo in words and hi in words:
            out[lo], out[hi] = words[hi], words[lo]
    return out


RECORDED = [f for f in FILES if f.parent.name == "recorded"]


@pytest.mark.parametrize("path", RECORDED, ids=[f.stem for f in RECORDED])
def test_high_first_controller_decodes_the_same(path, make_controller, make_collector):
    """No real high_first controller is at hand: swap the 32-bit words of a recorded dump."""
    fx = load_fixture(path)
    assert fx["word_order"] == "low_first"
    words = fixture_words(fx)
    low = scrape(make_collector(make_controller(words).port, word_order="low_first"))
    high = scrape(make_collector(make_controller(swap_words(words)).port, word_order="high_first"))

    def values(got):
        return {k: v for k, v in got.items()
                if not k.startswith(("genset_modbus_", "genset_controller_info"))}

    assert values(high) == values(low)
    assert swap_words(words) != words
