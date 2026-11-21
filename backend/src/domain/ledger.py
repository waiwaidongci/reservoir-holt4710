"""Meter handover calibration for the daily reservoir ledger.

A batch may contain more than one meter.  On a handover day the crew records
both the outgoing and the incoming meter, so the difference between the two
same-day readings is the scale offset of the new meter.  Every later reading is
shifted back by the accumulated offset, so the whole series lives on the first
meter's scale and a meter change never looks like a sudden rise.

Rules:

* A handover is the first day a meter not seen before appears.  If the outgoing
  meter is read that day as well, the pair is a same-day dual reading.  Any
  later day on which both meters are read is a consistency check.
* A handover with no same-day dual reading, or whose dual readings disagree by
  more than ``HANDOVER_TOLERANCE_M``, cannot be folded back.  The chain stops on
  that handover date and everything from there on is isolated; the earlier
  contiguous segment is still usable for forecasting.
* A blank ``meter_id`` means "the meter in use at that time", so old
  single-meter ledgers keep working unchanged and a handover day may be written
  as one blank row (the outgoing meter) plus one row naming the new meter.
* Readings are merged by ``(reading_date, meter_id)`` so replaying a batch adds
  nothing, and a late reading simply rebuilds the affected segments.
"""

import hashlib
import json
from datetime import date, timedelta

# Readings are written to 0.01 m.  Dual readings taken on different days may
# differ by one last digit without the meter being considered inconsistent.
HANDOVER_TOLERANCE_M = 0.01
_EPSILON = 1e-12
IMPLICIT_METER = "未标注"
_DAY = timedelta(days=1)

# Per-record states.
USED = "used"
OVERLAP = "overlap"
ISOLATED = "isolated"

_MISSING_DUAL = "该次换表缺少同日双表读数，无法计算刻度偏差"
_INCONSISTENT = "各日双表读数算出的刻度偏差不一致"
_AMBIGUOUS_START = "首日双表读数无法判断原液位计，台账整体隔离"
_NO_SUCCESSOR = "旧液位计停止读数，但没有可用于折算的新液位计读数"
_MULTIPLE_NEW = "同日出现多座未登记的新液位计，无法确定交接顺序"


def fingerprint(rows):
    """Content fingerprint of a canonical ledger entry set."""
    raw = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def meter_name(meter):
    return meter or IMPLICIT_METER


def _meter_key(value, index):
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise ValueError(f"第 {index} 条晨检读数的液位计编号无效，请填写不超过 40 个字符的文本。")
    value = value.strip()
    if len(value) > 40:
        raise ValueError(f"第 {index} 条晨检读数的液位计编号无效，请填写不超过 40 个字符的文本。")
    return value


def normalize_rows(rows):
    """Validate rows and return ``(reservoir_id, entries)`` in calendar order.

    ``entries`` is a list of ``{date, meter, meter_key, level}``.  A date may
    appear twice, which is how a same-day dual reading is written.
    """
    entries = []
    reservoir_id = None
    seen = set()
    for index, row in enumerate(rows, start=1):
        try:
            pool = row["reservoir_id"].strip()
            day = date.fromisoformat(row["reading_date"])
            level = row["level_m"]
            if not pool or len(pool) > 40 or isinstance(level, bool) or not isinstance(level, (int, float)):
                raise ValueError
            if not 0 <= level <= 20:
                raise ValueError
        except (AttributeError, KeyError, TypeError, ValueError):
            raise ValueError(f"第 {index} 条晨检读数无效，请检查水池编号、日期和 0–20 m 液位。") from None
        meter = _meter_key(row.get("meter_id"), index)
        if reservoir_id is not None and pool != reservoir_id:
            raise ValueError("同一批次只能预测一座水池，请按水池编号拆分 CSV。")
        if (day, meter) in seen:
            label = f"液位计 {meter}" if meter else "未标注液位计"
            raise ValueError(f"{day} 的{label}存在重复晨检读数，请保留当天唯一的有效读数。")
        seen.add((day, meter))
        reservoir_id = pool
        entries.append({"date": day, "meter": meter or None, "meter_key": meter, "level": float(level)})
    if not entries:
        raise ValueError("缺少晨检读数，请先导入单池日液位台账。")
    per_day = {}
    for entry in entries:
        per_day.setdefault(entry["date"], []).append(entry)
    for day, items in per_day.items():
        if len(items) > 2:
            raise ValueError(f"{day} 有 {len(items)} 条读数；同一天最多记录新旧两座液位计，请核对换表交接记录。")
    entries.sort(key=lambda entry: (entry["date"], entry["meter_key"]))
    return reservoir_id, entries


def merge_rows(*batches):
    """Idempotently merge rows from several batches of one reservoir.

    A key is ``(reading_date, meter_id)``.  Replaying the same batch therefore
    adds nothing.  A different level under an existing key is ambiguous and is
    rejected instead of silently double-booking the day.
    """
    merged = {}
    reservoir_id = None
    for rows in batches:
        if not rows:
            continue
        pool, entries = normalize_rows(rows)
        if reservoir_id is not None and pool != reservoir_id:
            raise ValueError("台账只能合并同一座水池的读数，请按水池编号拆分。")
        reservoir_id = pool
        for entry in entries:
            key = (entry["date"], entry["meter_key"])
            previous = merged.get(key)
            if previous is not None and abs(previous["level"] - entry["level"]) > _EPSILON:
                day = entry["date"].isoformat()
                raise ValueError(f"{day} 的液位计 {meter_name(entry['meter'])} 已有不同读数，请核对补录数据后再提交。")
            merged[key] = entry
    if not merged:
        raise ValueError("缺少晨检读数，请先导入单池日液位台账。")
    return reservoir_id, [merged[key] for key in sorted(merged)]


def canonical_rows(reservoir_id, entries):
    """Raw rows that rebuild exactly this entry set (used for snapshots)."""
    return [
        {
            "reservoir_id": reservoir_id,
            "reading_date": entry["date"].isoformat(),
            "level_m": entry["level"],
            "meter_id": entry["meter_key"],
        }
        for entry in sorted(entries, key=lambda item: (item["date"], item["meter_key"]))
    ]


def _starting_meter(days, by_day):
    """Meter the series starts on, or ``None`` when the first day is ambiguous.

    A blank first-day row is the outgoing meter.  With two *named* meters on the
    first day, the one that is no longer read on the earliest later day where
    only one of them survives is the outgoing meter.
    """
    first = sorted(by_day[days[0]])
    if len(first) == 1:
        return first[0]
    if "" in first:
        return ""
    for day in days[1:]:
        present = set(by_day[day])
        surviving = [key for key in first if key in present]
        if len(surviving) == 1:
            return next(key for key in first if key != surviving[0])
        if not surviving:
            return None
    return None


def _walk(days, by_day, start):
    """Resolve each raw row to the meter it belongs to and find the handovers.

    Returns ``(handovers, plan, break_day, break_reason)``.  ``plan[day]`` is a
    list of ``(raw_key, resolved_key, entry, order)`` where ``order`` is 0 for the
    row that represents the active meter and 1 for a redundant/retired row.
    """
    handovers = []
    plan = {}
    if start is None:
        return handovers, plan, days[0], _AMBIGUOUS_START

    chain = {start}
    current = start
    break_day = None
    break_reason = None
    for day in days:
        resolved = {}
        conflict = None
        for key in sorted(by_day[day]):
            resolved_key = current if key == "" else key
            if resolved_key in resolved:
                conflict = resolved_key
            resolved[resolved_key] = by_day[day][key]
        rows = []
        for key in sorted(by_day[day]):
            resolved_key = current if key == "" else key
            rows.append((key, resolved_key, by_day[day][key]))
        plan[day] = rows
        if break_day is not None:
            continue
        if conflict is not None:
            raise ValueError(
                f"{day} 同时有未标注液位计的读数与液位计 {meter_name(conflict)} 的读数；"
                "同一天只有换表交接才录两条，请补全旧表编号或删去重复读数。")
        present = set(resolved)
        if present == {current}:
            continue
        unseen = present - chain
        handover = None
        if len(unseen) > 1:
            break_day, break_reason = day, _MULTIPLE_NEW
        elif len(unseen) == 1:
            new = next(iter(unseen))
            handover = {
                "date": day, "old": current, "new": new,
                "same_day_dual": current in present,
                "dual_readings": [], "offset_m": None, "cumulative_offset_m": None,
                "consistent": False, "isolated": True, "reason": None,
            }
            chain.add(new)
            current = new
        elif current not in present:
            # Only retired meters are read today: the active meter has a gap,
            # but there is no new meter to hand over to.
            continue
        if handover is not None:
            handovers.append(handover)

    # Every day on which both the outgoing and the incoming meter are read is a
    # same-day dual reading for that handover, including the handover day and
    # any late cross-check that arrived out of order.
    position = {day: index for index, day in enumerate(days)}
    for handover in handovers:
        for day in days[position[handover["date"]]:]:
            levels = {resolved_key: entry["level"] for _raw, resolved_key, entry in plan.get(day, [])}
            if handover["old"] in levels and handover["new"] in levels:
                old_level = levels[handover["old"]]
                new_level = levels[handover["new"]]
                handover["dual_readings"].append({
                    "date": day, "old_level_m": old_level, "new_level_m": new_level,
                    "offset_m": round(new_level - old_level, 6),
                })
        offsets = [reading["offset_m"] for reading in handover["dual_readings"]]
        if not offsets:
            handover["reason"] = _MISSING_DUAL
        elif max(offsets) - min(offsets) > HANDOVER_TOLERANCE_M + _EPSILON:
            handover["reason"] = _INCONSISTENT
        if handover["reason"] and (break_day is None or handover["date"] < break_day):
            break_day, break_reason = handover["date"], handover["reason"]
    # Isolation starts on the handover date, so that handover and every later
    # one are marked isolated too.
    if break_day is not None:
        for handover in handovers:
            if handover["date"] >= break_day:
                handover["isolated"] = True
                handover["consistent"] = False
                handover["reason"] = handover["reason"] or break_reason
    return handovers, plan, break_day, break_reason


def _offsets(handovers, start, break_day):
    """Cumulative offset per meter, only while the chain is intact."""
    cumulative = {start: 0.0}
    for handover in handovers:
        if break_day is not None and handover["date"] >= break_day:
            continue
        base = cumulative.get(handover["old"])
        if base is None or handover["reason"]:
            continue
        handover["offset_m"] = handover["dual_readings"][0]["offset_m"]
        handover["cumulative_offset_m"] = round(base + handover["offset_m"], 6)
        handover["consistent"] = True
        handover["isolated"] = False
        cumulative[handover["new"]] = handover["cumulative_offset_m"]
    return cumulative


def _active_by_day(days, handovers, start, break_day):
    """Meter in use on each day (after that day's handover)."""
    active = {}
    current = start
    index = 0
    for day in days:
        while index < len(handovers) and handovers[index]["date"] <= day:
            handover = handovers[index]
            if break_day is None or handover["date"] < break_day:
                current = handover["new"]
            index += 1
        active[day] = current
    return active


def _segments(records, isolation):
    """Contiguous runs of usable days, followed by the isolated tail."""
    segments = []
    start = previous = None
    count = 0
    for record in sorted((item for item in records if item["state"] == USED), key=lambda item: item["date"]):
        day = date.fromisoformat(record["date"])
        if previous is not None and (day - previous).days > 1:
            segments.append({"index": len(segments) + 1, "start": start.isoformat(),
                             "end": previous.isoformat(), "days": count, "state": "usable", "reason": None})
            start = None
        if start is None:
            start, count = day, 0
        count += 1
        previous = day
    if start is not None:
        segments.append({"index": len(segments) + 1, "start": start.isoformat(),
                         "end": previous.isoformat(), "days": count, "state": "usable", "reason": None})
    isolated_days = sorted({record["date"] for record in records if record["state"] == ISOLATED})
    if isolated_days:
        segments.append({"index": len(segments) + 1, "start": isolated_days[0],
                         "end": isolated_days[-1], "days": len(isolated_days),
                         "state": "isolated", "reason": isolation["reason"] if isolation else None})
    return segments


def build_ledger_from_entries(reservoir_id, entries):
    """Fold every reading back to the first meter's scale and segment the result."""
    if not entries:
        raise ValueError("缺少晨检读数，请先导入单池日液位台账。")
    entries = sorted(entries, key=lambda entry: (entry["date"], entry["meter_key"]))
    days = sorted({entry["date"] for entry in entries})
    by_day = {}
    for entry in entries:
        by_day.setdefault(entry["date"], {})[entry["meter_key"]] = entry
    display = {entry["meter_key"]: meter_name(entry["meter"]) for entry in entries}

    start = _starting_meter(days, by_day)
    if start is None:
        # The first day's two meters cannot be ordered, so nothing can be folded
        # back and the whole ledger is isolated.
        records = [{
            "date": day.isoformat(),
            "meter": meter_name(by_day[day][key]["meter"]),
            "raw_level_m": by_day[day][key]["level"],
            "offset_m": None, "level_m": None, "state": ISOLATED,
            "reason": _AMBIGUOUS_START,
        } for day in days for key in sorted(by_day[day])]
        records.sort(key=lambda record: (record["date"], record["meter"]))
        isolation = {"date": days[0].isoformat(), "reason": _AMBIGUOUS_START}
        return {
            "reservoir_id": reservoir_id,
            "checksum": fingerprint(canonical_rows(reservoir_id, entries)),
            "records": records,
            "handovers": [],
            "segments": _segments(records, isolation),
            "gaps": [],
            "isolation": isolation,
            "counts": {"used": 0, "overlap": 0, "isolated": len(records)},
        }
    handovers, plan, break_day, break_reason = _walk(days, by_day, start)
    cumulative = _offsets(handovers, start, break_day) if start is not None else {}
    active = _active_by_day(days, handovers, start, break_day) if start is not None else {}

    records = []
    for day in days:
        for raw_key, resolved_key, entry in plan.get(day, []):
            offset = None
            level = None
            if break_day is not None and day >= break_day:
                state, reason = ISOLATED, break_reason
            elif resolved_key != active.get(day):
                state, reason = OVERLAP, None
            else:
                offset = cumulative.get(resolved_key)
                if offset is None:
                    state, reason = ISOLATED, "液位计刻度未折算"
                else:
                    state, reason = USED, None
                    level = round(entry["level"] - offset, 6)
            records.append({
                "date": day.isoformat(),
                "meter": display.get(raw_key, meter_name(entry["meter"])),
                "raw_level_m": entry["level"],
                "offset_m": offset,
                "level_m": level,
                "state": state,
                "reason": reason,
            })

    records.sort(key=lambda record: (record["date"], record["meter"]))

    gaps = []
    used_days = sorted({date.fromisoformat(record["date"]) for record in records if record["state"] == USED})
    for previous, current in zip(used_days, used_days[1:]):
        missing = (current - previous).days - 1
        if missing:
            gaps.append({"start": (previous + _DAY).isoformat(), "end": (current - _DAY).isoformat(), "days": missing})

    isolation = {"date": break_day.isoformat(), "reason": break_reason} if break_day is not None else None
    output_handovers = []
    for handover in handovers:
        output_handovers.append({
            "date": handover["date"].isoformat(),
            "from_meter": display.get(handover["old"], meter_name(handover["old"])),
            "to_meter": display.get(handover["new"], meter_name(handover["new"])),
            "same_day_dual": handover["same_day_dual"],
            "dual_readings": [{"date": item["date"].isoformat(), "old_level_m": item["old_level_m"],
                               "new_level_m": item["new_level_m"], "offset_m": item["offset_m"]}
                              for item in handover["dual_readings"]],
            "offset_m": handover["offset_m"],
            "cumulative_offset_m": handover["cumulative_offset_m"],
            "consistent": handover["consistent"],
            "isolated": handover["isolated"],
            "reason": handover["reason"],
        })

    return {
        "reservoir_id": reservoir_id,
        "checksum": fingerprint(canonical_rows(reservoir_id, entries)),
        "records": records,
        "handovers": output_handovers,
        "segments": _segments(records, isolation),
        "gaps": gaps,
        "isolation": isolation,
        "counts": {
            "used": sum(record["state"] == USED for record in records),
            "overlap": sum(record["state"] == OVERLAP for record in records),
            "isolated": sum(record["state"] == ISOLATED for record in records),
        },
    }


def build_ledger(rows):
    reservoir_id, entries = normalize_rows(rows)
    return build_ledger_from_entries(reservoir_id, entries)


def ledger_rows(snapshot):
    """Canonical rows that rebuild this exact ledger version."""
    rows = {}
    for record in snapshot["records"]:
        meter = "" if record["meter"] == IMPLICIT_METER else record["meter"]
        rows[(record["date"], meter)] = {
            "reservoir_id": snapshot["reservoir_id"],
            "reading_date": record["date"],
            "level_m": record["raw_level_m"],
            "meter_id": meter,
        }
    return [rows[key] for key in sorted(rows)]


def used_pairs(snapshot):
    """``(date, folded level)`` pairs for the usable daily series."""
    return [(date.fromisoformat(record["date"]), record["level_m"])
            for record in snapshot["records"] if record["state"] == USED]


def affected_segments(before, after):
    """Segments after a re-computation that differ from the previous snapshot."""
    previous = {(segment["start"], segment["end"], segment["state"]): segment
                for segment in before.get("segments", [])}
    return [segment for segment in after.get("segments", [])
            if previous.get((segment["start"], segment["end"], segment["state"])) != segment]
