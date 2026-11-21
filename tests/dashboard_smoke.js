// Runs the dashboard's script against a stub DOM and sample server data. Run: node tests/dashboard_smoke.js
const fs = require('fs'), path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'laptop', 'dashboard.html'), 'utf8');
const script = /<script>([\s\S]*)<\/script>/.exec(html)[1];

const els = {};
const ctx = new Proxy({}, {get: (t, k) => k === 'canvas' ? {} : () => {}, set: () => true});
const el = id => els[id] || (els[id] = {id, textContent: '', innerHTML: '', className: '', style: {}, value: '',
  width: 300, height: 90, getContext: () => ctx, getBoundingClientRect: () => ({left: 0, top: 0, width: 640, height: 480})});
global.document = {getElementById: el, activeElement: null};
global.URL = {createObjectURL: () => 'blob:x', revokeObjectURL: () => {}};
const series = Array.from({length: 40}, (_, i) => 40 + i);
const state = {connected: true, fps: 14.2, tracking: true, armed: true, target: true, error: null, angles: [100, 120],
  mode: 'map', sign: {pan: 1, tilt: -1}, view: {mirror: true, vflip: false, mask: false}, cfg: {lead_ms: 250}, boot: 1,
  engage: {state: 'LOCK', armed: true, auto_scan: false, shots: 3, max_shots: 30}};
const tele = {series: {age_ms: series, proc_ms: series.map(() => 3), interval_ms: series.map(() => 66), pan: series, tilt: series.map(() => 135),
  speed: [null, 12, 30]}, stats: {age_ms: {p50: 55, p95: 70, max: 80}, proc_ms: {p50: 3, p95: 4, max: 5}, interval_ms: {p50: 66, p95: 90, max: 120},
  speed: {max: 30}}, events: [[0.4, 'LOCK -> COOLDOWN'], [1.2, 'FIRE #3']], servo_rtt_ms: 40};
global.fetch = async url => {
  if (url.startsWith('/frame.jpg')) { await new Promise(r => setTimeout(r, 100)); return {status: 204}; }
  const body = url === '/state' ? state : url === '/telemetry' ? tele : {};
  return {status: 200, json: async () => body, headers: {get: () => '1'}, blob: async () => ({})};
};
new Function(script)();
setTimeout(() => {
  const must = (cond, msg) => { if (!cond) { console.error('FAIL:', msg); process.exit(1); } };
  must(els.es.textContent === 'LOCK', 'mission state shows LOCK, got ' + els.es.textContent);
  must(els.arm.textContent === 'ARM: ON' && els.arm.className === 'armed', 'ARM button shows armed');
  must(els.shots.textContent === '3 / 30', 'shots counter, got ' + els.shots.textContent);
  must(/55 \/ 70 \/ 80/.test(els.s1.textContent) && /servo cmd 40 ms/.test(els.s1.textContent), 's1: ' + els.s1.textContent);
  must(/15 fps/.test(els.s2.textContent), 's2: ' + els.s2.textContent);
  must(els.ev.innerHTML.includes('FIRE #3'), 'event log lists the fire event');
  must(els.fps.textContent === 14.2 || els.fps.textContent === '14.2', 'fps shown');
  console.log('dashboard smoke test passed');
  process.exit(0);
}, 1500);
