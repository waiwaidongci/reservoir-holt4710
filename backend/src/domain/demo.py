"""澄湾会展园 RW-02 的固定晨检台账。

11 月 4 日通讯中断，台账留空一天。基础演示保持单表口径，结果与既有流程一致；
换表折算的口径见 ``demo_handover_rows``，它额外记录 11 月 10 日的同日双表读数。
"""


_LEVELS = (0.83, 0.87, 0.89, None, 0.94, 1.02, 1.06, 1.13, 1.23, 1.25,
           1.35, 1.37, 1.44, 1.51, 1.59, 1.65, 1.72, 1.76, 1.83, 1.91)

# 换表示例：11 月 10 日新旧两表各录一条，其后新表读数高 9.97 m。
_HANDOVER_DAY = 10
_HANDOVER_OFFSET_M = 9.97
_OLD_METER = 'LS-01'
_NEW_METER = 'LS-02'


def demo_rows() -> list[dict]:
    return [
        {"reservoir_id": "RW-02", "reading_date": f"2026-11-{day:02d}", "level_m": level}
        for day, level in enumerate(_LEVELS, start=1)
        if level is not None
    ]


def demo_handover_rows() -> list[dict]:
    rows = []
    for day, level in enumerate(_LEVELS, start=1):
        if level is None:
            continue
        if day < _HANDOVER_DAY:
            rows.append({"reservoir_id": "RW-02", "reading_date": f"2026-11-{day:02d}",
                         "level_m": level, "meter_id": _OLD_METER})
        elif day == _HANDOVER_DAY:
            rows.append({"reservoir_id": "RW-02", "reading_date": f"2026-11-{day:02d}",
                         "level_m": level, "meter_id": _OLD_METER})
            rows.append({"reservoir_id": "RW-02", "reading_date": f"2026-11-{day:02d}",
                         "level_m": round(level + _HANDOVER_OFFSET_M, 2), "meter_id": _NEW_METER})
        else:
            rows.append({"reservoir_id": "RW-02", "reading_date": f"2026-11-{day:02d}",
                         "level_m": round(level + _HANDOVER_OFFSET_M, 2), "meter_id": _NEW_METER})
    return rows
