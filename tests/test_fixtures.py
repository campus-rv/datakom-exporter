"""End-to-end: fixture registers -> pymodbus TCP server -> exporter -> Prometheus exposition.

Every JSON under tests/fixtures/ (and tests/fixtures/recorded/, where dumps from real
controllers go) is picked up automatically.
"""

import pytest
from conftest import all_fixture_files, fixture_words, load_fixture, scrape

from datakom_exporter.collector import decode_snapshot
from datakom_exporter.registers import READ_PLAN
from datakom_exporter.sanity import check

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


def test_fixture_values_are_plausible(fixture):
    words = fixture_words(fixture)
    problems = check(decode_snapshot(words, fixture.get("word_order", "low_first")))
    assert problems == []


def test_fixture_wrong_word_order_is_detected_as_implausible(fixture):
    """Guard for the probe: decoding with the wrong order must not look plausible."""
    words = fixture_words(fixture)
    voltage_set = any(words.get(a) for a in (10240, 10241, 10246, 10247))
    hours_set = words.get(10622) or words.get(10623)
    if not (voltage_set or hours_set):
        pytest.skip("no 32-bit values set in this fixture")
    wrong = "high_first" if fixture.get("word_order", "low_first") == "low_first" else "low_first"
    assert check(decode_snapshot(words, wrong)) != []
