"""
myAISky - AI Weather & Agent Monitoring Dashboard (1교시 백엔드)

실행:
    pip install -r requirements.txt
    uvicorn app:app --reload --host 127.0.0.1 --port 8000

위치: 브라우저 위치 권한을 허용하면 현재 위치 날씨, 거부/실패 시 서울 날씨를 표시합니다.
      (위치 API는 localhost 또는 HTTPS에서만 동작)

기상청 API를 쓰려면 공공데이터포털(data.go.kr) 인증키(Decoding 키)를 환경변수로 지정:
    PowerShell:  $env:KMA_SERVICE_KEY = "발급받은_키"
    bash:        export KMA_SERVICE_KEY="발급받은_키"
키가 없으면 Open-Meteo(무료, 키 불필요) → 그것도 실패하면 샘플 데이터로 자동 대체합니다.
"""

from __future__ import annotations

import math
import os
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent

# 기본 관측 위치: 서울 — 브라우저 위치 권한이 없을 때 사용
LOCATION_NAME = "Seoul"
LAT, LON = 37.5665, 126.9780
KMA_SERVICE_KEY = os.getenv("KMA_SERVICE_KEY", "")

app = FastAPI(title="myAISky", description="AI Weather & Agent Monitoring Dashboard")

# /images/sky.jpg 로 배경 이미지를 서빙 (실행 위치와 무관하게 절대경로 사용)
app.mount("/images", StaticFiles(directory=BASE_DIR / "images"), name="images")


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


# ─────────────────────────────── 에이전트 로그 ───────────────────────────────
LOGS: deque[dict] = deque(maxlen=50)


def add_log(level: str, message: str) -> None:
    LOGS.append({"time": datetime.now().strftime("%H:%M:%S"), "level": level, "message": message})


add_log("INFO", "myAISky agent booted — sandbox session started")


# ─────────────────────────────── 기상 데이터 수집 ───────────────────────────────
KMA_PTY = {"0": "맑음/흐림", "1": "비", "2": "비/눈", "3": "눈", "5": "빗방울", "6": "빗방울/눈날림", "7": "눈날림"}
WMO_CODES = {
    0: "맑음", 1: "대체로 맑음", 2: "구름 조금", 3: "흐림", 45: "안개", 48: "안개",
    51: "이슬비", 53: "이슬비", 55: "이슬비", 61: "비", 63: "비", 65: "강한 비",
    71: "눈", 73: "눈", 75: "강한 눈", 80: "소나기", 81: "소나기", 82: "강한 소나기",
    95: "뇌우", 96: "뇌우(우박)", 99: "뇌우(우박)",
}


def latlon_to_kma_grid(lat: float, lon: float) -> tuple[int, int]:
    """위경도 → 기상청 동네예보 격자(nx, ny) 변환 (Lambert Conformal Conic, 기상청 공식 파라미터)."""
    RE, GRID = 6371.00877, 5.0
    SLAT1, SLAT2, OLON, OLAT = 30.0, 60.0, 126.0, 38.0
    XO, YO = 43, 136
    d = math.pi / 180.0
    re = RE / GRID
    slat1, slat2, olon, olat = SLAT1 * d, SLAT2 * d, OLON * d, OLAT * d
    sn = math.log(math.cos(slat1) / math.cos(slat2)) / math.log(
        math.tan(math.pi * 0.25 + slat2 * 0.5) / math.tan(math.pi * 0.25 + slat1 * 0.5))
    sf = math.tan(math.pi * 0.25 + slat1 * 0.5) ** sn * math.cos(slat1) / sn
    ro = re * sf / math.tan(math.pi * 0.25 + olat * 0.5) ** sn
    ra = re * sf / math.tan(math.pi * 0.25 + lat * d * 0.5) ** sn
    theta = lon * d - olon
    theta = (theta + math.pi) % (2 * math.pi) - math.pi
    theta *= sn
    return int(ra * math.sin(theta) + XO + 0.5), int(ro - ra * math.cos(theta) + YO + 0.5)


def in_korea(lat: float, lon: float) -> bool:
    """기상청 격자 범위(대략 한반도 주변)인지 확인."""
    return 32.5 <= lat <= 39.5 and 124.0 <= lon <= 132.0


def fetch_kma_weather(lat: float, lon: float, name: str) -> WeatherData:
    """[초안] 기상청 초단기실황(getUltraSrtNcst) 조회.

    - 매시 정각 데이터가 약 40분 후 제공되므로, 40분 이전이면 직전 시각 기준으로 요청.
    - 카테고리: T1H(기온), REH(습도), WSD(풍속), PTY(강수형태)
    """
    if not KMA_SERVICE_KEY:
        raise RuntimeError("KMA_SERVICE_KEY not set")
    if not in_korea(lat, lon):
        raise RuntimeError("location outside KMA grid")
    nx, ny = latlon_to_kma_grid(lat, lon)

    now = datetime.now()
    base = now - timedelta(hours=1) if now.minute < 40 else now
    params = {
        "serviceKey": KMA_SERVICE_KEY,
        "pageNo": 1,
        "numOfRows": 100,
        "dataType": "JSON",
        "base_date": base.strftime("%Y%m%d"),
        "base_time": base.strftime("%H00"),
        "nx": nx,
        "ny": ny,
    }
    url = "http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getUltraSrtNcst"
    resp = requests.get(url, params=params, timeout=5)
    resp.raise_for_status()
    items = resp.json()["response"]["body"]["items"]["item"]
    values = {it["category"]: it["obsrValue"] for it in items}

    return WeatherData(
        location=name,
        temperature=float(values["T1H"]),
        humidity=float(values.get("REH", 0)),
        wind_speed=float(values.get("WSD", 0)),
        condition=KMA_PTY.get(str(values.get("PTY", "0")), "알 수 없음"),
        source="KMA (기상청 초단기실황)",
        observed_at=f"{params['base_date']} {params['base_time']}",
    )


def fetch_open_meteo_weather(lat: float, lon: float, name: str) -> WeatherData:
    """공공 날씨 데이터(Open-Meteo, 키 불필요) 조회 — 시간별 기온 트렌드 포함."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
        "hourly": "temperature_2m",
        "wind_speed_unit": "ms",
        "forecast_days": 1,
        "timezone": "auto",  # 해당 위치의 현지 시간 기준
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
    hour = datetime.now().hour
    return WeatherData(
        location=name,
        temperature=temps[hour],
        humidity=55,
        wind_speed=2.4,
        condition="구름 조금",
        hourly_temps=temps,
        hourly_labels=[f"{h:02d}:00" for h in range(24)],
        source="Sample (offline)",
        observed_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


def get_current_weather(lat: Optional[float] = None, lon: Optional[float] = None) -> WeatherData:
    """KMA → Open-Meteo → 샘플 순으로 시도. 트렌드는 KMA 실황에 없으므로 Open-Meteo로 보강.

    lat/lon이 주어지면(브라우저 위치) 그 좌표를, 없으면 기본 위치(서울)를 사용.
    """
    if lat is None or lon is None:
        lat, lon, name = LAT, LON, LOCATION_NAME
    else:
        name = f"내 위치 ({lat:.2f}, {lon:.2f})"

    try:
        weather = fetch_kma_weather(lat, lon, name)
        try:
            om = fetch_open_meteo_weather(lat, lon, name)
            weather.hourly_temps, weather.hourly_labels = om.hourly_temps, om.hourly_labels
        except Exception:
            pass
        add_log("OK", f"KMA 실황 수신: {weather.temperature}℃ / {weather.condition}")
        return weather
    except Exception as e:
        if KMA_SERVICE_KEY:
            add_log("WARN", f"KMA API 미사용 → 대체 소스 사용 ({e})")

    try:
        weather = fetch_open_meteo_weather(lat, lon, name)
        add_log("OK", f"Open-Meteo 수신 [{name}]: {weather.temperature}℃ / {weather.condition}")
        return weather
    except Exception as e:
        add_log("WARN", f"외부 API 연결 실패 → 샘플 데이터 사용 ({type(e).__name__})")
        return sample_weather(name)


# ─────────────────────────────── API ───────────────────────────────
LatQ = Query(None, ge=-90, le=90, description="위도 (생략 시 서울)")
LonQ = Query(None, ge=-180, le=180, description="경도 (생략 시 서울)")


@app.get("/api/weather", response_model=WeatherData)
def api_weather(lat: Optional[float] = LatQ, lon: Optional[float] = LonQ) -> WeatherData:
    """현재 기상 데이터를 JSON으로 반환. ?lat=..&lon=.. 로 위치 지정 가능."""
    return get_current_weather(lat, lon)


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
    temps = w.hourly_temps or [w.temperature or 0]
    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "location": w.location,
        "summary": f"{w.location} 현재 {w.temperature}℃, {w.condition}. "
                   f"오늘 최고 {max(temps)}℃ / 최저 {min(temps)}℃.",
        "source": w.source,
    }
    add_log("REPORT", report["summary"])
    return report


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "time": datetime.now().isoformat()}


# ─────────────────────────────── 대시보드 HTML ───────────────────────────────
DASHBOARD_HTML = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>myAISky</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --cream: #F8F5EE;
    --ink: #2B2A28;
    --muted: #7A746A;
    --line: #E6E0D3;
    --accent: #4E7FA8;
    --warm: #D98C5F;
    --charcoal: #24262A;
    --charcoal-2: #2E3136;
    --ok: #3FB27F;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    min-height: 100vh;
    font-family: "Noto Sans KR", system-ui, sans-serif;
    color: var(--ink);
    background-color: #9DB8D3;
    background-image: url('/images/sky.jpg');
    background-size: cover;
    background-position: center;
    background-attachment: fixed;
    padding: 40px 16px;
  }
  .container {
    max-width: 1180px;
    margin: 0 auto;
    background: rgba(248, 245, 238, 0.94);
    border-radius: 24px;
    box-shadow: 0 20px 50px rgba(30, 45, 70, 0.22), 0 2px 8px rgba(30, 45, 70, 0.08);
    padding: 32px;
    backdrop-filter: blur(6px);
  }
  header { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; flex-wrap: wrap; margin-bottom: 28px; }
  h1 { font-size: 34px; font-weight: 700; letter-spacing: -0.5px; }
  h1 span { color: var(--accent); }
  .subtitle { color: var(--muted); margin-top: 4px; font-size: 15px; }
  .badge {
    display: inline-flex; align-items: center; gap: 8px;
    background: #fff; border: 1px solid var(--line); border-radius: 999px;
    padding: 8px 16px; font-size: 13px; font-weight: 500;
    box-shadow: 0 2px 6px rgba(0,0,0,0.05);
  }
  .card {
    background: #fff; border: 1px solid var(--line); border-radius: 18px;
    padding: 22px; box-shadow: 0 4px 14px rgba(60, 50, 30, 0.06);
  }
  .card h2 { font-size: 15px; font-weight: 700; margin-bottom: 14px; }
  .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 20px; }
  .stat .label { font-size: 13px; color: var(--muted); }
  .stat .value { font-size: 30px; font-weight: 700; margin-top: 6px; font-variant-numeric: tabular-nums; }
  .stat .value small { font-size: 15px; color: var(--muted); font-weight: 500; margin-left: 2px; }
  .main { display: grid; grid-template-columns: 1.4fr 1fr; gap: 20px; margin-bottom: 20px; }
  .left { display: flex; flex-direction: column; gap: 20px; }
  .analysis { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 20px; font-size: 14px; }
  .analysis dt { color: var(--muted); }
  .analysis dd { font-weight: 500; text-align: right; }
  .analysis div { display: flex; justify-content: space-between; border-bottom: 1px dashed var(--line); padding: 6px 0; }
  #chart { width: 100%; height: 200px; display: block; }
  .log-panel {
    background: var(--charcoal); color: #D8DADF; border-radius: 18px; padding: 22px;
    box-shadow: 0 10px 28px rgba(0,0,0,0.25); display: flex; flex-direction: column; min-height: 420px;
  }
  .log-panel h2 { color: #fff; font-size: 15px; margin-bottom: 14px; display: flex; justify-content: space-between; align-items: center; }
  .log-panel h2 .live { font-size: 11px; color: var(--ok); font-family: "JetBrains Mono", monospace; }
  #logs {
    flex: 1; overflow-y: auto; background: var(--charcoal-2); border-radius: 12px; padding: 14px;
    font-family: "JetBrains Mono", monospace; font-size: 12.5px; line-height: 1.75; max-height: 460px;
  }
  .log-line .t { color: #7D838D; margin-right: 8px; }
  .lv { display: inline-block; min-width: 62px; font-weight: 500; }
  .lv-INFO { color: #7FB3E0; } .lv-OK { color: var(--ok); } .lv-WARN { color: #E8B04E; } .lv-REPORT { color: #C79BE8; }
  .controls { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
  .controls p { color: var(--muted); font-size: 13px; margin-top: 4px; }
  .btns { display: flex; gap: 10px; flex-wrap: wrap; }
  button {
    font: inherit; font-size: 14px; font-weight: 500; border-radius: 12px; padding: 11px 20px;
    cursor: pointer; border: 1px solid var(--line); background: #fff; color: var(--ink); transition: transform .1s, box-shadow .2s;
  }
  button:hover { box-shadow: 0 4px 12px rgba(0,0,0,0.08); }
  button:active { transform: translateY(1px); }
  button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
  button:disabled { opacity: .6; cursor: wait; }
  #report { margin-top: 14px; font-size: 14px; background: var(--cream); border-radius: 12px; padding: 12px 14px; display: none; }
  footer { text-align: center; font-size: 12px; color: var(--muted); margin-top: 22px; }
  @media (max-width: 900px) {
    .stats { grid-template-columns: repeat(2, 1fr); }
    .main { grid-template-columns: 1fr; }
    .container { padding: 20px; }
  }
</style>
</head>
<body>
<div class="container">
  <header>
    <div>
      <h1>my<span>AI</span>Sky</h1>
      <p class="subtitle">AI Weather &amp; Agent Monitoring Dashboard</p>
    </div>
    <div class="badge">🟢 24/7 Sandbox Active</div>
  </header>

  <section class="stats">
    <div class="card stat"><div class="label">현재 기온</div><div class="value" id="s-temp">--<small>℃</small></div></div>
    <div class="card stat"><div class="label">습도</div><div class="value" id="s-hum">--<small>%</small></div></div>
    <div class="card stat"><div class="label">풍속</div><div class="value" id="s-wind">--<small>m/s</small></div></div>
    <div class="card stat"><div class="label">하늘 상태</div><div class="value" id="s-cond" style="font-size:22px;padding-top:6px">--</div></div>
  </section>

  <section class="main">
    <div class="left">
      <div class="card">
        <h2>🌤 실시간 기상 데이터 분석</h2>
        <dl class="analysis">
          <div><dt>관측 지점</dt><dd id="a-loc">--</dd></div>
          <div><dt>관측 시각</dt><dd id="a-time">--</dd></div>
          <div><dt>오늘 최고</dt><dd id="a-max">--</dd></div>
          <div><dt>오늘 최저</dt><dd id="a-min">--</dd></div>
          <div><dt>일교차</dt><dd id="a-range">--</dd></div>
          <div><dt>데이터 소스</dt><dd id="a-src">--</dd></div>
        </dl>
      </div>
      <div class="card">
        <h2>📈 온도 트렌드 (24h)</h2>
        <svg id="chart" viewBox="0 0 600 200" preserveAspectRatio="none"></svg>
      </div>
    </div>

    <aside class="log-panel">
      <h2>🤖 AI 에이전트 실시간 로그 <span class="live">● LIVE</span></h2>
      <div id="logs"></div>
    </aside>
  </section>

  <section class="card">
    <div class="controls">
      <div>
        <h2 style="margin:0">🛠 Manual Control Panel</h2>
        <p>기상 데이터를 즉시 동기화하거나 일일 요약 리포트를 생성합니다.</p>
      </div>
      <div class="btns">
        <button id="btn-sync" class="primary">🔄 데이터 동기화</button>
        <button id="btn-report">📝 리포트 생성</button>
      </div>
    </div>
    <div id="report"></div>
  </section>

  <footer>myAISky · FastAPI Sandbox · <span id="clock"></span></footer>
</div>

<script>
const $ = (id) => document.getElementById(id);
const fmt = (v, d = 1) => (v === null || v === undefined) ? '--' : Number(v).toFixed(d);

function render(w) {
  $('s-temp').innerHTML = fmt(w.temperature) + '<small>℃</small>';
  $('s-hum').innerHTML = fmt(w.humidity, 0) + '<small>%</small>';
  $('s-wind').innerHTML = fmt(w.wind_speed) + '<small>m/s</small>';
  $('s-cond').textContent = w.condition;
  $('a-loc').textContent = w.location;
  $('a-time').textContent = w.observed_at;
  $('a-src').textContent = w.source;
  const t = w.hourly_temps || [];
  if (t.length) {
    const max = Math.max(...t), min = Math.min(...t);
    $('a-max').textContent = max.toFixed(1) + '℃';
    $('a-min').textContent = min.toFixed(1) + '℃';
    $('a-range').textContent = (max - min).toFixed(1) + '℃';
  }
  drawChart(t, w.hourly_labels || []);
}

function drawChart(temps, labels) {
  const svg = $('chart');
  if (!temps.length) { svg.innerHTML = ''; return; }
  const W = 600, H = 200, P = 24;
  const max = Math.max(...temps) + 1, min = Math.min(...temps) - 1;
  const x = (i) => P + i * (W - 2 * P) / Math.max(temps.length - 1, 1);
  const y = (v) => H - P - (v - min) / (max - min) * (H - 2 * P);
  const pts = temps.map((v, i) => `${x(i)},${y(v)}`).join(' ');
  const area = `${x(0)},${H - P} ${pts} ${x(temps.length - 1)},${H - P}`;
  const nowIdx = new Date().getHours();
  let grid = '';
  for (let i = 0; i < temps.length; i += 6) {
    grid += `<text x="${x(i)}" y="${H - 6}" font-size="11" fill="#7A746A" text-anchor="middle">${labels[i] || ''}</text>`;
  }
  const dot = nowIdx < temps.length
    ? `<circle cx="${x(nowIdx)}" cy="${y(temps[nowIdx])}" r="5" fill="#D98C5F" stroke="#fff" stroke-width="2"/>` : '';
  svg.innerHTML = `
    <defs><linearGradient id="g" x1="0" x2="0" y1="0" y2="1">
      <stop offset="0%" stop-color="#4E7FA8" stop-opacity="0.28"/><stop offset="100%" stop-color="#4E7FA8" stop-opacity="0"/>
    </linearGradient></defs>
    <polygon points="${area}" fill="url(#g)"/>
    <polyline points="${pts}" fill="none" stroke="#4E7FA8" stroke-width="2.5" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>
    ${dot}${grid}`;
}

// ── 위치: 브라우저 위치 권한 → 실패/거부 시 서버 기본값(서울) ──
let coords = null;
const q = () => coords ? `?lat=${coords.lat.toFixed(4)}&lon=${coords.lon.toFixed(4)}` : '';
const geoLog = (msg) => fetch('/api/geo-status?status=' + encodeURIComponent(msg), { method: 'POST' });

function locate() {
  return new Promise((resolve) => {
    if (!navigator.geolocation) { geoLog('브라우저 미지원 → 기본 위치(서울)'); return resolve(); }
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        coords = { lat: pos.coords.latitude, lon: pos.coords.longitude };
        geoLog(`위치 허용 (정확도 ±${Math.round(pos.coords.accuracy)}m)`);
        resolve();
      },
      (err) => {
        const why = { 1: '권한 거부', 2: '위치 확인 불가', 3: '시간 초과' }[err.code] || err.message;
        geoLog(`${why} → 기본 위치(서울)`);
        resolve();
      },
      { enableHighAccuracy: false, timeout: 10000, maximumAge: 10 * 60 * 1000 }
    );
  });
}

async function loadWeather() { render(await (await fetch('/api/weather' + q())).json()); loadLogs(); }

async function loadLogs() {
  const logs = await (await fetch('/api/logs')).json();
  const box = $('logs');
  box.innerHTML = logs.map(l =>
    `<div class="log-line"><span class="t">${l.time}</span><span class="lv lv-${l.level}">[${l.level}]</span>${l.message.replace(/</g, '&lt;')}</div>`
  ).join('');
  box.scrollTop = box.scrollHeight;
}

async function withBusy(btn, fn) { btn.disabled = true; try { await fn(); } finally { btn.disabled = false; } }

$('btn-sync').onclick = (e) => withBusy(e.target, async () => {
  render(await (await fetch('/api/sync' + q(), { method: 'POST' })).json());
  loadLogs();
});
$('btn-report').onclick = (e) => withBusy(e.target, async () => {
  const r = await (await fetch('/api/report' + q(), { method: 'POST' })).json();
  const box = $('report');
  box.style.display = 'block';
  box.innerHTML = `<strong>📄 ${r.generated_at}</strong><br>${r.summary} <span style="color:#7A746A">(${r.source})</span>`;
  loadLogs();
});

setInterval(() => $('clock').textContent = new Date().toLocaleString('ko-KR'), 1000);
setInterval(loadLogs, 5000);
setInterval(loadWeather, 10 * 60 * 1000);
locate().then(loadWeather);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    return HTMLResponse(DASHBOARD_HTML)


if __name__ == "__main__":
    import uvicorn

    print("=" * 60)
    print(" myAISky 서버 실행 안내")
    print("  로컬     : uvicorn app:app --reload --host 127.0.0.1 --port 8000")
    print("  샌드박스 : uvicorn app:app --host 0.0.0.0 --port 8000")
    print("  접속     : http://127.0.0.1:8000  |  API: /api/weather  |  문서: /docs")
    print("=" * 60)
    uvicorn.run(app, host="127.0.0.1", port=8000)
