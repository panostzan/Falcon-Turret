/* Shared helpers for the dashboard pages: API calls, polling, the top status bar, and a small chart routine. */
const $ = id => document.getElementById(id);
const api = {
  get: async url => (await fetch(url, { cache: 'no-store' })).json(),
  post: async url => { const r = await fetch(url, { method: 'POST' }); try { return await r.json(); } catch (e) { return {}; } },
};

/* Call fn(data) now and every `ms`; fn(null) if the server cannot be reached. */
function poll(url, ms, fn) {
  const tick = async () => {
    try { fn(await api.get(url)); } catch (e) { fn(null); }
    setTimeout(tick, ms);
  };
  tick();
}

/* The status line in the top bar: video, gimbal and depth, each with a coloured dot. */
function setStatus(s) {
  const put = (id, cls, text) => { const el = $(id); if (el) { el.className = cls; el.textContent = text; } };
  if (!s) { put('st-video', 'bad', 'Server not reachable'); put('st-gimbal', '', ''); put('st-depth', '', ''); return; }
  put('st-video', s.connected ? 'on' : 'bad', s.connected ? `${s.source || 'Video'}: ${s.fps} fps` : `${s.source || 'Video'}: no picture`);
  put('st-gimbal', s.angles ? 'on' : s.error ? 'bad' : '', s.angles ? 'Gimbal connected' : s.error ? 'Gimbal error' : 'Gimbal idle');
  const d = s.depth;
  put('st-depth', s.view && s.view.depth ? (d && d.available ? 'on' : 'warn') : '', s.view && s.view.depth ? `Depth ${d ? d.fps : '–'} fps` : 'Depth off');
  document.body.classList.toggle('armed', !!(s.engage && s.engage.armed));
}

/* Line chart. sets: [{v:[numbers|null], color}], range: [min, max] or null for 0..max. */
function drawChart(canvas, sets, range) {
  if (!canvas || !canvas.getContext) return;
  const dpr = window.devicePixelRatio || 1, W = canvas.clientWidth || 300, H = canvas.clientHeight || 110;
  canvas.width = W * dpr; canvas.height = H * dpr;
  const g = canvas.getContext('2d'); g.scale(dpr, dpr); g.clearRect(0, 0, W, H);
  let [lo, hi] = range || [0, undefined];
  if (hi === undefined) { const all = sets.flatMap(s => s.v).filter(x => x != null); hi = Math.max(1, ...all) * 1.15; }
  g.strokeStyle = '#39434b'; g.lineWidth = 1; g.beginPath();
  for (let i = 1; i < 4; i++) { g.moveTo(0, H * i / 4); g.lineTo(W, H * i / 4); } g.stroke();
  for (const s of sets) {
    g.strokeStyle = s.color; g.lineWidth = 1.6; g.beginPath(); let pen = false;
    s.v.forEach((y, i) => {
      if (y == null) { pen = false; return; }
      const px = i / Math.max(1, s.v.length - 1) * W, py = H - 4 - (y - lo) / (hi - lo) * (H - 8);
      pen ? g.lineTo(px, py) : g.moveTo(px, py); pen = true;
    });
    g.stroke();
  }
}

const last = a => { for (let i = a.length - 1; i >= 0; i--) if (a[i] != null) return a[i]; return null; };
const fixed = (x, n = 0) => (x == null ? '–' : Number(x).toFixed(n));

if (typeof module !== 'undefined') module.exports = { $, api, poll, setStatus, drawChart, last, fixed };
