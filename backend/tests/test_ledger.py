import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests._support import collect
from src.domain import ledger


def row(day, level, meter=None):
    item = {"reservoir_id": "RW-02", "reading_date": f"2026-11-{day:02d}", "level_m": level}
    if meter is not None:
        item["meter_id"] = meter
    return item


def states(snapshot):
    return {(rec["date"], rec["meter"]): rec["state"] for rec in snapshot["records"]}


def test_single_meter_ledger_is_unchanged():
    snapshot = ledger.build_ledger([row(d, 0.80 + 0.03 * d) for d in range(1, 16)])
    assert snapshot["handovers"] == []
    assert snapshot["isolation"] is None
    assert snapshot["counts"] == {"used": 15, "overlap": 0, "isolated": 0}
    assert [rec["level_m"] for rec in snapshot["records"]] == [round(0.80 + 0.03 * d, 6) for d in range(1, 16)]


def test_same_day_dual_folds_new_meter_back():
    rows = [row(d, 0.80 + 0.03 * d, "A") for d in range(1, 6)]
    rows += [row(6, 0.98, "A"), row(6, 10.98, "B")]
    rows += [row(d, 10.98 + 0.03 * (d - 6), "B") for d in range(7, 16)]
    snapshot = ledger.build_ledger(rows)
    handover = snapshot["handovers"][0]
    assert handover["offset_m"] == 10.0
    assert handover["cumulative_offset_m"] == 10.0
    assert handover["consistent"] is True
    assert snapshot["isolation"] is None
    assert states(snapshot)[("2026-11-06", "A")] == "overlap"
    assert states(snapshot)[("2026-11-06", "B")] == "used"
    assert snapshot["counts"]["isolated"] == 0
    folded = {rec["date"]: rec["level_m"] for rec in snapshot["records"] if rec["state"] == "used"}
    assert folded["2026-11-07"] == 1.01
    assert folded["2026-11-15"] == 1.25


def test_chained_handovers_accumulate_offsets():
    rows = [row(d, 0.80 + 0.03 * d, "A") for d in range(1, 5)]
    rows += [row(5, 0.95, "A"), row(5, 10.95, "B")]
    rows += [row(d, 10.95 + 0.03 * (d - 5), "B") for d in range(6, 10)]
    rows += [row(10, 11.07, "B"), row(10, 1.07, "C")]
    rows += [row(d, 1.07 + 0.03 * (d - 10), "C") for d in range(11, 16)]
    snapshot = ledger.build_ledger(rows)
    assert [(h["offset_m"], h["cumulative_offset_m"]) for h in snapshot["handovers"]] == [(10.0, 10.0), (-10.0, 0.0)]
    folded = {rec["date"]: rec["level_m"] for rec in snapshot["records"] if rec["state"] == "used"}
    assert folded["2026-11-12"] == round(1.07 + 0.06, 6)


def test_missing_dual_isolates_from_handover_but_keeps_earlier_segment():
    rows = [row(d, 0.80 + 0.03 * d, "A") for d in range(1, 6)]
    rows += [row(d, 10.98 + 0.03 * (d - 6), "B") for d in range(6, 16)]
    snapshot = ledger.build_ledger(rows)
    assert snapshot["handovers"][0]["consistent"] is False
    assert snapshot["isolation"]["date"] == "2026-11-06"
    assert [seg["state"] for seg in snapshot["segments"]] == ["usable", "isolated"]
    assert snapshot["segments"][0] == {"index": 1, "start": "2026-11-01", "end": "2026-11-05",
                                       "days": 5, "state": "usable", "reason": None}
    assert snapshot["segments"][1]["start"] == "2026-11-06"
    assert states(snapshot)[("2026-11-06", "B")] == "isolated"


def test_late_dual_reading_recalibrates_and_reuses_the_handover_day():
    rows = [row(d, 0.80 + 0.03 * d, "A") for d in range(1, 6)]
    rows += [row(d, 10.98 + 0.03 * (d - 6), "B") for d in range(6, 16)]
    before = ledger.build_ledger(rows)
    assert before["isolation"]["date"] == "2026-11-06"
    rows += [row(8, 1.04, "A")]
    after = ledger.build_ledger(rows)
    assert after["isolation"] is None
    assert after["handovers"][0]["offset_m"] == 10.0
    assert after["handovers"][0]["dual_readings"][0]["date"] == "2026-11-08"
    assert states(after)[("2026-11-08", "A")] == "overlap"
    assert ledger.affected_segments(before, after) == [
        {"index": 1, "start": "2026-11-01", "end": "2026-11-15", "days": 15, "state": "usable", "reason": None}]


def test_inconsistent_dual_readings_isolate_from_the_handover():
    rows = [row(d, 0.80 + 0.03 * d, "A") for d in range(1, 6)]
    rows += [row(6, 0.98, "A"), row(6, 10.98, "B")]
    rows += [row(7, 11.01, "B")]
    rows += [row(8, 1.04, "A"), row(8, 11.10, "B")]
    rows += [row(d, 10.98 + 0.03 * (d - 6), "B") for d in range(9, 16)]
    snapshot = ledger.build_ledger(rows)
    assert snapshot["isolation"]["date"] == "2026-11-06"
    assert "不一致" in snapshot["isolation"]["reason"]
    assert snapshot["segments"][0]["end"] == "2026-11-05"


def test_blank_meter_means_active_meter_and_handover_can_be_written_with_blank_old_row():
    rows = [row(d, 0.80 + 0.03 * d) for d in range(1, 6)]
    rows += [row(6, 0.98), row(6, 10.98, "B")]
    rows += [row(d, 10.98 + 0.03 * (d - 6), "B") for d in range(7, 16)]
    snapshot = ledger.build_ledger(rows)
    assert snapshot["handovers"][0]["offset_m"] == 10.0
    assert snapshot["isolation"] is None
    folded = {rec["date"]: rec["level_m"] for rec in snapshot["records"] if rec["state"] == "used"}
    assert folded["2026-11-15"] == 1.25


def test_replaying_a_batch_is_idempotent_and_conflicts_are_rejected():
    rows = [row(d, 0.80 + 0.03 * d, "A") for d in range(1, 6)]
    _pool, once = ledger.merge_rows(rows)
    _pool, twice = ledger.merge_rows(rows, rows)
    assert ledger.canonical_rows("RW-02", once) == ledger.canonical_rows("RW-02", twice)
    try:
        ledger.merge_rows(rows, [row(3, 9.99, "A")])
    except ValueError as error:
        assert "已有不同读数" in str(error)
    else:
        raise AssertionError("conflicting replay should be rejected")


def test_ledger_rows_round_trip_rebuilds_the_same_snapshot():
    rows = [row(d, 0.80 + 0.03 * d, "A") for d in range(1, 6)]
    rows += [row(6, 0.98, "A"), row(6, 10.98, "B")]
    rows += [row(d, 10.98 + 0.03 * (d - 6), "B") for d in range(7, 16)]
    snapshot = ledger.build_ledger(rows)
    rebuilt = ledger.build_ledger(ledger.ledger_rows(snapshot))
    assert rebuilt == snapshot


def test_first_day_dual_reading_is_a_valid_calibration():
    rows = [row(1, 0.83, "A"), row(1, 10.83, "B")]
    rows += [row(d, 10.83 + 0.03 * (d - 1), "B") for d in range(2, 10)]
    snapshot = ledger.build_ledger(rows)
    assert snapshot["isolation"] is None
    assert snapshot["handovers"][0]["offset_m"] == 10.0
    folded = {rec["date"]: rec["level_m"] for rec in snapshot["records"] if rec["state"] == "used"}
    assert folded["2026-11-09"] == round(0.83 + 0.03 * 8, 6)


def test_two_meters_read_together_forever_cannot_be_ordered():
    rows = [row(d, 0.80 + 0.03 * d, "A") for d in range(1, 6)]
    rows += [row(d, 10.80 + 0.03 * d, "B") for d in range(1, 6)]
    snapshot = ledger.build_ledger(rows)
    assert snapshot["isolation"]["date"] == "2026-11-01"
    assert all(record["state"] == "isolated" for record in snapshot["records"])
    assert snapshot["segments"][0]["state"] == "isolated"


collect(globals())
