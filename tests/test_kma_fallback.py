"""기상청(KMA) 예외 처리·캐시 폴백 테스트 (네트워크·실제 DB 미사용).

실행: python -m unittest discover -s tests -v
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 실제 weather_logs.db 를 건드리지 않도록 backend.main 임포트 전에 임시 DB 경로 지정
import atexit, tempfile  # noqa: E401,E402
_TEST_DB = Path(tempfile.gettempdir()) / f"myaisky_test_{os.getpid()}.db"
os.environ["MYAISKY_DB_PATH"] = str(_TEST_DB)
atexit.register(lambda: _TEST_DB.unlink(missing_ok=True))

import backend.main as m  # noqa: E402

FAKE_KEY = "FAKE/test+key=="
KMA_PREFIX = m.KMA_BASE_URL


class FakeResp:
    def __init__(self, status: int = 200, json_data=None, text: str = ""):
        self.status_code = status
        self._json = json_data
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} Client Error for url: {KMA_PREFIX}/x?serviceKey={FAKE_KEY}&nx=60")


def kma_ok_ncst(t1h="8.9", pty="0"):
    items = [{"category": "T1H", "obsrValue": t1h}, {"category": "REH", "obsrValue": "22"},
             {"category": "WSD", "obsrValue": "4.5"}, {"category": "PTY", "obsrValue": pty}]
    return {"response": {"header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"},
                         "body": {"items": {"item": items}}}}


def kma_ok_fcst():
    items = [{"category": "SKY", "fcstDate": "20261107", "fcstTime": "1500", "fcstValue": "1"}]
    return {"response": {"header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"},
                         "body": {"items": {"item": items}}}}


OPEN_METEO = {"current": {"temperature_2m": 10.0, "relative_humidity_2m": 50, "weather_code": 0,
                          "wind_speed_10m": 1.0, "time": "2026-11-07T14:00"},
              "hourly": {"temperature_2m": [9.0, 10.0], "time": ["2026-11-07T00:00", "2026-11-07T01:00"]}}

XML_AUTH = ("<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR</errMsg>"
            "<returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg>"
            "<returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>")


def router(kma=None, open_meteo=None):
    """URL 에 따라 KMA / Open-Meteo 응답을 돌려주는 HTTP.get(재시도 세션) 대체 함수."""
    def fake_get(url, params=None, timeout=None):
        handler = kma if url.startswith(KMA_PREFIX) else open_meteo
        if handler is None:
            raise requests.ConnectionError(f"blocked: {url}?serviceKey={FAKE_KEY}")
        return handler(url) if callable(handler) else handler
    return fake_get


def kma_success(url):
    return FakeResp(200, kma_ok_fcst() if url.endswith("getUltraSrtFcst") else kma_ok_ncst())


class KmaFallbackTest(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = Path(path)
        self.patches = [
            mock.patch.object(m, "DB_PATH", self.db_path),
            mock.patch.object(m, "KMA_SERVICE_KEY", FAKE_KEY),
            mock.patch.object(m, "_kma_auth_block_until", None),
            mock.patch.dict(m._kma_cache, {}, clear=True),
        ]
        for p in self.patches:
            p.start()
        m.LOGS.clear()
        m.init_db()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.db_path.unlink(missing_ok=True)

    def row_count(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM weather_logs").fetchone()[0]
        finally:
            conn.close()

    def insert_row(self, ts, temperature):
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO weather_logs (timestamp, temperature, condition, humidity, wind_speed, "
                    "location, nx, ny, created_at) VALUES (?, ?, ?, 70, 2.0, ?, ?, ?, ?)",
                    (ts, temperature, "흐림", "Seoul", m.SEOUL_NX, m.SEOUL_NY, ts))
        finally:
            conn.close()

    def logs_text(self):
        return "\n".join(entry["message"] for entry in m.LOGS)

    def assert_no_key(self, text):
        self.assertNotIn(FAKE_KEY, text)
        self.assertNotIn("FAKE%2Ftest", text)

    # (a) 403 → KmaError(HTTP_403), 메시지에 키 없음, 쿨다운 동작
    def test_403_converted_and_masked(self):
        with mock.patch.object(m.HTTP, "get", side_effect=router(kma=FakeResp(403, text="Forbidden"))) as g:
            with self.assertRaises(m.KmaError) as cm:
                m.fetch_kma_realtime()
            self.assertEqual(cm.exception.code, "HTTP_403")
            self.assert_no_key(str(cm.exception))
            calls = g.call_count
            with self.assertRaises(m.KmaError) as cm2:
                m.fetch_kma_realtime()
            self.assertEqual(cm2.exception.code, "COOLDOWN")
            self.assertEqual(g.call_count, calls)  # 쿨다운 중에는 네트워크 호출 없음

    def test_403_log_masked_in_full_flow(self):
        with mock.patch.object(m.HTTP, "get", side_effect=router(kma=FakeResp(403))):
            w = m.get_current_weather()
        self.assertEqual(w.source, "Sample (offline)")
        self.assertIn("HTTP_403", self.logs_text())
        self.assert_no_key(self.logs_text())

    def test_network_error_masked(self):
        with mock.patch.object(m.HTTP, "get", side_effect=router(kma=None)):
            with self.assertRaises(m.KmaError) as cm:
                m.fetch_kma_realtime()
        self.assertEqual(cm.exception.code, "NETWORK")
        self.assert_no_key(str(cm.exception))
        self.assertIn("연결 실패 (ConnectionError)", str(cm.exception))

    # (b) XML 인증 오류 응답
    def test_xml_auth_error(self):
        with mock.patch.object(m.HTTP, "get", side_effect=router(kma=FakeResp(200, text=XML_AUTH))):
            with self.assertRaises(m.KmaError) as cm:
                m.fetch_kma_realtime()
        self.assertEqual(cm.exception.code, "AUTH")
        self.assertIn("SERVICE_KEY_IS_NOT_REGISTERED_ERROR", str(cm.exception))
        self.assertIsNotNone(m._kma_auth_block_until)

    # (c) 구조가 깨진 JSON / 빈 items / 오류 resultCode
    def test_broken_json_structure(self):
        cases = [
            ({"foo": 1}, "PARSE"),
            ({"response": {"header": {"resultCode": "00"}}}, "PARSE"),
            ({"response": {"header": {"resultCode": "00"}, "body": {"items": ""}}}, "NO_ITEMS"),
            ({"response": {"header": {"resultCode": "00"}, "body": {"items": {"item": []}}}}, "NO_ITEMS"),
            ({"response": {"header": {"resultCode": "10", "resultMsg": "INVALID_REQUEST_PARAMETER_ERROR"}}}, "10"),
        ]
        for payload, code in cases:
            with self.subTest(code=code, payload=payload):
                with mock.patch.object(m.HTTP, "get", side_effect=router(kma=FakeResp(200, payload))):
                    with self.assertRaises(m.KmaError) as cm:
                        m.fetch_kma_realtime()
                self.assertEqual(cm.exception.code, code)

    def test_invalid_t1h_value(self):
        with mock.patch.object(m.HTTP, "get", side_effect=router(kma=FakeResp(200, kma_ok_ncst(t1h="")))):
            with self.assertRaises(m.KmaError) as cm:
                m.fetch_kma_realtime()
        self.assertEqual(cm.exception.code, "NO_T1H")

    # (g) 정상 응답 → DB 저장 + 캐시 갱신
    def test_success_saves_and_caches(self):
        with mock.patch.object(m.HTTP, "get", side_effect=router(kma=kma_success, open_meteo=FakeResp(200, OPEN_METEO))):
            w = m.get_current_weather()
        self.assertEqual(w.source, "기상청 초단기실황")
        self.assertEqual(w.temperature, 8.9)
        self.assertEqual(w.condition, "맑음")
        self.assertIsNotNone(w.log_id)
        self.assertEqual(self.row_count(), 1)
        self.assertIn((m.SEOUL_NX, m.SEOUL_NY), m._kma_cache)

    # (d) KMA 실패 + 메모리 캐시 → 캐시 반환, DB 변화 없음
    def test_memory_cache_used_on_failure(self):
        with mock.patch.object(m.HTTP, "get", side_effect=router(kma=kma_success, open_meteo=FakeResp(200, OPEN_METEO))):
            ok = m.get_current_weather()
        before = self.row_count()
        with mock.patch.object(m.HTTP, "get", side_effect=router(kma=FakeResp(403), open_meteo=FakeResp(200, OPEN_METEO))):
            w = m.get_current_weather()
        self.assertEqual(w.source, m.KMA_CACHE_SOURCE)
        self.assertEqual(w.temperature, ok.temperature)
        self.assertEqual(w.observed_at, ok.observed_at)
        self.assertEqual(self.row_count(), before)
        self.assertIn("KMA 캐시 사용", self.logs_text())
        self.assert_no_key(self.logs_text())

    # (d) KMA 실패 + DB 캐시(메모리 없음) → DB 최신 행 반환, DB 변화 없음
    def test_db_cache_used_when_memory_empty(self):
        ts = (datetime.now(m.KST) - timedelta(hours=1)).strftime("%Y-%m-%d %H:00")
        self.insert_row(ts, 5.5)
        with mock.patch.object(m.HTTP, "get", side_effect=router(kma=FakeResp(500), open_meteo=FakeResp(200, OPEN_METEO))):
            w = m.get_current_weather()
        self.assertEqual(w.source, m.KMA_CACHE_SOURCE)
        self.assertEqual(w.temperature, 5.5)
        self.assertEqual(w.observed_at, ts)
        self.assertEqual(self.row_count(), 1)

    # (e) 캐시 없음 → Open-Meteo
    def test_no_cache_falls_back_to_open_meteo(self):
        with mock.patch.object(m.HTTP, "get", side_effect=router(kma=FakeResp(403), open_meteo=FakeResp(200, OPEN_METEO))):
            w = m.get_current_weather()
        self.assertEqual(w.source, "Open-Meteo")
        self.assertEqual(self.row_count(), 0)

    # (e) TTL 만료 캐시(메모리·DB 모두) → Open-Meteo
    def test_expired_cache_falls_back_to_open_meteo(self):
        old = (datetime.now(m.KST) - m.KMA_CACHE_TTL - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
        self.insert_row(old, 1.0)
        m._kma_cache[(m.SEOUL_NX, m.SEOUL_NY)] = m.WeatherData(
            location="Seoul", temperature=1.0, source="기상청 초단기실황", observed_at=old,
            nx=m.SEOUL_NX, ny=m.SEOUL_NY)
        with mock.patch.object(m.HTTP, "get", side_effect=router(kma=FakeResp(403), open_meteo=FakeResp(200, OPEN_METEO))):
            w = m.get_current_weather()
        self.assertEqual(w.source, "Open-Meteo")

    def test_other_grid_cache_not_used(self):
        m._kma_cache[(1, 1)] = m.WeatherData(
            location="X", temperature=1.0, source="기상청 초단기실황",
            observed_at=datetime.now(m.KST).strftime("%Y-%m-%d %H:%M"), nx=1, ny=1)
        with mock.patch.object(m.HTTP, "get", side_effect=router(kma=FakeResp(403), open_meteo=FakeResp(200, OPEN_METEO))):
            w = m.get_current_weather()
        self.assertEqual(w.source, "Open-Meteo")

    # (f) 전부 실패 → 샘플
    def test_all_fail_returns_sample(self):
        with mock.patch.object(m.HTTP, "get", side_effect=router(kma=None, open_meteo=None)):
            w = m.get_current_weather()
        self.assertEqual(w.source, "Sample (offline)")
        self.assertEqual(self.row_count(), 0)
        self.assert_no_key(self.logs_text())


if __name__ == "__main__":
    unittest.main()
