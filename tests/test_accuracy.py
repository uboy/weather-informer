#!/usr/bin/env python3
"""Векторы accuracy_query: lead в ЧАСАХ (регрессия: считали в днях — всегда 0 сэмплов),
окно window ловит METAR :30, регистронезависимость provider, phys-фильтр, negative-lead отсев."""

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from caching_server import accuracy_query, _history_conn
import caching_server as cs

FORECAST_DB_ORIG = cs.FORECAST_DB


class AccuracyTests(unittest.TestCase):
    def setUp(self):
        # подменяем БД на in-memory нельзя (connect по пути) — временная копия
        import tempfile, os
        self.tmp = tempfile.mktemp(suffix=".db")
        cs.FORECAST_DB = self.tmp
        self._recreate_tables()

    def tearDown(self):
        import os
        cs.FORECAST_DB = FORECAST_DB_ORIG
        try:
            os.remove(self.tmp)
        except OSError:
            pass

    def _recreate_tables(self):
        conn = _history_conn()
        conn.execute("DELETE FROM forecast_points")
        conn.execute("DELETE FROM observations")
        conn.commit()
        conn.close()

    def _add(self, created_off_h, valid_off_h, temp_f=20.0, provider="Test", phys="27459", obs_temp=18.0, obs_at=None):
        now = datetime.now()
        conn = _history_conn()
        conn.execute("INSERT INTO forecast_points VALUES (?,?,?,?,?,?,?,?,?)",
                     ((now + timedelta(hours=created_off_h)).isoformat(timespec="seconds"),
                      provider, "56.32:44.0",
                      (now + timedelta(hours=valid_off_h)).isoformat(timespec="seconds"),
                      temp_f, None, None, None, None))
        if obs_at is not None:
            conn.execute("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?)",
                         ((now + timedelta(hours=obs_at)).isoformat(timespec="seconds"),
                          "S1", "test-obs", obs_temp, None, None, None, phys))
        conn.commit()
        conn.close()

    def test_lead_hours_not_days(self):
        # прогноз +3ч против наблюдения в +3ч: lead=3 должен ловить (регрессия «дней вместо часов»)
        self._add(0, 3, obs_at=3, obs_temp=18.0)
        mae, n = accuracy_query("Test", lead=3, days=1, phys="27459", window=1.5)
        self.assertEqual(n, 1, "lead=3ч обязан находить пару +3ч")
        self.assertEqual(mae, 2.0)

    def test_lead_24_not_caught_by_short_lead(self):
        self._add(0, 24, obs_at=24)
        _, n3 = accuracy_query("Test", lead=3, days=2, phys="27459", window=1.5)
        _, n24 = accuracy_query("Test", lead=24, days=2, phys="27459", window=1.5)
        self.assertEqual(n3, 0)
        self.assertEqual(n24, 1, "lead=24 должен ловить +24ч пару")

    def test_window_catches_half_hour_obs(self):
        # наблюдение на :30 (METAR) — равенство часов не совпадёт, окно ±1.5ч ловит
        now = datetime.now()
        conn = _history_conn()
        conn.execute("INSERT INTO forecast_points VALUES (?,?,?,?,?,?,?,?,?)",
                     (now.isoformat(timespec="seconds"), "Test", "k",
                      (now + timedelta(hours=1)).isoformat(timespec="seconds"), 20.0, None, None, None, None))
        conn.execute("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?)",
                     ((now + timedelta(hours=1, minutes=30)).isoformat(timespec="seconds"),
                      "S2", "metar", 19.0, None, None, None, "27459"))
        conn.commit()
        conn.close()
        mae, n = accuracy_query("Test", lead=1, days=1, phys="27459", window=1.5)
        self.assertEqual(n, 1, "окно ±1.5ч обязано ловить :30-наблюдение")

    def test_provider_case_insensitive(self):
        self._add(0, 1, provider="Yandex", obs_at=1)
        _, n1 = accuracy_query("yandex", lead=1, days=1, phys="27459", window=1.5)
        _, n2 = accuracy_query("YANDEX", lead=1, days=1, phys="27459", window=1.5)
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 1)

    def test_phys_filter(self):
        self._add(0, 1, obs_at=1, phys="STRIGINO")
        _, n_other = accuracy_query("Test", lead=1, days=1, phys="27459", window=1.5)
        _, n_all = accuracy_query("Test", lead=1, days=1, phys="", window=1.5)
        self.assertEqual(n_other, 0, "phys=27459 не должен видеть STRIGINO")
        self.assertEqual(n_all, 1)

    def test_negative_lead_excluded(self):
        # прошедший час (valid_at < created): окна lead>=0 его отсекают
        self._add(0, -2, obs_at=-2)
        _, n = accuracy_query("Test", lead=1, days=1, phys="27459", window=1.5)
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
