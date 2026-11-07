"""백그라운드 수집 스레드 테스트 (네트워크·실제 DB 미사용, get_current_weather 는 mock).

실행: python -m unittest discover -s tests -v
"""

import asyncio
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backend.main as m  # noqa: E402

INTERVAL = 0.05


def kma_weather(source="기상청 초단기실황"):
    return m.WeatherData(location="Seoul", temperature=8.9, source=source,
                         observed_at="2026-11-07 14:00", nx=m.SEOUL_NX, ny=m.SEOUL_NY)


def wait_until(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def collector_threads():
    return [t for t in threading.enumerate() if t.name == "kma-collector" and t.is_alive()]


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.status_patch = mock.patch.dict(m.COLLECTOR_STATUS, {
            "enabled": False, "interval_sec": None, "runs": 0, "last_run": None,
            "last_source": None, "consecutive_failures": 0, "last_error": None})
        self.status_patch.start()
        m.LOGS.clear()

    def tearDown(self):
        m.stop_collector(timeout=2)
        self.status_patch.stop()
        self.assertEqual(collector_threads(), [], "수집 스레드가 남아 있음")

    def test_loop_runs_repeatedly_and_health_updates(self):
        with mock.patch.object(m, "get_current_weather", return_value=kma_weather()) as g:
            self.assertTrue(m.start_collector(INTERVAL))
            self.assertTrue(wait_until(lambda: m.collector_status()["runs"] >= 3))
            health = m.health()
        self.assertGreaterEqual(g.call_count, 3)
        for key in ("status", "time", "kma_key"):
            self.assertIn(key, health)  # 기존 키 유지
        c = health["collector"]
        self.assertTrue(c["enabled"])
        self.assertEqual(c["interval_sec"], INTERVAL)
        self.assertGreaterEqual(c["runs"], 3)
        self.assertEqual(c["last_source"], "기상청 초단기실황")
        self.assertEqual(c["consecutive_failures"], 0)
        self.assertIsNotNone(c["last_run"])

    def test_exceptions_do_not_kill_thread(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] <= 3:
                raise RuntimeError("boom url?serviceKey=SECRETKEY123&nx=60")
            return kma_weather()

        with mock.patch.object(m, "get_current_weather", side_effect=flaky):
            m.start_collector(INTERVAL)
            self.assertTrue(wait_until(lambda: m.collector_status()["consecutive_failures"] >= 3))
            self.assertTrue(collector_threads(), "예외 후 스레드가 죽음")
            self.assertTrue(wait_until(lambda: m.collector_status()["last_source"] == "기상청 초단기실황"))
        status = m.collector_status()
        self.assertEqual(status["consecutive_failures"], 0)
        self.assertGreaterEqual(status["runs"], 4)
        errors = [l["message"] for l in m.LOGS if l["level"] == "ERROR"]
        self.assertGreaterEqual(len(errors), 3)
        self.assertTrue(all("SECRETKEY123" not in e and "serviceKey=***" in e for e in errors))

    def test_fallback_source_counts_as_failure(self):
        with mock.patch.object(m, "get_current_weather", return_value=kma_weather(m.KMA_CACHE_SOURCE)):
            m.start_collector(INTERVAL)
            self.assertTrue(wait_until(lambda: m.collector_status()["consecutive_failures"] >= 2))
        self.assertEqual(m.collector_status()["last_source"], m.KMA_CACHE_SOURCE)

    def test_stop_event_stops_loop(self):
        with mock.patch.object(m, "get_current_weather", return_value=kma_weather()):
            m.start_collector(INTERVAL)
            self.assertTrue(wait_until(lambda: m.collector_status()["runs"] >= 2))
            m.stop_collector(timeout=2)
            self.assertEqual(collector_threads(), [])
            runs = m.collector_status()["runs"]
            time.sleep(INTERVAL * 4)
            self.assertEqual(m.collector_status()["runs"], runs)
        self.assertFalse(m.collector_status()["enabled"])

    def test_single_thread_guard(self):
        with mock.patch.object(m, "get_current_weather", return_value=kma_weather()):
            self.assertTrue(m.start_collector(INTERVAL))
            self.assertFalse(m.start_collector(INTERVAL))
            self.assertEqual(len(collector_threads()), 1)

    def test_lifespan_respects_env(self):
        async def run_lifespan(check):
            async with m.lifespan(m.app):
                check()

        with mock.patch.object(m, "get_current_weather", return_value=kma_weather()):
            with mock.patch.dict(os.environ, {"COLLECT_ENABLED": "0"}):
                asyncio.run(run_lifespan(lambda: self.assertEqual(collector_threads(), [])))

            def started():
                self.assertTrue(wait_until(lambda: m.collector_status()["runs"] >= 2))
                self.assertEqual(len(collector_threads()), 1)

            with mock.patch.dict(os.environ, {"COLLECT_ENABLED": "1", "COLLECT_INTERVAL_SEC": str(INTERVAL)}):
                asyncio.run(run_lifespan(started))
        self.assertEqual(collector_threads(), [])  # lifespan 종료 시 정지

    def test_interval_from_env(self):
        with mock.patch.dict(os.environ, {"COLLECT_INTERVAL_SEC": "15"}):
            self.assertEqual(m.collect_interval_from_env(), 15.0)
        with mock.patch.dict(os.environ, {"COLLECT_INTERVAL_SEC": "abc"}):
            self.assertEqual(m.collect_interval_from_env(), m.COLLECT_INTERVAL_DEFAULT)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COLLECT_INTERVAL_SEC", None)
            self.assertEqual(m.collect_interval_from_env(), 600.0)


if __name__ == "__main__":
    unittest.main()
