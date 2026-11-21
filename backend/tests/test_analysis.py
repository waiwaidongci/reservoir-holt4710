import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests._support import collect
from src.domain import analysis, ledger
from src.domain.spec import SPEC

DEFAULT = {field["key"]: field["default"] for field in SPEC["params"]}


def row(day, level, meter=None):
    item = {"reservoir_id": "RW-02", "reading_date": f"2026-11-{day:02d}", "level_m": level}
    if meter is not None:
        item["meter_id"] = meter
    return item


def tables(result):
    return {table["title"]: table["rows"] for table in result["tables"]}


def test_same_day_handover_forecasts_on_the_folded_curve():
    rows = [row(d, 1.00 + 0.05 * d, "A") for d in range(1, 8)]
    rows += [row(8, 1.40, "A"), row(8, 11.40, "B")]
    rows += [row(d, 11.40 + 0.05 * (d - 8), "B") for d in range(9, 16)]
    result = analysis.analyze(rows, DEFAULT)
    assert result["ledger_version"]["isolation"] is None
    assert result["ledger_version"]["handovers"][0]["offset_m"] == 10.0
    # The last actual is 1.75 m on the original scale, not 11.75 m.
    assert result["metrics"][0]["value"] == 1.75
    assert "1 次换表" in result["note"]
    assert "同日双表" in result["note"]


def test_uncalibrated_handover_forecasts_the_earlier_segment_only():
    rows = [row(d, 1.00 + 0.05 * d, "A") for d in range(1, 10)]
    rows += [row(d, 11.00 + 0.05 * d, "B") for d in range(10, 16)]
    result = analysis.analyze(rows, DEFAULT)
    assert result["ledger_version"]["isolation"]["date"] == "2026-11-10"
    assert result["metrics"][0]["detail"] == "2026-11-09"
    assert result["metrics"][0]["value"] == round(1.00 + 0.05 * 9, 6)
    texts = " ".join(section["text"] for section in result["sections"])
    assert "2026-11-10" in texts
    assert result["state"] == "warning"


def test_isolated_readings_never_enter_the_forecast():
    rows = [row(d, 1.00 + 0.05 * d, "A") for d in range(1, 10)]
    rows += [row(d, 19.50, "B") for d in range(10, 16)]
    result = analysis.analyze(rows, DEFAULT)
    forecasts = tables(result)["逐日预报"]
    assert all(item["selected"] < 5 for item in forecasts)


def test_ledger_tables_document_offsets_and_isolation():
    rows = [row(d, 1.00 + 0.05 * d, "A") for d in range(1, 8)]
    rows += [row(8, 1.40, "A"), row(8, 11.40, "B")]
    rows += [row(d, 11.40 + 0.05 * (d - 8), "B") for d in range(9, 16)]
    rows += [row(15, 1.75, "A")]  # a same-day cross-check that agrees
    result = analysis.analyze(rows, DEFAULT)
    handovers = tables(result)["换表交接偏差"]
    assert len(handovers) == 1
    assert handovers[0]["offset_m"] == 10.0
    assert handovers[0]["dual_days"] == 2  # handover day plus the day-15 cross-check
    records = tables(result)["台账折算明细"]
    assert any(item["state"] == "重叠读数（不采用）" for item in records)
    states = {item["state"] for item in tables(result)["台账分段"]}
    assert states == {"可预报"}


def test_a_missing_day_still_resets_to_the_last_contiguous_segment():
    rows = [row(d, 1.00 + 0.05 * d, "A") for d in range(1, 16) if d != 5]
    result = analysis.analyze(rows, DEFAULT)
    assert result["metrics"][0]["detail"] == "2026-11-15"
    metrics = {item["label"]: item for item in result["metrics"]}
    assert metrics["缺测天数"]["value"] == 1
    assert metrics["换表次数"]["value"] == 0


collect(globals())
