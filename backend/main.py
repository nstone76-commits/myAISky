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
    KMA 실패 시 KMA 캐시(같은 격자, 3시간 이내) → Open-Meteo → 샘플 데이터 순으로 대체 (대체 데이터는 DB에 저장하지 않음)

백그라운드 수집: 서버 시작 시 daemon 스레드가 즉시 1회, 이후 COLLECT_INTERVAL_SEC(기본 600초)마다
    서울 기본 격자를 get_current_weather() 로 수집해 weather_logs 에 쌓음. COLLECT_ENABLED=0 이면 끔. 상태는 GET /health 의 collector.
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
import threading
from collections import deque
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote

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

# 외부 API 호출: 일시적 지연/끊김에 대비해 재시도하는 세션 사용
_RETRY = requests.adapters.Retry(total=2, backoff_factor=0.3, status_forcelist=[502, 503, 504])
HTTP = requests.Session()
HTTP.mount("https://", requests.adapters.HTTPAdapter(max_retries=_RETRY))
HTTP.mount("http://", requests.adapters.HTTPAdapter(max_retries=_RETRY))
HTTP_TIMEOUT = (3, 8)  # (connect, read) 초

# 기본 관측 위치: 서울 — 브라우저 위치 권한이 없을 때 사용
LOCATION_NAME = "Seoul"
LAT, LON = 37.5665, 126.9780          # Open-Meteo 보강용 좌표
SEOUL_NX, SEOUL_NY = 60, 127          # 기상청 격자 좌표 (서울 중구·시청)



@asynccontextmanager
async def lifespan(_app: FastAPI):
    """서버 시작 시 백그라운드 수집 스레드 시작, 종료 시 정지."""
    if os.getenv("COLLECT_ENABLED", "1").strip() != "0":
        start_collector(collect_interval_from_env())
    else:
        add_log("INFO", "백그라운드 수집 비활성화 (COLLECT_ENABLED=0)")
    try:
        yield
    finally:
        stop_collector()


app = FastAPI(title="myAISky", description="AI Weather & Agent Monitoring Dashboard", lifespan=lifespan)
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
    conn = sqlite3.connect(DB_PATH, timeout=10)  # 수집 스레드·API 요청 동시 쓰기 대비
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
        # 같은 관측 시각·격자는 한 번만 저장. 이전 버전에서 쌓인 중복은 가장 먼저 저장된 행만 남김.
        conn.execute(
            """
            DELETE FROM weather_logs WHERE id NOT IN (
                SELECT MIN(id) FROM weather_logs GROUP BY timestamp, nx, ny
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_weather_logs_obs ON weather_logs(timestamp, nx, ny)"
        )


def save_weather_log(w: WeatherData) -> tuple[int, bool]:
    """기상청 실황 데이터를 weather_logs 에 저장. 같은 관측 시각·격자가 이미 있으면 건너뜀.

    반환: (행 id, 새로 저장했는지 여부)
    """
    with closing(db_connect()) as conn, conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO weather_logs
                (timestamp, temperature, condition, humidity, wind_speed, location, nx, ny, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (w.observed_at, w.temperature, w.condition, w.humidity, w.wind_speed,
             w.location, w.nx, w.ny, datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")),
        )
        if cur.rowcount:
            return int(cur.lastrowid), True
        row = conn.execute(
            "SELECT id FROM weather_logs WHERE timestamp = ? AND nx = ? AND ny = ?",
            (w.observed_at, w.nx, w.ny),
        ).fetchone()
        return int(row["id"]), False


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


KMA_AUTH_COOLDOWN = timedelta(minutes=10)   # 인증 오류(401/403, 키 미등록) 후 KMA 호출 생략 시간
KMA_CACHE_TTL = timedelta(hours=3)        # 마지막 성공 KMA 데이터(캐시) 허용 범위 (관측 시각 기준)
KMA_CACHE_SOURCE = "기상청 초단기실황 (캐시)"
_kma_auth_block_until: Optional[datetime] = None
_kma_cache: dict[tuple[int, int], WeatherData] = {}   # (nx, ny) → 마지막 성공 KMA 데이터


def mask_secret(text: object) -> str:
    """로그·에러 메시지에서 serviceKey 값을 *** 로 가림."""
    s = str(text)
    s = re.sub(r"(serviceKey=)[^&\s'\"]*", r"\1***", s, flags=re.IGNORECASE)
    if KMA_SERVICE_KEY:
        for secret in {KMA_SERVICE_KEY, quote(KMA_SERVICE_KEY, safe="")}:
            s = s.replace(secret, "***")
    return s


def _to_float(value: object) -> Optional[float]:
    """관측값 → float. 결측·형식 오류면 None."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


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
    global _kma_auth_block_until
    if not KMA_SERVICE_KEY:
        raise KmaError("KMA_SERVICE_KEY 미설정")
    if _kma_auth_block_until is not None and datetime.now(KST) < _kma_auth_block_until:
        raise KmaError(f"인증 오류 쿨다운 중 (~{_kma_auth_block_until:%H:%M}) → KMA 호출 생략", "COOLDOWN")
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
    # 요청 URL에 serviceKey가 포함되므로 requests 예외 메시지를 그대로 로그에 노출하지 않음
    try:
        resp = HTTP.get(f"{KMA_BASE_URL}/{operation}", params=params, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise KmaError(f"연결 실패 ({type(e).__name__})", "NETWORK") from None
    if resp.status_code in (401, 403):
        _kma_auth_block_until = datetime.now(KST) + KMA_AUTH_COOLDOWN
        raise KmaError(f"HTTP {resp.status_code} 인증 거부 (키 등록·활용신청 승인 확인 필요)",
                       f"HTTP_{resp.status_code}")
    if resp.status_code >= 400:
        raise KmaError(f"HTTP {resp.status_code} 오류", f"HTTP_{resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        # 인증 오류 등은 JSON 요청이어도 XML 로 응답됨
        text = resp.text or ""
        m = re.search(r"<returnAuthMsg>(.*?)</returnAuthMsg>|<resultMsg>(.*?)</resultMsg>", text)
        msg = mask_secret((m.group(1) or m.group(2)) if m else text[:120])
        if re.search(r"SERVICE_KEY|SERVICE ACCESS DENIED|<returnReasonCode>(20|22|30|31|32|33)<", text):
            _kma_auth_block_until = datetime.now(KST) + KMA_AUTH_COOLDOWN
            raise KmaError(f"인증 오류 응답: {msg}", "AUTH")
        raise KmaError(f"비정상 응답: {msg}", "BAD_RESPONSE")
    try:
        header = data["response"]["header"]
        code = str(header["resultCode"])
        if code != "00":
            raise KmaError(f"{code} {mask_secret(header.get('resultMsg', ''))}", code)
        items = data["response"]["body"]["items"]
        items = items.get("item") if isinstance(items, dict) else None
    except (KeyError, TypeError, AttributeError) as e:
        raise KmaError(f"응답 구조 이상 ({type(e).__name__}: {mask_secret(e)})", "PARSE") from None
    if isinstance(items, dict):  # 항목이 하나면 dict 로 올 수 있음
        items = [items]
    if not isinstance(items, list) or not items:
        raise KmaError("응답 items 비어 있음", "NO_ITEMS")
    return items


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
    values = {it["category"]: it.get("obsrValue") for it in items
              if isinstance(it, dict) and "category" in it}
    temperature = _to_float(values.get("T1H"))
    if temperature is None:
        raise KmaError("응답에 기온(T1H) 없음 또는 형식 오류", "NO_T1H")

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
            add_log("WARN", f"하늘상태(SKY) 조회 실패 → 강수형태만 표시 ({mask_secret(e)})")

    return WeatherData(
        location=name,
        temperature=temperature,
        humidity=_to_float(values.get("REH")),
        wind_speed=_to_float(values.get("WSD")),
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
    resp = HTTP.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=HTTP_TIMEOUT)
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


def kma_cached_weather(nx: int, ny: int, name: str) -> Optional[WeatherData]:
    """같은 격자의 마지막 성공 KMA 데이터 (메모리 캐시 → weather_logs 최신 행). TTL 초과면 None."""
    now = datetime.now(KST)

    def fresh(observed_at: str) -> bool:
        try:
            obs = datetime.strptime(observed_at, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        except (TypeError, ValueError):
            return False
        return now - obs <= KMA_CACHE_TTL

    cached = _kma_cache.get((nx, ny))
    if cached is not None and fresh(cached.observed_at):
        return cached.model_copy(update={"location": name, "source": KMA_CACHE_SOURCE})
    try:
        with closing(db_connect()) as conn:
            row = conn.execute(
                "SELECT id, timestamp, temperature, condition, humidity, wind_speed FROM weather_logs "
                "WHERE nx = ? AND ny = ? ORDER BY timestamp DESC, id DESC LIMIT 1",
                (nx, ny),
            ).fetchone()
    except sqlite3.Error as e:
        add_log("WARN", f"weather_logs 캐시 조회 실패 ({type(e).__name__}: {e})")
        return None
    if row is None or not fresh(row["timestamp"]):
        return None
    return WeatherData(
        location=name,
        temperature=row["temperature"],
        humidity=row["humidity"],
        wind_speed=row["wind_speed"],
        condition=row["condition"] or "알 수 없음",
        source=KMA_CACHE_SOURCE,
        observed_at=row["timestamp"],
        nx=nx,
        ny=ny,
        log_id=row["id"],
    )


def get_current_weather(lat: Optional[float] = None, lon: Optional[float] = None) -> WeatherData:
    """KMA(→ DB 저장) → KMA 캐시 → Open-Meteo → 샘플 순으로 시도. 24h 트렌드는 Open-Meteo 로 보강.

    lat/lon 이 없으면 서울 격자(nx=60, ny=127), 있으면 해당 좌표의 격자를 사용.
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
            weather.log_id, inserted = save_weather_log(weather)
            add_log("OK", f"KMA 실황 수신 ({nx},{ny}): {weather.temperature}℃ / {weather.condition}")
            if inserted:
                add_log("INFO", f"weather_logs INSERT #{weather.log_id}")
            else:
                add_log("INFO", f"{weather.observed_at} 관측분 이미 저장됨 (#{weather.log_id}) → 건너뜀")
            try:
                om = fetch_open_meteo_weather(lat, lon, name)
                weather.hourly_temps, weather.hourly_labels = om.hourly_temps, om.hourly_labels
            except Exception:
                pass
            _kma_cache[(nx, ny)] = weather.model_copy()
            return weather
        except Exception as e:
            code = getattr(e, "code", "") or type(e).__name__
            add_log("WARN", f"KMA API 실패 [{code}] → 대체 소스 사용 ({mask_secret(e)})")
        cached = kma_cached_weather(nx, ny, name)
        if cached is not None:
            add_log("WARN", f"KMA 캐시 사용 ({nx},{ny}): {cached.observed_at} 관측분 "
                            f"{cached.temperature}℃ / {cached.condition}")
            return cached
    else:
        add_log("INFO", "기상청 격자 범위 밖 → Open-Meteo 사용")

    try:
        weather = fetch_open_meteo_weather(lat, lon, name)
        add_log("OK", f"Open-Meteo 수신 [{name}]: {weather.temperature}℃ / {weather.condition}")
        return weather
    except Exception as e:
        add_log("WARN", f"외부 API 연결 실패 → 샘플 데이터 사용 ({type(e).__name__})")
        return sample_weather(name)


# ─────────────────────────────── 백그라운드 수집 ───────────────────────────────
COLLECT_INTERVAL_DEFAULT = 600.0
COLLECTOR_STATUS: dict = {
    "enabled": False,           # 스레드 동작 중 여부
    "interval_sec": None,
    "runs": 0,                  # 실행 횟수
    "last_run": None,           # 마지막 실행 시각 (KST ISO)
    "last_source": None,        # 마지막 결과의 source
    "consecutive_failures": 0,  # 실시간 KMA 수집 실패(캐시·대체 소스 사용 또는 예외) 연속 횟수
    "last_error": None,
}
_collector_lock = threading.Lock()                 # 한 프로세스에 수집 스레드 하나만 (모듈 수준 가드)
_collector_thread: Optional[threading.Thread] = None
_collector_stop = threading.Event()


def collect_interval_from_env() -> float:
    raw = os.getenv("COLLECT_INTERVAL_SEC", str(int(COLLECT_INTERVAL_DEFAULT)))
    try:
        interval = float(raw)
    except ValueError:
        interval = 0
    if interval <= 0:
        add_log("WARN", f"COLLECT_INTERVAL_SEC 값 오류 ({raw!r}) → 기본 {int(COLLECT_INTERVAL_DEFAULT)}초 사용")
        return COLLECT_INTERVAL_DEFAULT
    return interval


def collect_once() -> None:
    """서울 기본 격자 1회 수집 (KMA 성공 → DB 저장, 실패 → 캐시·대체 소스). 예외는 로그만 남김."""
    now = datetime.now(KST).isoformat(timespec="seconds")
    try:
        weather = get_current_weather()
    except Exception as e:  # 스레드가 죽지 않도록 모든 예외를 기록하고 계속
        with _collector_lock:
            COLLECTOR_STATUS.update(runs=COLLECTOR_STATUS["runs"] + 1, last_run=now, last_source=None,
                                    consecutive_failures=COLLECTOR_STATUS["consecutive_failures"] + 1,
                                    last_error=mask_secret(f"{type(e).__name__}: {e}"))
        add_log("ERROR", mask_secret(f"백그라운드 수집 실패 ({type(e).__name__}: {e})"))
        return
    live = weather.source == "기상청 초단기실황"
    with _collector_lock:
        COLLECTOR_STATUS.update(runs=COLLECTOR_STATUS["runs"] + 1, last_run=now, last_source=weather.source,
                                consecutive_failures=0 if live else COLLECTOR_STATUS["consecutive_failures"] + 1,
                                last_error=None)


def _collector_loop(stop: threading.Event, interval: float) -> None:
    while not stop.is_set():
        collect_once()
        if stop.wait(interval):
            break
    with _collector_lock:
        COLLECTOR_STATUS["enabled"] = False


def start_collector(interval: float = COLLECT_INTERVAL_DEFAULT) -> bool:
    """수집 스레드 시작. 이미 이 프로세스에서 돌고 있으면 False."""
    global _collector_thread, _collector_stop
    with _collector_lock:
        if _collector_thread is not None and _collector_thread.is_alive():
            return False
        _collector_stop = threading.Event()
        _collector_thread = threading.Thread(target=_collector_loop, args=(_collector_stop, interval),
                                             name="kma-collector", daemon=True)
        COLLECTOR_STATUS.update(enabled=True, interval_sec=interval)
        _collector_thread.start()
    add_log("INFO", f"백그라운드 수집 시작 ({interval:g}초 주기)")
    return True


def stop_collector(timeout: float = 15.0) -> None:
    """수집 스레드 정지 (진행 중인 수집은 최대 timeout 초 대기)."""
    global _collector_thread
    with _collector_lock:
        thread = _collector_thread
        _collector_stop.set()
    if thread is None:
        return
    thread.join(timeout)
    if thread.is_alive():
        add_log("WARN", "백그라운드 수집 스레드가 제한 시간 안에 종료되지 않음 (daemon 이므로 프로세스와 함께 종료)")
    else:
        with _collector_lock:
            if _collector_thread is thread:
                _collector_thread = None
        add_log("INFO", "백그라운드 수집 정지")


def collector_status() -> dict:
    with _collector_lock:
        return dict(COLLECTOR_STATUS)


# ─────────────────────────────── API ───────────────────────────────
LatQ = Query(None, ge=-90, le=90, description="위도 (생략 시 서울 nx=60, ny=127)")
LonQ = Query(None, ge=-180, le=180, description="경도 (생략 시 서울 nx=60, ny=127)")


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
    return {"status": "ok", "time": datetime.now(KST).isoformat(), "kma_key": bool(KMA_SERVICE_KEY),
            "collector": collector_status()}


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


def server_address(port: int) -> tuple[str, str]:
    """(bind host, 브라우저 접속 URL). Codespaces 에서는 포워딩 주소로 접속해야 하므로 0.0.0.0 바인딩."""
    codespace = os.getenv("CODESPACE_NAME")
    if codespace:
        domain = os.getenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev")
        return "0.0.0.0", f"https://{codespace}-{port}.{domain}"
    return "127.0.0.1", f"http://127.0.0.1:{port}"


def run(reload: bool = False) -> None:
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    host, url = server_address(port)
    print("=" * 60)
    print(" myAISky 서버 실행 안내")
    print(f"  브라우저 접속 : {url}")
    print(f"  API: {url}/api/weather  |  문서: {url}/docs")
    print(f"  기상청 키     : {'설정됨' if KMA_SERVICE_KEY else '없음 (Open-Meteo 사용)'}")
    print("=" * 60)
    uvicorn.run("backend.main:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    run()
