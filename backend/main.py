"""
myAISky - AI Weather & Agent Monitoring Dashboard (backend)

실행 (프로젝트 루트에서):
    pip install -r requirements.txt
    uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
    (기존 명령 `uvicorn app:app --reload` 도 그대로 동작)

기상청 API 인증키는 프로젝트 루트의 `.env` 파일(KMA_SERVICE_KEY=...) 또는 환경변수로 지정합니다.
Encoding 키(%2F, %3D 포함)와 Decoding 키 모두 사용 가능합니다.

데이터 흐름:
    기상청 초단기실황(getUltraSrtNcst) → 기온·습도·풍속·강수형태
    기상청 초단기예보(getUltraSrtFcst) → 하늘상태(SKY) 보강 (강수 없을 때 맑음/구름많음/흐림 판별)
    → SQLite `weather_logs.db` 의 `weather_logs` 테이블에 자동 INSERT
    → GET /api/weather 로 JSON 반환 → frontend/ 대시보드 렌더링
    KMA 실패 시 Open-Meteo → 샘플 데이터 순으로 대체 (대체 데이터는 DB에 저장하지 않음)
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
from collections import deque
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

import requests
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
DB_PATH = BASE_DIR / "weather_logs.db"
KST = timezone(timedelta(hours=9))


def _load_dotenv(path: Path) -> None:
    """의존성 없이 .env 의 KEY=VALUE 를 환경변수로 로드 (이미 설정된 값은 유지)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")

# requests 가 파라미터를 다시 URL 인코딩하므로, Encoding 키는 디코딩해서 보관 (Decoding 키는 변화 없음)
KMA_SERVICE_KEY = unquote(os.getenv("KMA_SERVICE_KEY", ""))
KMA_BASE_URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0"

# 기본 관측 위치: 서울 — 브라우저 위치 권한이 없을 때 사용
LOCATION_NAME = "Seoul"
LAT, LON = 37.5665, 126.9780          # Open-Meteo 보강용 좌표
SEOUL_NX, SEOUL_NY = 55, 127          # 기상청 격자 좌표 (요청 사양)

app = FastAPI(title="myAISky", description="AI Weather & Agent Monitoring Dashboard")
app.mount("/images", StaticFiles(directory=BASE_DIR / "images"), name="images")
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


# ─────────────────────────────── 모델 ───────────────────────────────
class WeatherData(BaseModel):
    location: str
    temperature: Optional[float] = None  # ℃
    humidity: Optional[float] = None     # %
    wind_speed: Optional[float] = None   # m/s
    condition: str = "알 수 없음"
    hourly_temps: list[float] = []      # 온도 트렌드용 (시간별)
    hourly_labels: list[str] = []
    source: str
    observed_at: str
    nx: Optional[int] = None
    ny: Optional[int] = None
    log_id: Optional[int] = None         # weather_logs 에 저장된 행 id (KMA 데이터일 때)


# ─────────────────────────────── 에이전트 로그 ───────────────────────────────
LOGS: deque[dict] = deque(maxlen=50)


def add_log(level: str, message: str) -> None:
    LOGS.append({"time": datetime.now(KST).strftime("%H:%M:%S"), "level": level, "message": message})


# ─────────────────────────────── SQLite (weather_logs.db) ───────────────────────────────
def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(db_connect()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS weather_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,   -- 기상청 관측 기준 시각 (KST, 'YYYY-MM-DD HH:MM')
                temperature REAL,            -- ℃
                condition   TEXT,            -- 날씨 상태 (맑음/구름많음/흐림/비/눈 ...)
                humidity    REAL,            -- %
                wind_speed  REAL,            -- m/s
                location    TEXT,
                nx          INTEGER,
                ny          INTEGER,
                created_at  TEXT NOT NULL    -- 적재 시각 (KST)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_weather_logs_ts ON weather_logs(timestamp)")


def save_weather_log(w: WeatherData) -> int:
    """기상청 실황 데이터를 weather_logs 테이블에 INSERT 하고 행 id 반환."""
    with closing(db_connect()) as conn, conn:
        cur = conn.execute(
            """
            INSERT INTO weather_logs
                (timestamp, temperature, condition, humidity, wind_speed, location, nx, ny, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (w.observed_at, w.temperature, w.condition, w.humidity, w.wind_speed,
             w.location, w.nx, w.ny, datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")),
        )
        return int(cur.lastrowid)


def recent_weather_logs(limit: int = 20) -> list[dict]:
    with closing(db_connect()) as conn:
        rows = conn.execute(
            "SELECT id, timestamp, temperature, condition, humidity, wind_speed, location, created_at "
            "FROM weather_logs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


init_db()
add_log("INFO", "myAISky agent booted — sandbox session started")
add_log("INFO", f"SQLite 연결: {DB_PATH.name} (weather_logs)")


# ─────────────────────────────── 기상청 API ───────────────────────────────
KMA_PTY = {"0": "강수 없음", "1": "비", "2": "비/눈", "3": "눈", "5": "빗방울", "6": "빗방울/눈날림", "7": "눈날림"}
KMA_SKY = {"1": "맑음", "3": "구름많음", "4": "흐림"}
WMO_CODES = {
    0: "맑음", 1: "대체로 맑음", 2: "구름 조금", 3: "흐림", 45: "안개", 48: "안개",
    51: "이슬비", 53: "이슬비", 55: "이슬비", 61: "비", 63: "비", 65: "강한 비",
    71: "눈", 73: "눈", 75: "강한 눈", 80: "소나기", 81: "소나기", 82: "강한 소나기",
    95: "뇌우", 96: "뇌우(우박)", 99: "뇌우(우박)",
}


class KmaError(RuntimeError):
    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


def latlon_to_kma_grid(lat: float, lon: float) -> tuple[int, int]:
    """위경도 → 기상청 동네예보 격자(nx, ny) 변환 (Lambert Conformal Conic, 기상청 공식 파라미터)."""
    RE, GRID = 6371.00877, 5.0
    SLAT1, SLAT2, OLON, OLAT = 30.0, 60.0, 126.0, 38.0
    XO, YO = 43, 136
    d = math.pi / 180.0
    re_ = RE / GRID
    slat1, slat2, olon, olat = SLAT1 * d, SLAT2 * d, OLON * d, OLAT * d
    sn = math.log(math.cos(slat1) / math.cos(slat2)) / math.log(
        math.tan(math.pi * 0.25 + slat2 * 0.5) / math.tan(math.pi * 0.25 + slat1 * 0.5))
    sf = math.tan(math.pi * 0.25 + slat1 * 0.5) ** sn * math.cos(slat1) / sn
    ro = re_ * sf / math.tan(math.pi * 0.25 + olat * 0.5) ** sn
    ra = re_ * sf / math.tan(math.pi * 0.25 + lat * d * 0.5) ** sn
    theta = lon * d - olon
    theta = (theta + math.pi) % (2 * math.pi) - math.pi
    theta *= sn
    return int(ra * math.sin(theta) + XO + 0.5), int(ro - ra * math.cos(theta) + YO + 0.5)


def in_korea(lat: float, lon: float) -> bool:
    """기상청 격자 범위(대략 한반도 주변)인지 확인."""
    return 32.5 <= lat <= 39.5 and 124.0 <= lon <= 132.0


def kma_request(operation: str, base: datetime, nx: int, ny: int, rows: int = 100) -> list[dict]:
    """기상청 단기예보 서비스 공통 호출. 정상 응답이면 item 리스트 반환, 아니면 KmaError."""
    if not KMA_SERVICE_KEY:
        raise KmaError("KMA_SERVICE_KEY 미설정")
    params = {
        "serviceKey": KMA_SERVICE_KEY,
        "pageNo": 1,
        "numOfRows": rows,
        "dataType": "JSON",
        "base_date": base.strftime("%Y%m%d"),
        "base_time": base.strftime("%H%M"),
        "nx": nx,
        "ny": ny,
    }
    resp = requests.get(f"{KMA_BASE_URL}/{operation}", params=params, timeout=8)
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        # 인증 오류 등은 JSON 요청이어도 XML 로 응답됨
        m = re.search(r"<returnAuthMsg>(.*?)</returnAuthMsg>|<resultMsg>(.*?)</resultMsg>", resp.text)
        raise KmaError(f"비정상 응답: {(m.group(1) or m.group(2)) if m else resp.text[:120]}")
    header = data["response"]["header"]
    if header["resultCode"] != "00":
        raise KmaError(f"{header['resultCode']} {header['resultMsg']}", header["resultCode"])
    return data["response"]["body"]["items"]["item"]


def fetch_kma_realtime(nx: int = SEOUL_NX, ny: int = SEOUL_NY, name: str = LOCATION_NAME) -> WeatherData:
    """오늘 날짜 기준 기상청 초단기실황으로 실시간 기온·날씨 상태 조회.

    - 초단기실황(getUltraSrtNcst): 매시 정각 관측, 약 10분 후 제공 → 아직 없으면 직전 시각으로 재시도.
      카테고리 T1H(기온), REH(습도), WSD(풍속), PTY(강수형태)
    - 강수 없음(PTY=0)이면 초단기예보(getUltraSrtFcst)의 가장 가까운 SKY 값으로 맑음/구름많음/흐림 판별.
    """
    now = datetime.now(KST)
    base = now.replace(minute=0, second=0, microsecond=0)
    if now.minute < 15:
        base -= timedelta(hours=1)

    items: list[dict] = []
    for attempt in range(2):
        try:
            items = kma_request("getUltraSrtNcst", base, nx, ny)
            break
        except KmaError as e:
            if e.code == "03" and attempt == 0:  # NO_DATA → 한 시간 전 기준으로 재시도
                base -= timedelta(hours=1)
                continue
            raise
    values = {it["category"]: it["obsrValue"] for it in items}
    if "T1H" not in values:
        raise KmaError("응답에 기온(T1H) 없음")

    pty = str(values.get("PTY", "0")).strip()
    condition = KMA_PTY.get(pty, "알 수 없음")
    if pty == "0":
        try:
            # 초단기예보: 매시 30분 발표, 약 45분 후 제공
            fbase = now.replace(minute=30, second=0, microsecond=0)
            if now.minute < 45:
                fbase -= timedelta(hours=1)
            fitems = kma_request("getUltraSrtFcst", fbase, nx, ny, rows=60)
            sky = sorted((it for it in fitems if it["category"] == "SKY"),
                         key=lambda it: it["fcstDate"] + it["fcstTime"])
            if sky:
                condition = KMA_SKY.get(str(sky[0]["fcstValue"]), condition)
        except Exception as e:  # 하늘상태 보강 실패는 치명적이지 않음
            add_log("WARN", f"하늘상태(SKY) 조회 실패 → 강수형태만 표시 ({e})")

    return WeatherData(
        location=name,
        temperature=float(values["T1H"]),
        humidity=float(values["REH"]) if "REH" in values else None,
        wind_speed=float(values["WSD"]) if "WSD" in values else None,
        condition=condition,
        source="기상청 초단기실황",
        observed_at=base.strftime("%Y-%m-%d %H:%M"),
        nx=nx,
        ny=ny,
    )


# ─────────────────────────────── 대체 소스 ───────────────────────────────
def fetch_open_meteo_weather(lat: float, lon: float, name: str) -> WeatherData:
    """공공 날씨 데이터(Open-Meteo, 키 불필요) 조회 — 시간별 기온 트렌드 포함."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
        "hourly": "temperature_2m",
        "wind_speed_unit": "ms",
        "forecast_days": 1,
        "timezone": "auto",
    }
    resp = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=5)
    resp.raise_for_status()
    data = resp.json()
    cur = data["current"]
    hourly = data.get("hourly", {})
    return WeatherData(
        location=name,
        temperature=cur["temperature_2m"],
        humidity=cur["relative_humidity_2m"],
        wind_speed=cur["wind_speed_10m"],
        condition=WMO_CODES.get(cur["weather_code"], "알 수 없음"),
        hourly_temps=hourly.get("temperature_2m", []),
        hourly_labels=[t[-5:] for t in hourly.get("time", [])],
        source="Open-Meteo",
        observed_at=cur["time"].replace("T", " "),
    )


def sample_weather(name: str) -> WeatherData:
    """네트워크가 막힌 샌드박스용 샘플 데이터."""
    temps = [12.1, 11.6, 11.2, 10.9, 10.7, 10.8, 11.5, 12.8, 14.3, 15.9, 17.2, 18.1,
             18.8, 19.2, 19.0, 18.4, 17.3, 16.0, 15.1, 14.4, 13.8, 13.3, 12.9, 12.5]
    hour = datetime.now(KST).hour
    return WeatherData(
        location=name,
        temperature=temps[hour],
        humidity=55,
        wind_speed=2.4,
        condition="구름많음",
        hourly_temps=temps,
        hourly_labels=[f"{h:02d}:00" for h in range(24)],
        source="Sample (offline)",
        observed_at=datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
    )


def get_current_weather(lat: Optional[float] = None, lon: Optional[float] = None) -> WeatherData:
    """KMA(→ DB 저장) → Open-Meteo → 샘플 순으로 시도. 24h 트렌드는 Open-Meteo 로 보강.

    lat/lon 이 없으면 서울 격자(nx=55, ny=127), 있으면 해당 좌표의 격자를 사용.
    """
    if lat is None or lon is None:
        lat, lon, name = LAT, LON, LOCATION_NAME
        nx, ny = SEOUL_NX, SEOUL_NY
    else:
        name = f"내 위치 ({lat:.2f}, {lon:.2f})"
        nx, ny = latlon_to_kma_grid(lat, lon) if in_korea(lat, lon) else (None, None)

    if nx is not None and ny is not None:
        try:
            weather = fetch_kma_realtime(nx, ny, name)
            weather.log_id = save_weather_log(weather)
            add_log("OK", f"KMA 실황 수신 ({nx},{ny}): {weather.temperature}℃ / {weather.condition}")
            add_log("INFO", f"weather_logs INSERT #{weather.log_id}")
            try:
                om = fetch_open_meteo_weather(lat, lon, name)
                weather.hourly_temps, weather.hourly_labels = om.hourly_temps, om.hourly_labels
            except Exception:
                pass
            return weather
        except Exception as e:
            add_log("WARN", f"KMA API 실패 → 대체 소스 사용 ({e})")
    else:
        add_log("INFO", "기상청 격자 범위 밖 → Open-Meteo 사용")

    try:
        weather = fetch_open_meteo_weather(lat, lon, name)
        add_log("OK", f"Open-Meteo 수신 [{name}]: {weather.temperature}℃ / {weather.condition}")
        return weather
    except Exception as e:
        add_log("WARN", f"외부 API 연결 실패 → 샘플 데이터 사용 ({type(e).__name__})")
        return sample_weather(name)


# ─────────────────────────────── API ───────────────────────────────
LatQ = Query(None, ge=-90, le=90, description="위도 (생략 시 서울 nx=55, ny=127)")
LonQ = Query(None, ge=-180, le=180, description="경도 (생략 시 서울 nx=55, ny=127)")


@app.get("/api/weather", response_model=WeatherData)
def api_weather(lat: Optional[float] = LatQ, lon: Optional[float] = LonQ) -> WeatherData:
    """기상청 실시간 데이터를 JSON 으로 반환 (조회 시 weather_logs 에 자동 저장)."""
    return get_current_weather(lat, lon)


@app.get("/api/weather/history")
def api_weather_history(limit: int = Query(20, ge=1, le=200)) -> list[dict]:
    """weather_logs 테이블에 적재된 최근 기록."""
    return recent_weather_logs(limit)


@app.get("/api/logs")
def api_logs() -> list[dict]:
    return list(LOGS)


@app.post("/api/sync", response_model=WeatherData)
def api_sync(lat: Optional[float] = LatQ, lon: Optional[float] = LonQ) -> WeatherData:
    add_log("INFO", "수동 데이터 동기화 요청 수신")
    return get_current_weather(lat, lon)


@app.post("/api/geo-status")
def api_geo_status(status: str = Query(..., max_length=80)) -> dict:
    """브라우저 위치 권한 결과를 에이전트 로그에 남김."""
    add_log("INFO", f"위치 감지: {status}")
    return {"ok": True}


@app.post("/api/report")
def api_report(lat: Optional[float] = LatQ, lon: Optional[float] = LonQ) -> dict:
    w = get_current_weather(lat, lon)
    temps = [*w.hourly_temps, *([w.temperature] if w.temperature is not None else [])] or [0]
    report = {
        "generated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
        "location": w.location,
        "summary": f"{w.location} 현재 {w.temperature}℃, {w.condition}. "
                   f"오늘 최고 {max(temps)}℃ / 최저 {min(temps)}℃.",
        "source": w.source,
    }
    add_log("REPORT", report["summary"])
    return report


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "time": datetime.now(KST).isoformat(), "kma_key": bool(KMA_SERVICE_KEY)}


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000)
