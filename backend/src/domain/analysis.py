"""Chronological reservoir forecasts with separated selection and holdout.

The forecast always runs on one pinned ledger version.  Readings that sit on a
replacement meter are folded back to the original meter's scale first; readings
after an uncalibrated handover are isolated and never enter the model.
"""

from datetime import date, timedelta
from math import isclose, sqrt
from statistics import fmean

from . import ledger as ledger_domain
from .spec import SPEC


_DAY = timedelta(days=1)
_MIN_TRAINING = 3
_MIN_ROLLING = 2
_ALERT_TOLERANCE_M = 1e-12
_MODELS = {"holt": "Holt 趋势", "level_only": "仅水平"}
# Prefer the simpler model when the errors are equal.
_SELECTION_ORDER = ("level_only", "holt")
_PREDICTIONS = (("holt", "Holt 趋势（m）"), ("level_only", "仅水平（m）"))
_EVALUATION = (("date", "实测日期"), ("step", "提前天数"), ("actual", "实测（m）")) + _PREDICTIONS
_STATES = {"used": "采用", "overlap": "重叠读数（不采用）", "isolated": "隔离（不采用）"}


def validate_rows(rows):
    """Validate a single pool's daily readings and return them in date order."""
    reservoir_id, entries = ledger_domain.normalize_rows(rows)
    readings = {}
    for entry in entries:
        readings.setdefault(entry["date"], entry["level"])
    return reservoir_id, sorted(readings.items())


def _options(params):
    fields = {field["key"]: field for field in SPEC["params"]}
    if set(params) - fields.keys():
        raise ValueError("存在未知预测参数，请重新打开预测设置。")
    values = {}
    for key, field in fields.items():
        value = params.get(key, field["default"])
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not field["min"] <= value <= field["max"]
                or (field["type"] == "int" and not isinstance(value, int))):
            raise ValueError(f'{field["label"]}必须为 {field["min"]}–{field["max"]} 范围内的有效值。')
        values[key] = value
    return values


def _recent_segment(readings):
    """Last contiguous run of usable days; gaps reset both models."""
    start = 0
    for index, ((previous, _), (current, _)) in enumerate(zip(readings, readings[1:]), start=1):
        if (current - previous).days > 1:
            start = index
    return readings[start:]


class _Models:
    def __init__(self, first, second, alpha, beta):
        self.alpha = alpha
        self.beta = beta
        self.holt_level = second
        self.trend = second - first
        self.level_only = first + alpha * (second - first)

    def predict(self, step):
        return {"holt": max(0.0, self.holt_level + step * self.trend), "level_only": self.level_only}

    def update(self, actual):
        previous_level = self.holt_level
        projected_level = previous_level + self.trend
        self.holt_level = projected_level + self.alpha * (actual - projected_level)
        self.trend += self.beta * (self.holt_level - previous_level - self.trend)
        self.level_only += self.alpha * (actual - self.level_only)


def _evaluation_row(models, reading, step):
    day, actual = reading
    return {"date": day.isoformat(), "step": step, "actual": actual, **models.predict(step)}


def _errors(rows, model):
    errors = [abs(row[model] - row["actual"]) for row in rows]
    return fmean(errors), sqrt(fmean(error * error for error in errors))


def _table(title, columns, rows):
    return {"title": title, "columns": [{"key": key, "label": label} for key, label in columns], "rows": rows}


def _chart(title, points):
    return {"title": title, "kind": "line", "series": [{"label": day, "value": value} for day, value in points]}


def _ledger_tables(snapshot):
    handover_rows = [
        {
            "date": handover["date"],
            "from_meter": handover["from_meter"],
            "to_meter": handover["to_meter"],
            "same_day_dual": handover["same_day_dual"],
            "dual_days": len(handover["dual_readings"]),
            "offset_m": handover["offset_m"],
            "cumulative_offset_m": handover["cumulative_offset_m"],
            "consistent": handover["consistent"],
            "isolated": handover["isolated"],
            "reason": handover["reason"],
        }
        for handover in snapshot["handovers"]
    ]
    dual_rows = [
        {
            "handover_date": handover["date"],
            "reading_date": reading["date"],
            "from_meter": handover["from_meter"],
            "to_meter": handover["to_meter"],
            "old_level_m": reading["old_level_m"],
            "new_level_m": reading["new_level_m"],
            "offset_m": reading["offset_m"],
        }
        for handover in snapshot["handovers"]
        for reading in handover["dual_readings"]
    ]
    record_rows = [
        {
            "date": record["date"],
            "meter": record["meter"],
            "raw_level_m": record["raw_level_m"],
            "offset_m": record["offset_m"],
            "level_m": record["level_m"],
            "state": _STATES.get(record["state"], record["state"]),
            "reason": record["reason"],
        }
        for record in snapshot["records"]
    ]
    segment_rows = [
        {
            "index": segment["index"],
            "start": segment["start"],
            "end": segment["end"],
            "days": segment["days"],
            "state": "可预报" if segment["state"] == "usable" else "隔离",
            "reason": segment["reason"],
        }
        for segment in snapshot["segments"]
    ]
    return [
        _table("换表交接偏差", (("date", "换表日期"), ("from_meter", "原液位计"), ("to_meter", "新液位计"),
               ("same_day_dual", "换表日双表"), ("dual_days", "双表读数天数"), ("offset_m", "新表偏差（m）"),
               ("cumulative_offset_m", "累计折算（m）"), ("consistent", "偏差一致"), ("isolated", "已隔离"),
               ("reason", "说明")), handover_rows),
        _table("换表双表读数", (("handover_date", "换表日期"), ("reading_date", "双表日期"),
               ("from_meter", "原液位计"), ("to_meter", "新液位计"), ("old_level_m", "原表读数（m）"),
               ("new_level_m", "新表读数（m）"), ("offset_m", "新表偏差（m）")), dual_rows),
        _table("台账折算明细", (("date", "晨检日期"), ("meter", "液位计"), ("raw_level_m", "原始读数（m）"),
               ("offset_m", "折算偏差（m）"), ("level_m", "折回原刻度（m）"), ("state", "采用情况"),
               ("reason", "说明")), record_rows),
        _table("台账分段", (("index", "段号"), ("start", "起始日"), ("end", "截止日"), ("days", "天数"),
               ("state", "状态"), ("reason", "说明")), segment_rows),
    ]


def _ledger_metrics(snapshot):
    handovers = snapshot["handovers"]
    calibrated = [handover for handover in handovers if handover["consistent"]]
    return [
        {"label": "换表次数", "value": len(handovers),
         "detail": f"{len(calibrated)} 次已折算，{len(handovers) - len(calibrated)} 次隔离"},
        {"label": "累计折算", "value": calibrated[-1]["cumulative_offset_m"] if calibrated else 0.0,
         "detail": "最后一次换表后的累计偏差（m）"},
        {"label": "隔离读数", "value": snapshot["counts"]["isolated"],
         "detail": snapshot["isolation"]["date"] if snapshot["isolation"] else "没有隔离"},
    ]


def analyze_snapshot(snapshot, normalized_params):
    """Forecast the last usable segment of a pinned ledger snapshot."""
    readings = ledger_domain.used_pairs(snapshot)
    if not readings:
        raise ValueError("台账在换表后没有可折回原刻度的读数，无法预报；请先补录同日双表读数。")
    options = _options(normalized_params)
    recent = _recent_segment(readings)
    holdout_days = options["holdout_days"]
    horizon = options["forecast_days"]
    threshold = options["alert_level_m"]
    required = _MIN_TRAINING + _MIN_ROLLING + holdout_days
    if len(recent) < required:
        start = recent[0][0]
        raise ValueError(f"从 {start} 起仅有 {len(recent)} 条连续日读数，至少需要 {required} 条才能完成滚动验证和留出评估。请补录缺测或缩短留出期。")
    last_day, last_level = recent[-1]
    if (date.max - last_day).days < horizon:
        raise ValueError("预报日期超出日历范围，请缩短未来预报天数。")

    selection_rows = recent[:-holdout_days]
    holdout_rows = recent[-holdout_days:]
    models = _Models(recent[0][1], recent[1][1], options["alpha"], options["beta"])
    for _, actual in selection_rows[2:_MIN_TRAINING]:
        models.update(actual)
    rolling = []
    for reading in selection_rows[_MIN_TRAINING:]:
        rolling.append(_evaluation_row(models, reading, 1))
        models.update(reading[1])
    rolling_scores = {model: _errors(rolling, model) for model in _MODELS}
    winner = min(_SELECTION_ORDER, key=lambda model: rolling_scores[model][0])
    holdout = [_evaluation_row(models, reading, step) for step, reading in enumerate(holdout_rows, start=1)]
    holdout_scores = {model: _errors(holdout, model) for model in _MODELS}
    for _, actual in holdout_rows:
        models.update(actual)

    forecasts = []
    for step in range(1, horizon + 1):
        day = (last_day + step * _DAY).isoformat()
        estimates = models.predict(step)
        selected = estimates[winner]
        reaches_alert = selected >= threshold or isclose(selected, threshold, rel_tol=0, abs_tol=_ALERT_TOLERANCE_M)
        forecasts.append({"date": day, "step": step, **estimates, "selected": selected, "reaches_alert": reaches_alert})
    if last_level >= threshold:
        trigger = last_day.isoformat()
        headline = f"末日实测已达预警线 · {trigger}"
    else:
        trigger = next((row["date"] for row in forecasts if row["reaches_alert"]), None)
        headline = f"预计 {trigger} 首次达到高位预警线" if trigger else f"未来 {horizon} 天预计未触线"

    comparison = [
        {"model": label, "rolling_mae": rolling_scores[model][0], "rolling_rmse": rolling_scores[model][1],
         "holdout_mae": holdout_scores[model][0], "holdout_rmse": holdout_scores[model][1], "selected": model == winner}
        for model, label in _MODELS.items()
    ]
    handovers = snapshot["handovers"]
    calibrated = [handover for handover in handovers if handover["consistent"]]
    if calibrated:
        fold_note = f"台账含 {len(handovers)} 次换表，其中 {len(calibrated)} 次按同日双表读数折算回原刻度"
    else:
        fold_note = "台账没有需要折算的换表记录"
    isolation = snapshot["isolation"]

    tables = [
        _table("逐日预报", (("date", "预报日期"), ("step", "提前天数")) + _PREDICTIONS
               + (("selected", "采用预报（m）"), ("reaches_alert", "达到预警线")), forecasts),
        _table("模型误差对照", (("model", "模型"), ("rolling_mae", "滚动 MAE（m）"),
               ("rolling_rmse", "滚动 RMSE（m）"), ("holdout_mae", "留出 MAE（m）"),
               ("holdout_rmse", "留出 RMSE（m）"), ("selected", "采用")), comparison),
        _table("滚动单步验证", _EVALUATION, rolling),
        _table("固定起点留出评估", _EVALUATION, holdout),
        _table("日读数缺测区间", (("start", "缺测首日"), ("end", "缺测末日"), ("days", "缺测天数")), snapshot["gaps"]),
        *_ledger_tables(snapshot),
    ]
    metrics = [
        {"label": "末日实测（m）", "value": last_level, "detail": last_day.isoformat()},
        {"label": "预警线（m）", "value": threshold, "detail": "达到预警线即计入"},
        {"label": "首次触线日期", "value": trigger or "窗口内未触线", "detail": "末日实测" if last_level >= threshold else "采用模型预报"},
        {"label": "滚动验证天数", "value": len(rolling), "detail": f'{rolling[0]["date"]} 至 {rolling[-1]["date"]}'},
        {"label": "留出评估天数", "value": holdout_days, "detail": f'{holdout[0]["date"]} 至 {holdout[-1]["date"]}'},
        {"label": "缺测天数", "value": sum(gap["days"] for gap in snapshot["gaps"]), "detail": f"共 {len(snapshot['gaps'])} 个缺测区间"},
        *_ledger_metrics(snapshot),
    ]
    sections = [
        {"title": "台账版本", "text": (f"本次预报固定使用台账版本指纹 {snapshot['checksum'][:12]}…，"
                                      f"共 {snapshot['counts']['used']} 条折算后读数进入模型。"
                                      "后续补录会生成新版本并重算受影响的后续分段，本报告仍保留当时的读数与偏差。")},
        {"title": "评估时间边界", "text": f"模型仅按留出前的滚动 MAE 选择。留出预测统一以 {selection_rows[-1][0]} 为起点。留出评估结束后，将这些实测纳入未来预报。"},
        {"title": "换表折算", "text": f"{fold_note}。新表偏差取同日双表的读数差；不同日期的双表读数偏差需在 0.01 m 内一致，否则不采用。"},
        {"title": "隔离处理", "text": (f"从 {isolation['date']} 起读数已隔离，不进入预报：{isolation['reason']}。隔离前的连续段仍可预报。"
                                      if isolation else "所有换表均有同日双表读数可折算，没有隔离读数。")},
        {"title": "缺测处理", "text": "缺测中断日序列。两模型均使用最后一段连续日读数，缺测区间见下表。"},
    ]
    return {
        "state": "warning" if trigger or snapshot["gaps"] or isolation else "ok",
        "headline": headline,
        "note": (f"{snapshot['reservoir_id']} 使用 {recent[0][0]} 至 {last_day} 的 {len(recent)} 条连续晨检读数，"
                 f"预报采用{_MODELS[winner]}模型；{fold_note}。"),
        "metrics": metrics,
        "tables": tables,
        "charts": [
            _chart("连续段实测液位（m，最近 30 天）", ((day.isoformat(), level) for day, level in recent[-30:])),
            _chart(f"{_MODELS[winner]}逐日预报（m）", ((row["date"], row["selected"]) for row in forecasts)),
        ],
        "sections": sections,
        "ledger_version": {
            "checksum": snapshot["checksum"],
            "reservoir_id": snapshot["reservoir_id"],
            "segments": snapshot["segments"],
            "handovers": handovers,
            "isolation": isolation,
            "counts": snapshot["counts"],
        },
    }


def analyze(rows, normalized_params):
    """Backward-compatible entry point: build a ledger from raw rows, then forecast."""
    return analyze_snapshot(ledger_domain.build_ledger(rows), normalized_params)
