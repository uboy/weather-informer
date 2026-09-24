#!/usr/bin/env python3
"""
Отчёт точности прогнозов по weather_stats.csv.

Сравнивает прогнозы источников (+3/+6 ч) с фактической температурой.
Эталон факта: Yandex (основной источник), при отсутствии — медиана фактов всех источников в тот час.

Запуск: python3 stats_report.py [путь к weather_stats.csv]
"""
import csv
import sys
import statistics
from collections import defaultdict
from datetime import datetime

DEFAULT = "weather_stats.csv"


def load(path):
    actuals = defaultdict(dict)   # target_iso -> {source: temp}
    forecasts = []                # (ts, source, target_iso, temp)
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                ts = datetime.fromisoformat(row["ts"])
                tgt = datetime.fromisoformat(row["target"])
                t = float(row["temp_c"])
            except (ValueError, KeyError):
                continue
            if row["kind"] == "actual":
                actuals[row["target"]][row["source"]] = t
            else:
                forecasts.append((ts, row["source"], row["target"], t))
    return actuals, forecasts


def truth(actuals_for_target):
    vals = list(actuals_for_target.values())
    if "Yandex" in actuals_for_target:
        return actuals_for_target["Yandex"], "Yandex"
    if vals:
        return statistics.median(vals), "median(all)"
    return None, None


def main(path):
    actuals, forecasts = load(path)
    if not forecasts:
        print("Нет прогнозов в", path)
        return
    err = defaultdict(list)  # (source, horizon_h) -> [errors]
    min_ts = min(f[0] for f in forecasts)
    for ts, source, target, temp in forecasts:
        t, ref = truth(actuals.get(target, {}))
        if t is None:
            continue
        horizon = round((datetime.fromisoformat(target) - ts).total_seconds() / 3600)
        err[(source, horizon)].append(abs(temp - t))
    print("Эталон факта: Yandex, иначе медиана всех источников\n")
    print(f"{'источник':<12} {'горизонт':>9} {'N':>4} {'MAE °C':>8}")
    for (source, horizon), errs in sorted(err.items(), key=lambda x: (x[0][0], x[0][1])):
        print(f"{source:<12} +{horizon:<8} {len(errs):>4} {statistics.mean(errs):>8.2f}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT)
