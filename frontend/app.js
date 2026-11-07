const $ = (id) => document.getElementById(id);
const fmt = (v, d = 1) => (v === null || v === undefined) ? '--' : Number(v).toFixed(d);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

// 날씨 상태 → 아이콘 / 히어로 배경 분위기
function weatherVisual(cond, observedAt) {
  const hour = Number((observedAt || '').slice(11, 13)) || new Date().getHours();
  const night = hour >= 18 || hour < 6;
  const c = cond || '';
  if (/뇌우/.test(c)) return { icon: '⛈️', mood: 'rain' };
  if (/눈날림|비\/눈|빗방울\/눈/.test(c)) return { icon: '🌨️', mood: 'snow' };
  if (/눈/.test(c)) return { icon: '❄️', mood: 'snow' };
  if (/빗방울|이슬비|소나기/.test(c)) return { icon: '🌦️', mood: 'rain' };
  if (/비/.test(c)) return { icon: '🌧️', mood: 'rain' };
  if (/안개/.test(c)) return { icon: '🌫️', mood: 'cloudy' };
  if (/흐림/.test(c)) return { icon: '☁️', mood: 'cloudy' };
  if (/구름/.test(c)) return { icon: night ? '☁️' : '⛅', mood: night ? 'night' : 'cloudy' };
  if (/맑음/.test(c)) return { icon: night ? '🌙' : '☀️', mood: night ? 'night' : 'clear' };
  return { icon: '🌡️', mood: night ? 'night' : 'clear' };
}

function windHint(ws) {
  if (ws === null || ws === undefined) return '--';
  if (ws < 1) return '고요함';
  if (ws < 4) return '약한 바람';
  if (ws < 9) return '약간 강한 바람';
  if (ws < 14) return '강한 바람';
  return '매우 강한 바람';
}

function render(w) {
  const v = weatherVisual(w.condition, w.observed_at);
  const hero = $('hero');
  hero.dataset.mood = v.mood;
  hero.classList.remove('flash'); void hero.offsetWidth; hero.classList.add('flash');

  $('h-icon').textContent = v.icon;
  $('h-temp').textContent = fmt(w.temperature);
  $('h-cond').textContent = w.condition;
  $('h-loc').textContent = '📍 ' + w.location;
  $('h-time').textContent = w.observed_at;
  $('h-grid').textContent = (w.nx != null) ? `${w.nx}, ${w.ny}` : '—';
  $('h-updated').textContent = '갱신 ' + new Date().toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' });
  const src = $('h-src');
  src.textContent = w.source;
  src.className = 'src-pill ' + (w.source.startsWith('기상청') ? 'kma' : 'fallback');

  $('s-hum').innerHTML = fmt(w.humidity, 0) + '<small>%</small>';
  $('b-hum').style.width = (w.humidity ?? 0) + '%';
  $('s-wind').innerHTML = fmt(w.wind_speed) + '<small>m/s</small>';
  $('s-wind-hint').textContent = windHint(w.wind_speed);

  const t = w.hourly_temps || [];
  // 최고/최저는 트렌드(예보)와 기상청 실측 현재 기온을 함께 고려
  const all = w.temperature != null ? [...t, w.temperature] : t;
  if (all.length) {
    $('s-max').innerHTML = Math.max(...all).toFixed(1) + '<small>℃</small>';
    $('s-min').innerHTML = Math.min(...all).toFixed(1) + '<small>℃</small>';
  }
  drawChart(t, w.hourly_labels || [], w.temperature);
}

function drawChart(temps, labels, current) {
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
    grid += `<text x="${x(i)}" y="${H - 6}" font-size="11" fill="#7A746A" text-anchor="middle">${esc(labels[i])}</text>`;
  }
  const dotY = (current != null && current >= min && current <= max) ? y(current) : y(temps[nowIdx]);
  const dot = nowIdx < temps.length
    ? `<line x1="${x(nowIdx)}" x2="${x(nowIdx)}" y1="${P / 2}" y2="${H - P}" stroke="#D98C5F" stroke-dasharray="3 4" vector-effect="non-scaling-stroke"/>
       <circle cx="${x(nowIdx)}" cy="${dotY}" r="5" fill="#D98C5F" stroke="#fff" stroke-width="2"/>` : '';
  svg.innerHTML = `
    <defs><linearGradient id="g" x1="0" x2="0" y1="0" y2="1">
      <stop offset="0%" stop-color="#4E7FA8" stop-opacity="0.28"/><stop offset="100%" stop-color="#4E7FA8" stop-opacity="0"/>
    </linearGradient></defs>
    <polygon points="${area}" fill="url(#g)"/>
    <polyline points="${pts}" fill="none" stroke="#4E7FA8" stroke-width="2.5" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>
    ${dot}${grid}`;
}

// ── DB 적재 기록 ──
let lastTopId = 0;
async function loadHistory() {
  const rows = await (await fetch('/api/weather/history?limit=30')).json();
  const body = $('db-rows');
  $('db-count').textContent = rows.length ? `최근 ${rows.length}건` : '';
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="6" class="empty">아직 저장된 기록이 없습니다.</td></tr>';
    return;
  }
  body.innerHTML = rows.map(r => `
    <tr class="${r.id > lastTopId && lastTopId ? 'new' : ''}">
      <td>${r.id}</td>
      <td>${esc(r.timestamp)}</td>
      <td><b>${fmt(r.temperature)}℃</b></td>
      <td>${weatherVisual(r.condition, r.timestamp).icon} ${esc(r.condition)}</td>
      <td>${fmt(r.humidity, 0)}%</td>
      <td class="muted" style="margin:0">${esc((r.created_at || '').slice(5))}</td>
    </tr>`).join('');
  lastTopId = rows[0].id;
}

// ── 위치: 브라우저 위치 권한 → 실패/거부 시 서버 기본값(서울 nx=55, ny=127) ──
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

async function refreshSide() { await Promise.all([loadLogs(), loadHistory()]); }

async function loadWeather() {
  try {
    render(await (await fetch('/api/weather' + q())).json());
  } catch (e) {
    $('h-cond').textContent = '데이터를 불러오지 못했습니다';
  }
  refreshSide();
}

async function loadLogs() {
  const logs = await (await fetch('/api/logs')).json();
  const box = $('logs');
  box.innerHTML = logs.map(l =>
    `<div class="log-line"><span class="t">${esc(l.time)}</span><span class="lv lv-${esc(l.level)}">[${esc(l.level)}]</span>${esc(l.message)}</div>`
  ).join('');
  box.scrollTop = box.scrollHeight;
}

async function withBusy(btn, fn) { btn.disabled = true; try { await fn(); } finally { btn.disabled = false; } }

$('btn-sync').onclick = (e) => withBusy(e.currentTarget, async () => {
  render(await (await fetch('/api/sync' + q(), { method: 'POST' })).json());
  refreshSide();
});
$('btn-report').onclick = (e) => withBusy(e.currentTarget, async () => {
  const r = await (await fetch('/api/report' + q(), { method: 'POST' })).json();
  const box = $('report');
  box.style.display = 'block';
  box.innerHTML = `<strong>📄 ${esc(r.generated_at)}</strong><br>${esc(r.summary)} <span style="color:#7A746A">(${esc(r.source)})</span>`;
  refreshSide();
});

setInterval(() => $('clock').textContent = new Date().toLocaleString('ko-KR'), 1000);
setInterval(loadLogs, 5000);
setInterval(loadWeather, 10 * 60 * 1000);   // 10분마다 기상청 재조회 + DB 적재
locate().then(loadWeather);
