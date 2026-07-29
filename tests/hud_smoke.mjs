/* Headless runtime smoke test for the HUD (ui/jarvis.html).
 *
 * node --check only proves the script parses. This loads the inline script in a
 * mocked DOM/canvas via node's vm, drives the animation loop, and feeds every
 * WebSocket message type — including the new `jarvis_stream` streaming subtitle
 * and an `anthropic` config — calling the `handle()` dispatcher DIRECTLY so any
 * runtime error is surfaced (the real onmessage swallows errors in a try/catch).
 *
 *     node tests/hud_smoke.mjs
 */
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const html = fs.readFileSync(path.join(__dirname, '..', 'ui', 'jarvis.html'), 'utf8');
const blocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (blocks.length !== 1) { console.error('expected 1 script block, got', blocks.length); process.exit(1); }
const code = blocks[0];

/* ── a universal, callable "DOM node / canvas ctx" ──────────────────────────
 * Any property returns another universal node; any method call returns a sane
 * mock by name. String/number props round-trip through a backing store.        */
const NUM_KEYS = new Set(['width', 'height', 'offsetWidth', 'offsetHeight',
  'clientWidth', 'clientHeight', 'innerWidth', 'innerHeight', 'length',
  'scrollWidth', 'scrollHeight', 'devicePixelRatio', 'top', 'left', 'x', 'y']);
const ARR_KEYS = new Set(['options', 'children', 'childNodes', 'classList_']);
let rafCb = null;

function classList() {
  return { add() {}, remove() {}, toggle() {}, contains() { return false; }, replace() {} };
}
function styleObj() {
  return new Proxy({ setProperty() {}, getPropertyValue() { return ''; },
                     removeProperty() {}, item() { return ''; } }, {
    get(t, p) { return p in t ? t[p] : ''; },
    set(t, p, v) { t[p] = v; return true; },
  });
}
function callByName(name) {
  switch (name) {
    case 'getContext': return node();
    case 'measureText': return { width: 10 };
    case 'getBoundingClientRect': return { width: 800, height: 600, top: 0, left: 0, right: 800, bottom: 600 };
    case 'getImageData':
    case 'createImageData': return { data: new Uint8ClampedArray(4), width: 1, height: 1 };
    case 'createLinearGradient':
    case 'createRadialGradient':
    case 'createPattern': return { addColorStop() {} };
    case 'animate': return { finished: Promise.resolve(), cancel() {}, onfinish: null };
    case 'getPropertyValue': return '';
    default: return node();
  }
}
function node(name = 'node') {
  const fn = function () { return callByName(fn.__name); };
  fn.__name = name;
  fn.__store = {};
  return new Proxy(fn, {
    get(t, prop) {
      if (prop === Symbol.toPrimitive) return () => 0;
      if (typeof prop === 'symbol') return undefined;
      if (prop in t.__store) return t.__store[prop];
      if (prop === 'style') return (t.__store.style ||= styleObj());
      if (prop === 'classList') return (t.__store.classList ||= classList());
      if (prop === 'dataset') return (t.__store.dataset ||= {});
      if (ARR_KEYS.has(prop)) return [];
      if (NUM_KEYS.has(prop)) return 800;
      if (prop === 'parentNode' || prop === 'firstChild' || prop === 'lastChild') return node(prop);
      const child = node(prop);
      return child;
    },
    set(t, prop, val) { t.__store[prop] = val; return true; },
    has() { return true; },
  });
}

const captured = { rafFrames: 0, wsInstances: [] };
class FakeWS {
  constructor(url) { this.url = url; this.readyState = 1; captured.wsInstances.push(this); }
  send() {} close() { this.readyState = 3; }
}

const documentMock = new Proxy({}, {
  get(_, prop) {
    if (prop === 'getElementById' || prop === 'querySelector' || prop === 'createElement'
        || prop === 'createElementNS' || prop === 'getElementsByClassName'
        || prop === 'getElementsByTagName') return () => node(String(prop));
    if (prop === 'querySelectorAll') return () => [];
    if (prop === 'addEventListener' || prop === 'removeEventListener') return () => {};
    if (prop === 'body' || prop === 'documentElement' || prop === 'head'
        || prop === 'activeElement') return node(String(prop));
    if (prop === 'hidden') return false;
    if (prop === 'visibilityState') return 'visible';
    return node(String(prop));
  },
});

const sandbox = {
  document: documentMock,
  navigator: { userAgent: 'node', platform: 'test' },
  location: { href: 'http://localhost/', reload() {} },
  devicePixelRatio: 1,
  innerWidth: 1200, innerHeight: 780,
  WebSocket: FakeWS,
  requestAnimationFrame: (cb) => { rafCb = cb; return 1; },
  cancelAnimationFrame: () => {},
  setInterval: () => 0, clearInterval: () => {},
  setTimeout: () => 0, clearTimeout: () => {},
  addEventListener: () => {}, removeEventListener: () => {},
  getComputedStyle: () => ({ getPropertyValue: () => '' }),
  performance: { now: () => Date.now() },
  console,
  Date, Math, JSON, Array, Object, String, Number, Boolean, Promise,
  Uint8ClampedArray, Float32Array, Uint8Array, isNaN, parseInt, parseFloat,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

const ctx = vm.createContext(sandbox);

function fail(msg, err) {
  console.error('  FAIL', msg);
  if (err) console.error(err && err.stack ? err.stack : err);
  process.exit(1);
}

// 1) evaluate the whole HUD script (runs boot, connect(), sets up the draw loop)
try {
  vm.runInContext(code, ctx, { filename: 'jarvis.html' });
} catch (e) { fail('script threw during initial evaluation', e); }
console.log('  ok   script evaluated cleanly');

// 2) drive the animation loop through a few frames
function pump(n) {
  for (let i = 0; i < n; i++) {
    if (typeof rafCb === 'function') {
      const cb = rafCb; rafCb = null;
      try { cb(1000 + i * 16); } catch (e) { fail('animation frame threw', e); }
    }
  }
}
pump(5);
console.log('  ok   animation loop ran');

// 3) feed every message type through the real dispatcher
const handle = sandbox.handle;
if (typeof handle !== 'function') fail('handle() dispatcher not found in HUD scope');

const messages = [
  { type: 'status', text: 'online' },
  { type: 'brain', name: 'anthropic' },
  { type: 'telemetry', cpu: 22, mem: 47 },
  { type: 'state', state: 'listening' },
  { type: 'user_partial', text: '' },              // live "listening…" cue
  { type: 'user_partial', text: 'research c' },     // live partial transcription
  { type: 'user_partial', text: 'research cnc and download' },
  { type: 'spectrum', bins: Array.from({ length: 32 }, (_, i) => (i % 5) / 5), level: 0.6 },
  { type: 'state', state: 'thinking' },
  { type: 'user', text: "what's the capital of France?" },
  { type: 'jarvis_stream', text: 'Paris' },
  { type: 'jarvis_stream', text: 'Paris, sir' },
  { type: 'jarvis_stream', text: 'Paris, sir — the city of light.' },
  { type: 'jarvis', text: 'Paris, sir — the city of light.' },
  { type: 'state', state: 'speaking' },
  { type: 'pulse' },
  { type: 'state', state: 'idle' },
  { type: 'jarvis', text: 'A non-streamed reply (typewriter path).' },
  // agent activity panel — running → done, plus a second mission that fails
  { type: 'mission', mission: { id: 'm1', title: 'Researching CNC', tag: 'RESEARCH', status: 'running',
      steps: [ { label: 'Opening web, encyclopaedia & video sources', state: 'done', detail: '' },
               { label: 'Pulling PDFs off the internet', state: 'active', detail: 'Downloading PDF 2…' } ] } },
  { type: 'mission', mission: { id: 'm1', title: 'Researching CNC', tag: 'DONE', status: 'done',
      steps: [ { label: 'Opening web, encyclopaedia & video sources', state: 'done' },
               { label: 'Downloaded 3 PDFs -> Documents', state: 'done' },
               { label: 'Explaining the topic', state: 'done' } ] } },
  { type: 'mission', mission: { id: 'm2', title: 'Task: tidy the repo', tag: 'AGENT', status: 'running',
      steps: [ { label: 'Working on the task', state: 'active', detail: '' } ] } },
  { type: 'mission', mission: { id: 'm2', title: 'Task: tidy the repo', tag: 'FAILED', status: 'error',
      steps: [ { label: 'Working on the task', state: 'done' },
               { label: 'ran into a snag', state: 'done' } ] } },
  // compact corner mode on/off
  { type: 'compact', on: true, label: 'Researching CNC' },
  { type: 'compact', on: false },
  { type: 'config', config: {
      brain: 'anthropic', has_anthropic_key: true, anthropic_model: 'claude-haiku-4-5-20251001',
      has_groq_key: false, groq_model: 'llama-3.3-70b-versatile', ollama_model: 'llama3.2',
      whisper_model: 'base', tts_engine: 'edge', tts_voice: 'en-GB-RyanNeural',
      wakeword_threshold: 0.5, enable_voice: true, enable_clap: true, clap_count: 2,
      clap_sensitivity: 0.22, enable_tray: true, close_to_tray: false, user_title: 'sir' } },
  { type: 'boot_done' },
];
for (const m of messages) {
  try { handle(m); } catch (e) { fail(`handle(${m.type}) threw`, e); }
  pump(2);
}
console.log('  ok   all ' + messages.length + ' message types handled');
pump(10);
console.log('\n  HUD RUNTIME SMOKE PASSED');
