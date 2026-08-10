/* ZENITH/OS v2 — core: window manager, dock, palette, telemetry, terminals.
   App definitions live in apps.js (loaded after this file), which also calls boot().
   XSS note: all dynamic values are escaped via esc() before entering any HTML
   template; rendered content is the user's own local files served from 127.0.0.1. */
'use strict';

/* ================= utilities ================= */
const $ = s => document.querySelector(s);
function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = html;
  return e;
}
const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function ts2ms(t) {
  if (t == null) return 0;
  if (typeof t === 'number') return t > 1e12 ? t : t * 1000;
  return Date.parse(t) || 0;
}
function timeAgo(t) {
  const ms = ts2ms(t);
  if (!ms) return '—';
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 60) return Math.floor(s) + 's ago';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  if (s < 86400 * 30) return Math.floor(s / 86400) + 'd ago';
  return new Date(ms).toLocaleDateString('en-US', {month:'2-digit', day:'2-digit', year:'2-digit'});
}
function countdown(t) {
  const ms = ts2ms(t);
  if (!ms) return '—';
  const s = (ms - Date.now()) / 1000;
  if (s <= 0) return 'due';
  if (s < 3600) return 'in ' + Math.ceil(s / 60) + 'm';
  if (s < 86400) return 'in ' + Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
  return 'in ' + Math.floor(s / 86400) + 'd';
}
function fmtBytes(n) {
  if (n == null) return '—';
  if (n < 1024) return n + 'B';
  if (n < 1048576) return (n / 1024).toFixed(0) + 'KB';
  return (n / 1048576).toFixed(1) + 'MB';
}
function fmtNum(n) {
  if (n == null) return '—';
  if (n < 1000) return String(n);
  if (n < 1e6) return (n / 1e3).toFixed(1) + 'k';
  return (n / 1e6).toFixed(2) + 'M';
}
function fmtModel(m) {
  return String(m || '').replace(/^claude-/, '').replace(/-\d{8}$/, '');
}

/* ---- model tier chips (fable gold / opus violet / sonnet blue / haiku green) ---- */
const MODELS = [
  { id: 'claude-haiku-4-5-20251001', label: 'haiku', tier: 'haiku' },
  { id: 'claude-sonnet-5', label: 'sonnet', tier: 'sonnet' },
  { id: 'claude-opus-4-8', label: 'opus 4.8', tier: 'opus' },
  { id: 'claude-fable-5', label: 'FABLE 5', tier: 'fable' },
];
function tierOf(m) {
  const s = String(m || '').toLowerCase();
  if (s.includes('fable')) return 'fable';
  if (s.includes('opus')) return 'opus';
  if (s.includes('sonnet')) return 'sonnet';
  if (s.includes('haiku')) return 'haiku';
  return '';
}
function modelLabel(m) {
  const spec = MODELS.find(x => x.id === m || x.tier === tierOf(m));
  return spec ? spec.label : (fmtModel(m) || '?');
}
function modelChip(m) {
  const tier = tierOf(m);
  return `<span class="chip ${tier ? 'tier-' + tier : ''}">${esc(modelLabel(m))}</span>`;
}
/* ---- agent chips (P2): claude=cyan, codex=green, aider=violet ---- */
function agentChip(a) {
  if (!a) return '';
  if (a === 'provider') return '<span class="chip vi">local</span>';
  const cls = { claude: 'cy', codex: 'on', aider: 'vi' }[a] || '';
  return `<span class="chip ${cls}">${esc(a)}</span>`;
}

/* ================= UI sounds (Settings → uiSounds; off by default) ================= */
let _actx = null;
function blip(freq = 880, dur = 0.06, vol = 0.05) {
  try {
    if (!Settings.load().uiSounds) return;
    _actx = _actx || new (window.AudioContext || window.webkitAudioContext)();
    const o = _actx.createOscillator(), g = _actx.createGain();
    o.type = 'sine'; o.frequency.value = freq;
    g.gain.setValueAtTime(vol, _actx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.0001, _actx.currentTime + dur);
    o.connect(g); g.connect(_actx.destination);
    o.start(); o.stop(_actx.currentTime + dur + 0.02);
  } catch (e) { /* no audio available */ }
}

/* ================= toasts (deduped, user-action only) ================= */
const _toastSeen = new Map();
function toast(msg, kind) {
  const now = Date.now();
  const last = _toastSeen.get(msg);
  if (last && now - last < 5000) return;   // dedupe: same message within 5s
  _toastSeen.set(msg, now);
  blip(kind === 'err' ? 300 : 880);
  const t = el('div', 'toast' + (kind ? ' ' + kind : ''), esc(msg));
  $('#toasts').appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; t.style.transition = 'opacity .4s'; }, 3600);
  setTimeout(() => t.remove(), 4100);
}

/* ================= fetch ================= */
async function api(path, opts) {
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({ error: 'bad response' }));
  if (r.status === 428 && data.gate) return { __gate: data.gate };  // risk gate → jpost modal
  if (!r.ok) throw new Error(data.error || r.status);
  return data;
}
// {silent:true} → no toast on failure (used by ALL periodic polls)
async function apiSafe(path, opts, cfg) {
  try { return await api(path, opts); }
  catch (e) {
    if (!(cfg && cfg.silent)) toast(path.split('?')[0] + ': ' + e.message, 'err');
    return null;
  }
}

/* ================= mini markdown ================= */
function inlineMD(s) {
  s = esc(s);
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|\W)\*([^*\n]+)\*(?=\W|$)/g, '$1<em>$2</em>');
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, t, u) =>
    /^https?:/.test(u) ? `<a href="${u}" target="_blank">${t}</a>` : `<span style="color:var(--cyan-soft)">${t}</span>`);
  return s;
}
function renderMD(src) {
  const lines = String(src || '').split('\n');
  const out = [];
  let i = 0, list = null, para = [];
  const flushPara = () => { if (para.length) { out.push('<p>' + inlineMD(para.join(' ')) + '</p>'); para = []; } };
  const flushList = () => { if (list) { out.push('</' + list + '>'); list = null; } };
  while (i < lines.length) {
    const L = lines[i];
    if (/^```/.test(L)) {
      flushPara(); flushList();
      const buf = []; i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++;
      out.push('<pre><code>' + esc(buf.join('\n')) + '</code></pre>');
      continue;
    }
    const h = L.match(/^(#{1,4})\s+(.*)/);
    if (h) { flushPara(); flushList(); out.push(`<h${h[1].length}>${inlineMD(h[2])}</h${h[1].length}>`); i++; continue; }
    if (/^\s*[-*+]\s+/.test(L)) {
      flushPara();
      if (list !== 'ul') { flushList(); out.push('<ul>'); list = 'ul'; }
      out.push('<li>' + inlineMD(L.replace(/^\s*[-*+]\s+/, '')) + '</li>'); i++; continue;
    }
    if (/^\s*\d+\.\s+/.test(L)) {
      flushPara();
      if (list !== 'ol') { flushList(); out.push('<ol>'); list = 'ol'; }
      out.push('<li>' + inlineMD(L.replace(/^\s*\d+\.\s+/, '')) + '</li>'); i++; continue;
    }
    if (/^\s*\|/.test(L) && /^\s*\|/.test(lines[i + 1] || '')) {
      flushPara(); flushList();
      const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) {
        if (!/^\s*\|[\s:|-]+\|?\s*$/.test(lines[i]))
          rows.push(lines[i].replace(/^\s*\||\|\s*$/g, '').split('|').map(c => inlineMD(c.trim())));
        i++;
      }
      if (rows.length) {
        let t = '<table><tr>' + rows[0].map(c => `<th>${c}</th>`).join('') + '</tr>';
        for (const r of rows.slice(1)) t += '<tr>' + r.map(c => `<td>${c}</td>`).join('') + '</tr>';
        out.push(t + '</table>');
      }
      continue;
    }
    if (/^\s*>\s?/.test(L)) { flushPara(); flushList();
      out.push('<blockquote>' + inlineMD(L.replace(/^\s*>\s?/, '')) + '</blockquote>'); i++; continue; }
    if (/^\s*(---+|\*\*\*+)\s*$/.test(L)) { flushPara(); flushList(); out.push('<hr>'); i++; continue; }
    if (!L.trim()) { flushPara(); flushList(); i++; continue; }
    para.push(L.trim()); i++;
  }
  flushPara(); flushList();
  return out.join('\n');
}

/* ================= icons ================= */
const I = {
  sessions: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M20 15a2 2 0 01-2 2H8l-4 3V6a2 2 0 012-2h12a2 2 0 012 2z"/><path d="M8 9h8M8 12.5h5"/></svg>',
  projects: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="7" width="18" height="13" rx="2"/><path d="M8 7V5.5A2.5 2.5 0 0110.5 3h3A2.5 2.5 0 0116 5.5V7M3 12.5h18"/></svg>',
  files: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M8 3h6l4 4v13a1 1 0 01-1 1H8a1 1 0 01-1-1V4a1 1 0 011-1z"/><path d="M14 3v4h4M10 12h6M10 15.5h6M10 8.5h2"/></svg>',
  globe: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.5 3.5 6 3.5 9s-1 6.5-3.5 9c-2.5-2.5-3.5-6-3.5-9s1-6.5 3.5-9z"/></svg>',
  settings: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M6 4v5M6 13v7M12 4v9M12 17v3M18 4v3M18 11v9"/><circle cx="6" cy="11" r="2"/><circle cx="12" cy="15" r="2"/><circle cx="18" cy="9" r="2"/></svg>',
  command: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M9 7.5A2.5 2.5 0 106.5 10H9V7.5zM15 7.5A2.5 2.5 0 1117.5 10H15V7.5zM9 16.5A2.5 2.5 0 116.5 14H9v2.5zM15 16.5a2.5 2.5 0 102.5-2.5H15v2.5z"/><rect x="9" y="10" width="6" height="4" rx="0.5"/></svg>',
  memory: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="3"/><circle cx="5" cy="5" r="2"/><circle cx="19" cy="5" r="2"/><circle cx="5" cy="19" r="2"/><circle cx="19" cy="19" r="2"/><path d="M6.5 6.5L10 10m4 0l3.5-3.5M6.5 17.5L10 14m4 0l3.5 3.5"/></svg>',
  agents: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="5" y="7" width="14" height="11" rx="2"/><circle cx="9.5" cy="12" r="1.2" fill="currentColor"/><circle cx="14.5" cy="12" r="1.2" fill="currentColor"/><path d="M12 7V4m0 0h3M9 21h6"/></svg>',
  ops: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M13 2L4 14h6l-1 8 9-12h-6z"/></svg>',
  loops: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M16.5 1.5L20.5 5.5 16.5 9.5"/><path d="M3.5 11V9.5A4 4 0 017.5 5.5H20.5M7.5 22.5L3.5 18.5 7.5 14.5"/><path d="M20.5 13v1.5a4 4 0 01-4 4H3.5"/></svg>',
  research: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>',
  swarm: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="5" r="2.4"/><circle cx="5" cy="18" r="2.4"/><circle cx="19" cy="18" r="2.4"/><path d="M12 7.4V12M12 12L6.5 16M12 12l5.5 4"/></svg>',
  models: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="7" y="7" width="10" height="10" rx="2"/><path d="M10 7V4M14 7V4M10 20v-3M14 20v-3M7 10H4M7 14H4M20 10h-3M20 14h-3"/></svg>',
  fleet: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="6" y="6" width="12" height="12" rx="2"/><rect x="9.5" y="9.5" width="5" height="5" rx="1"/><path d="M9 6V3M15 6V3M9 21v-3M15 21v-3M6 9H3M6 15H3M21 9h-3M21 15h-3"/></svg>',
  dashboard: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="3" width="8" height="10" rx="1.5"/><rect x="13" y="3" width="8" height="6" rx="1.5"/><rect x="13" y="12" width="8" height="9" rx="1.5"/><rect x="3" y="16" width="8" height="5" rx="1.5"/></svg>',
  feed: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 11a9 9 0 019 9M4 4a16 16 0 0116 16"/><circle cx="5" cy="19" r="1.6" fill="currentColor"/></svg>',
  tile: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="3" width="8" height="8" rx="1.5"/><rect x="13" y="3" width="8" height="8" rx="1.5"/><rect x="3" y="13" width="8" height="8" rx="1.5"/><rect x="13" y="13" width="8" height="8" rx="1.5"/></svg>',
  doc: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M6 2h9l5 5v15H6z"/></svg>',
  app: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/><rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/></svg>',
  mem: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3"/></svg>',
  term: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="3" width="18" height="18" rx="2.5"/><path d="M7 8.5l3.5 3.5L7 15.5M12.5 15.5H17"/></svg>',
  chat: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 5a2 2 0 012-2h12a2 2 0 012 2v9a2 2 0 01-2 2H9l-5 4z"/></svg>',
  gear: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="3"/><path d="M12 2v3m0 14v3M2 12h3m14 0h3M5 5l2 2m10 10l2 2M19 5l-2 2M5 19l2-2"/></svg>',
  obs: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="2.6"/><path d="M12 1.5V7M12 17v5.5M1.5 12H7M17 12h5.5"/></svg>',
  ab: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="4" width="7.5" height="16" rx="1.5"/><rect x="13.5" y="4" width="7.5" height="16" rx="1.5"/><path d="M5.1 15.5l1.65-6h.5l1.65 6M5.7 13.6h2.6M15.7 9.5v6M15.7 9.5h1.7a1.4 1.4 0 010 2.8h-1.7M15.7 12.3h1.9a1.6 1.6 0 010 3.2h-1.9"/></svg>',
  watch: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M2 12s3.5-6.5 10-6.5S22 12 22 12s-3.5 6.5-10 6.5S2 12 2 12z"/><circle cx="12" cy="12" r="2.8"/><path d="M12 3v2M12 19v2M4 8l1.4.9M18.6 15.1L20 16"/></svg>',
};

/* ================= window manager ================= */
const ARR_MODES = ['columns', 'tiled', 'cascade'];
const ARR_LABEL = { columns: 'Columns', tiled: 'Tiles', cascade: 'Cascade' };
const ARR_GLYPH = { columns: '▥', tiled: '▦', cascade: '⧉' };
// view icons for the dock arrange-mode button (match the I.* icon style: 24-vb, stroke, rx)
const ARR_ICON = {
  columns: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="3" width="4.6" height="18" rx="1.3"/><rect x="9.7" y="3" width="4.6" height="18" rx="1.3"/><rect x="16.4" y="3" width="4.6" height="18" rx="1.3"/></svg>',
  tiled: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="3" width="8" height="8" rx="1.5"/><rect x="13" y="3" width="8" height="8" rx="1.5"/><rect x="3" y="13" width="8" height="8" rx="1.5"/><rect x="13" y="13" width="8" height="8" rx="1.5"/></svg>',
  cascade: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="4" y="4" width="12" height="12" rx="1.8"/><rect x="9" y="9" width="12" height="12" rx="1.8"/></svg>',
};
const WM = {
  z: 20,
  wins: new Map(),
  activeDesktop: 1,       // current virtual desktop (workspace)
  focusedWin: null,       // the currently-focused window (for send-to-desktop shortcuts)
  open(appId, opts) {
    // an id that was consolidated into a module → open the module and switch to its tab (cross-app nav,
    // palette actions, layout restore all keep working with the old ids)
    const m = (typeof MODULE_OF !== 'undefined') && MODULE_OF[appId];
    if (m) {
      const mod = APPS.find(a => a.id === m.m);
      if (mod) {
        // Phase-3 gating: whole module hidden → no window; target tab hidden → open the first visible
        // tab. Either way a toast points at Settings. open() still returns a window/undefined as before.
        if (!Caps.appVisible(mod)) { this._capToast(appId); return; }
        const w = this.spawn(mod, opts);
        let tab = m.t;
        if (!Caps.tabVisible(mod.id, m.t)) { this._capToast(appId); tab = this._firstVisibleTab(mod); }
        if (w && w._showTab && tab) w._showTab(tab);
        return w;
      }
    }
    const app = APPS.find(a => a.id === appId);
    if (!app) return;
    if (app.drawer) { Drawer.open(app); return; }   // drawer apps (Settings) fly out from the left
    if (!Caps.appVisible(app)) { this._capToast(appId); return; }   // Phase-3: hidden by user/integration
    return this.spawn(app, opts);
  },
  _firstVisibleTab(mod) {
    const t = (mod.tabs || []).find(t => Caps.tabVisible(mod.id, t.key));
    return t ? t.key : null;
  },
  _capToast(appId) {   // "<Feature> is disabled — enable in Settings → Integrations"
    const m = (typeof MODULE_OF !== 'undefined') && MODULE_OF[appId];
    let label;
    if (m) { const mod = APPS.find(a => a.id === m.m); const tab = mod && (mod.tabs || []).find(t => t.key === m.t); label = tab && tab.label; }
    if (!label) { const app = APPS.find(a => a.id === appId); label = (app && app.name) || appId; }
    toast(label + ' is disabled — enable in Settings → Integrations', 'err');
  },
  spawn(app, opts) {   // also used directly for multi-instance windows (terminals, chats)
    const existing = this.wins.get(app.id);
    if (existing) {
      existing.min = false;
      existing.desktop = this.activeDesktop;   // clicking its dock icon brings it to the current desktop
      existing.el.style.display = 'flex';
      this.refreshDeskControl(existing);
      this.focus(existing);
      Dock.update();
      return existing;
    }
    const win = { app, min: false, collapsed: false, arrange: true, timers: [], el: null,
      desktop: (opts && opts.desktop) || this.activeDesktop };
    const w = el('div', 'win');
    if (app.cls) w.classList.add(app.cls);
    if (app.accent) w.style.setProperty('--acc', app.accent);
    const geo = this.loadGeo(app.id, app);
    win.arrange = geo.arrange !== false;
    // exact: restore the saved spot verbatim (layout persistence). otherwise nudge off any overlap.
    // A tiled window's saved position IS its deterministic grid slot, so on a layout restore it goes
    // back verbatim even when exact-position persistence is off — that's what makes the arrangement
    // (autolayout) persist independently of the "restore exact positions" toggle.
    const useExact = (opts && opts.exact) || (opts && opts.restore && geo.tiled);
    const pos = useExact ? { x: geo.x, y: geo.y } : this.avoidOverlap(geo.x, geo.y);
    Object.assign(w.style, { left: pos.x + 'px', top: pos.y + 'px',
      width: geo.w + 'px', height: geo.h + 'px', zIndex: ++this.z });

    const head = el('div', 'win-head');
    // The app icon IS the auto-arrange toggle: lit = this window joins the arrangement,
    // dimmed = it sits it out. A <button> so dragify's button guard keeps a click from
    // starting a window drag, and so the header dblclick-to-collapse skips it too.
    head.innerHTML = `<button class="wicon warr${win.arrange ? ' on' : ''}">${app.icon}</button>
      <span class="wtitle">${esc(app.name)}</span><span class="wsub"></span>
      <span class="wvlabel">${esc(app.name)}</span>`;
    const btns = el('div', 'win-btns');
    const deskCtl = el('div', 'win-desks');   // 1·2·N chips to send this window to a desktop
    win._deskCtl = deskCtl;
    const bRen = el('button', 'wren', '✎');   // fast label attached below, once win exists
    bRen.style.cssText = 'background:none;border:none;color:var(--faint);cursor:pointer;font-size:11px;padding:0 3px;margin:0 2px;line-height:1;vertical-align:middle;opacity:.55;transition:.12s;flex:none';
    bRen.onmouseenter = () => { bRen.style.color = 'var(--acc)'; bRen.style.opacity = '1'; };
    bRen.onmouseleave = () => { bRen.style.color = 'var(--faint)'; bRen.style.opacity = '.55'; };
    const bArr = head.querySelector('.wicon');   // the icon, now doubling as the toggle
    const bColn = el('button', '', '◧'); bColn.title = 'Collapse to column strip';
    const bMax = el('button', '', '▢'), bClose = el('button', 'close', '✕');
    // ❐ default-size: a double-box (overlapping-squares) glyph reads clearly as "window size"
    const bNorm = el('button', 'wnorm', '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="4" y="8" width="12" height="12" rx="1.5"/><path d="M8 8V4h12v12h-4"/></svg>');
    bNorm.title = 'Default size';
    btns.append(deskCtl, bColn, bNorm, bMax, bClose);
    head.appendChild(btns);
    this.refreshDeskControl(win);
    // unchanged behaviour, new home — same toggle, saveGeo, and toast as the old ▦ button
    bArr.onclick = e => { e.stopPropagation(); this.setArrange(win, !win.arrange);
      toast(app.name + (win.arrange ? ' will auto-arrange' : ' excluded from auto-arrange'), 'ok'); };
    attachTip(bArr, () => win.arrange
      ? `${app.name} — in auto-arrange (click to exclude)`
      : `${app.name} — excluded from auto-arrange (click to include)`);
    bColn.onclick = e => { e.stopPropagation(); this.setColCollapsed(win, !win.colCollapsed); };
    bRen.onclick = e => { e.stopPropagation(); this.renameWindow(win); };
    head.querySelector('.wvlabel').onclick = e => { e.stopPropagation(); this.setColCollapsed(win, false); };
    const body = el('div', 'win-body');
    const rez = el('div', 'win-resize win-rz se');   // visible SE grip = one of the 8 handles
    w.append(head, body, rez);
    const rzHandles = { se: rez };
    ['n', 's', 'e', 'w', 'nw', 'ne', 'sw'].forEach(d => {
      const h = el('div', 'win-rz ' + d); w.appendChild(h); rzHandles[d] = h; });
    $('#desktop').appendChild(w);
    win.el = w; win.body = body; win.head = head;
    win.sub = head.querySelector('.wsub');
    this.wins.set(app.id, win);

    // custom window title — double-click the title text to rename (persisted per window)
    const wtitleEl = head.querySelector('.wtitle');
    // shows the full name (it ellipsizes when the window is narrow) — not a how-to
    attachTip(wtitleEl, () => win.customTitle || win.app.name);
    attachTip(bRen, 'Rename window');
    wtitleEl.after(bRen);   // ✎ sits directly to the right of the title text
    if (geo.title) this.applyWinTitle(win, geo.title);

    bClose.onclick = e => { e.stopPropagation(); this.close(win); };
    bNorm.onclick = e => { e.stopPropagation(); this.sizeDefault(win); };
    bMax.onclick = e => { e.stopPropagation(); this.maximize(win); };
    head.addEventListener('dblclick', e => {
      if (e.target.closest('button')) return;                                  // buttons do their own thing
      if (e.target.closest('.wtitle, .wsub')) { this.renameWindow(win); return; }  // title text → rename
      // (.wicon is the arrange toggle now — the button guard above already skipped it)
      this.setColCollapsed(win, !win.colCollapsed);                            // rest of the header → collapse to a vertical column strip
    });
    w.addEventListener('pointerdown', () => this.focus(win));
    this.dragify(win, head);
    const RZ = { n: { t: 1 }, s: { b: 1 }, e: { r: 1 }, w: { l: 1 },
      nw: { t: 1, l: 1 }, ne: { t: 1, r: 1 }, sw: { b: 1, l: 1 }, se: { b: 1, r: 1 } };
    Object.keys(rzHandles).forEach(d => this.resizify(win, rzHandles[d], RZ[d]));
    this.focus(win);
    Dock.update();
    this.persistLayout();
    try { app.render(body, win); }
    catch (e) { body.textContent = 'render error: ' + e.message; }
    // NOT restoring geo.collapsed: the ▾ roll-up button is gone, so a window that came
    // back collapsed would have no affordance to expand it again. Layouts saved before
    // the button was removed still carry the flag — ignore it rather than strand them.
    // (◧ column-collapse is still restored: its strip expands on click.)
    if (geo.colCollapsed) { win.prevWidth = geo.w + 'px'; this.setColCollapsed(win, true); }
    // restore arrangement membership on a real layout-restore, so the tiled/columns arrangement
    // (and pop-out/drag reflow) survives a reload — gated on `restore`, NOT on the exact-position
    // toggle, so autolayout persists even with "restore exact positions" turned off.
    if (geo.tiled && opts && opts.restore) { win.el.classList.add('tiled'); if (win.desktop === this.activeDesktop) this.arranged = true; }
    if (win.desktop !== this.activeDesktop) win.el.style.display = 'none';   // opened onto another desktop
    else if (this.arranged && win.arrange !== false && !win.collapsed && !(opts && opts.restore)) this.tile(null, {});  // squeeze newcomer in (not during a layout restore)
    return win;
  },
  setCollapsed(win, val) {
    win.collapsed = val;
    if (val) {
      win.prevHeight = win.el.style.height;   // remember expanded height
      win.el.classList.add('collapsed');
    } else {
      win.el.classList.remove('collapsed');
      win.el.style.height = win.prevHeight || (Math.min(520, innerHeight - 180) + 'px');
    }
    this.saveGeo(win);
  },
  /* Single writer for auto-arrange membership, so the .wicon toggle can never drift
     out of sync with the flag it displays (it is the only indicator of this state). */
  setArrange(win, val) {
    win.arrange = !!val;
    const ic = win.head && win.head.querySelector('.wicon');
    if (ic) ic.classList.toggle('on', win.arrange);
    this.saveGeo(win);
  },
  setColCollapsed(win, val) {
    win.colCollapsed = val;
    if (val) win.prevWidth = win.prevWidth || win.el.style.width;
    win.el.classList.toggle('col-collapsed', val);
    if ((Settings.load().arrangeMode || 'tiled') === 'columns') {
      this.tile(null, { mode: 'columns' });   // reflow columns to reclaim/return the space
    } else if (!val) {
      win.el.style.width = win.prevWidth || (Math.min(760, innerWidth - 120) + 'px');
    }
    if (!val) win.prevWidth = null;   // done expanding; drop the remembered width
    this.saveGeo(win);
  },
  focus(win) {
    for (const w of this.wins.values()) w.el.classList.remove('focus');
    win.el.classList.add('focus');
    win.el.style.zIndex = ++this.z;
    this.focusedWin = win;
    // let the window reclaim its own inner focus (a terminal refocuses its xterm textarea,
    // so ⌘V/paste reaches the PTY however the window was activated — titlebar, ⌘K, dock, desktop switch)
    if (typeof win.onActivate === 'function') { try { win.onActivate(); } catch (e) { /* ignore */ } }
  },
  close(win) {
    const wasTiled = win.el.classList.contains('tiled') && this.arranged;
    win.timers.forEach(clearInterval);
    if (win.cleanup) { try { win.cleanup(); } catch (e) { /* already gone */ } }
    win.el.classList.add('closing');
    setTimeout(() => win.el.remove(), 170);
    this.wins.delete(win.app.id);
    if (wasTiled) this.reflowGap(win);   // survivors fill the slot it vacated
    Dock.update();
    this.persistLayout();
  },
  minimize(win) {
    const wasTiled = win.el.classList.contains('tiled') && this.arranged;
    win.min = true; win.el.style.display = 'none';
    if (wasTiled) this.reflowGap(win);   // same freed slot as a close
    Dock.update();
    this.persistLayout();
  },
  maximize(win) {
    this.arranged = false;
    win._popped = null;
    win.el.classList.remove('tiled');
    const w = win.el;
    if (win.maxed) {
      Object.assign(w.style, win.maxed);
      win.maxed = null;
    } else {
      win.maxed = { left: w.style.left, top: w.style.top, width: w.style.width, height: w.style.height };
      const { w: W, h: H } = this.desktopBox();
      Object.assign(w.style, { left: '8px', top: '8px',
        width: (W - 16) + 'px', height: (H - 16) + 'px' });
    }
  },
  // header ❐ button: restore the window to its default size (keeps its center, clamped
  // on-screen), leaving any arrangement/collapse/maximize state.
  sizeDefault(win) {
    if (win._popped) {   // second click: toggle back to where it was, re-expanding any arrangement it left
      const p = win._popped; win._popped = null;
      const affected = this.visibleWins();
      affected.forEach(w => w.el.classList.add('reflow'));
      Object.assign(win.el.style, p.geo);
      if (p.wasTiled && p.arrMode !== 'cascade') {
        win.el.classList.add('tiled'); win.arrange = true; this.arranged = true;
        this.tile(null, { mode: p.arrMode });   // window rejoins its slot; the group makes room again
      } else { this.arranged = p.wasArranged; this.saveGeo(win); }
      this.focus(win);
      this._clearReflow(affected.includes(win) ? affected : affected.concat(win));
      return;
    }
    const arrMode = Settings.load().arrangeMode || 'tiled';
    const wasTiled = win.el.classList.contains('tiled');
    const s = win.el.style, d = this.defaultSize(win.app), { w: W, h: H } = this.desktopBox();
    win._popped = { geo: { left: s.left, top: s.top, width: s.width, height: s.height }, wasTiled, wasArranged: this.arranged, arrMode };
    win.maxed = null;
    win.collapsed = false; win.colCollapsed = false;
    win.el.classList.remove('tiled', 'collapsed', 'col-collapsed');
    // center it — but cascade-offset if another popped-out (free) window is already parked there
    let left = Math.round((W - d.w) / 2), top = Math.round((H - d.h) / 2);
    const freeWins = () => [...this.wins.values()].filter(o => o !== win && !o.min &&
      o.el.style.display !== 'none' && !o.el.classList.contains('tiled'));
    for (let g = 0; g < 12 && freeWins().some(o =>
      Math.abs(parseInt(o.el.style.left) - left) < 40 && Math.abs(parseInt(o.el.style.top) - top) < 40); g++) {
      left += 42; top += 42;
    }
    left = Math.max(0, Math.min(left, W - d.w)); top = Math.max(0, Math.min(top, H - d.h));
    win.el.classList.add('reflow');
    Object.assign(s, { left: left + 'px', top: top + 'px', width: d.w + 'px', height: d.h + 'px', zIndex: ++this.z });
    // honor the drag setting for the window it left behind: reflow => the rest close the gap
    // (compact, as if dragged out); free => leave the void, others stay put.
    if (wasTiled) this.reflowGap(win);
    this._clearReflow([win]);
    this.saveGeo(win);
    this.focus(win);
  },
  loadGeo(id, app) {
    try {
      const g = JSON.parse(localStorage.getItem('zen.geo.' + (app.geoKey || id)));
      if (g && g.w >= 60) {
        // CLAMP an off-canvas saved position back to reachable instead of DISCARDING the whole geo —
        // discarding used to pop a window out to a cascade spot (losing a manually-placed layout, or a
        // tiled slot from a wider screen). Keep the saved size + flags; just pull the corner on-screen so
        // a grab handle stays reachable. Positions are relative to the dock-free #desktop canvas.
        const { w: W, h: H } = this.desktopBox();
        g.x = Math.max(0, Math.min(g.x, Math.max(0, W - 80)));
        g.y = Math.max(0, Math.min(g.y, Math.max(0, H - 44)));
        return g;
      }
    } catch (e) { /* fresh geometry */ }
    const n = this.wins.size, d = this.defaultSize(app);
    return { x: 60 + n * 36, y: 10 + n * 30, w: d.w, h: d.h };
  },
  // {w,h} in px for a new window, from the default-size setting + current dock-free canvas.
  defaultSize(app) {
    const { w: W, h: H } = this.desktopBox();
    const ds = Settings.load().defaultWinSize || { mode: 'preset', preset: 'app' };
    const FRAC = { quarter: [0.5, 0.5], third: [0.6, 0.62], half: [0.75, 0.8] };
    let fw, fh;
    if (ds.mode === 'capture' && ds.fw) { fw = ds.fw; fh = ds.fh; }
    else if (ds.mode === 'preset' && FRAC[ds.preset]) { [fw, fh] = FRAC[ds.preset]; }
    if (fw) return { w: Math.round(Math.max(0.2, Math.min(1, fw)) * W), h: Math.round(Math.max(0.2, Math.min(1, fh)) * H) };
    return { w: Math.min(app.w || 760, W - 120), h: Math.min(app.h || 520, H - 120) };   // per-app default
  },
  saveGeo(win) {
    const s = win.el.style;
    const h = win.collapsed ? (parseInt(win.prevHeight) || 480) : parseInt(s.height);
    const w = win.colCollapsed ? (parseInt(win.prevWidth) || 760) : parseInt(s.width);
    localStorage.setItem('zen.geo.' + (win.app.geoKey || win.app.id), JSON.stringify({
      x: parseInt(s.left), y: parseInt(s.top), w, h, desktop: win.desktop || 1,
      arrange: win.arrange !== false, collapsed: !!win.collapsed, colCollapsed: !!win.colCollapsed,
      title: win.customTitle || undefined,             // user-renamed window title (persists across reloads)
      tiled: win.el.classList.contains('tiled') }));   // remember arrangement membership across reloads
  },
  /* Give a window a directory context: two titlebar buttons that launch a new agent
     session / a new shell in that same dir. Any app whose window is scoped to a
     folder can opt in with one call; pass a falsy cwd to remove them again.
     Idempotent — re-calling just retargets the existing buttons. */
  setWinCwd(win, cwd) {
    if (!win || !win.head) return;
    let box = win.head.querySelector('.win-cwd');
    if (!cwd) { if (box) box.remove(); win.cwd = null; return; }
    win.cwd = cwd;
    const short = cwd.replace(/\/+$/, '').split('/').filter(Boolean).pop() || cwd;
    if (!box) {
      box = el('div', 'win-cwd');
      // icon-only: the header is crowded and the hover label carries the meaning.
      // Same glyphs as the dock/header icons so "new terminal" reads as a terminal.
      const bAg = el('button', 'wc-ico', I.agents);
      const bSh = el('button', 'wc-ico', I.term);
      bAg.onclick = e => { e.stopPropagation(); launchTerm(win.cwd, defaultAgent()); };
      bSh.onclick = e => { e.stopPropagation(); launchTerm(win.cwd, 'shell'); };
      box.append(bAg, bSh);
      const btns = win.head.querySelector('.win-btns');
      btns ? btns.before(box) : win.head.appendChild(box);
    }
    const [bAg, bSh] = box.children;
    // the hover label tracks the *default* agent, which the user can change in Settings
    attachTip(bAg, () => `New ${AGENT_LABEL[defaultAgent()] || defaultAgent()} session in ${short}`);
    attachTip(bSh, `New terminal in ${short}`);
  },
  // set (or clear) a window's custom title; also updates the vertical column label
  applyWinTitle(win, text) {
    win.customTitle = text || null;
    const show = text || win.app.name;
    const t = win.head.querySelector('.wtitle'); if (t) t.textContent = show;
    const v = win.head.querySelector('.wvlabel'); if (v) v.textContent = show;
  },
  // inline-rename a window: swap the title text for an input, commit on Enter/blur, cancel on Esc
  renameWindow(win) {
    const t = win.head.querySelector('.wtitle');
    if (t.querySelector('input')) return;
    const inp = el('input', 'win-rename');
    inp.value = win.customTitle || win.app.name;
    // The title span normally clips with ellipsis — unclip it and grow the field to fit the full text.
    const prevOverflow = t.style.overflow, prevMax = t.style.maxWidth;
    t.style.overflow = 'visible'; t.style.maxWidth = 'none';
    const fit = () => { inp.style.width = Math.max(14, inp.value.length + 3) + 'ch'; };
    inp.oninput = fit;
    t.replaceChildren(inp);
    fit();
    inp.focus(); inp.select();
    let done = false;
    const restore = () => { t.style.overflow = prevOverflow; t.style.maxWidth = prevMax; };
    const finish = save => {
      if (done) return; done = true;
      restore();
      if (save) {
        const v = inp.value.trim();
        this.applyWinTitle(win, (v && v !== win.app.name) ? v : null);
        this.saveGeo(win);
      } else { this.applyWinTitle(win, win.customTitle); }
    };
    inp.onkeydown = e => { e.stopPropagation();
      if (e.key === 'Enter') { e.preventDefault(); inp.blur(); }
      else if (e.key === 'Escape') { e.preventDefault(); done = true; restore(); this.applyWinTitle(win, win.customTitle); } };
    inp.onblur = () => finish(true);
    inp.onpointerdown = e => e.stopPropagation();   // don't start a window drag
    inp.onclick = e => e.stopPropagation();
  },
  dragify(win, head) {
    head.addEventListener('pointerdown', e => {
      if (e.target.closest('button')) return;
      const s = win.el.style;
      const ox = e.clientX - parseInt(s.left), oy = e.clientY - parseInt(s.top);
      let moved = false;
      const arrMode = Settings.load().arrangeMode || 'tiled';
      const dragMode = Settings.load().dragMode || 'reflow';
      // REFLOW ("balls in a pit"): inside an arranged layout the group compacts to fill the void when
      // a window is pulled out, and parts to make room when it hovers back over the band — no snap
      // needed. FREE: the window drops wherever you like (void stays) and only rejoins on a snap.
      let sortable = null;
      if (dragMode === 'reflow' && this.arranged && arrMode !== 'cascade') {
        const group = this.visibleWins().filter(w => w !== win && w.el.classList.contains('tiled')).sort(this.arrangeCmp(arrMode));
        if (group.length >= 1) { sortable = { mode: arrMode, group, engaged: null, k: 0 }; group.forEach(o => o.el.classList.add('reflow')); win.el.classList.add('lifted'); }
      }
      let lastSnapped = false;   // (free mode) did the final position land on a snap line?
      const placeGroup = (slots, k) => sortable.group.forEach((o, j) => {   // k<0 => compact (no gap)
        const sl = slots[k < 0 ? j : (j < k ? j : j + 1)];
        Object.assign(o.el.style, { left: sl.left + 'px', top: sl.top + 'px', width: sl.w + 'px', height: sl.h + 'px' });
      });
      try { head.setPointerCapture(e.pointerId); } catch (err) { /* synthetic/absent pointer */ }
      const move = ev => {
        if (Math.abs(ev.clientX - e.clientX) + Math.abs(ev.clientY - e.clientY) > 4) { moved = true; win._popped = null; }
        const x = Math.max(-200, Math.min(innerWidth - 90, ev.clientX - ox));
        const y = Math.max(0, Math.min(innerHeight - 46 - 44, ev.clientY - oy));
        if (sortable) {                     // dragged floats with the cursor; the group reflows around it
          s.left = x + 'px'; s.top = y + 'px';
          if (!moved) return;
          win.el.classList.remove('tiled');
          const dcx = x + (parseInt(s.width) || 0) / 2, dcy = y + (parseInt(s.height) || 0) / 2;
          // pass window objects so collapsed strips keep strip width; the dragged (`win`) is a full column
          const insert = k => { const a = sortable.group.slice(); a.splice(k, 0, win); return a; };
          const prov = this.slotGeom([...sortable.group, win], sortable.mode);   // provisional: locate the slot
          let engaged;                                        // is the dragged in a spot where it would rejoin?
          if (sortable.mode === 'columns') engaged = y < 90;  // aligned near the column-top band
          else {
            const x0 = Math.min(...prov.map(g => g.left)), y0 = Math.min(...prov.map(g => g.top));
            const x1 = Math.max(...prov.map(g => g.left + g.w)), y1 = Math.max(...prov.map(g => g.top + g.h));
            engaged = dcx > x0 - 70 && dcx < x1 + 70 && dcy > y0 - 70 && dcy < y1 + 70;
          }
          if (engaged) {                     // open a gap at the nearest slot
            let k = 0, best = Infinity;
            prov.forEach((sl, i) => { const cx = sl.left + sl.w / 2, cy = sl.top + sl.h / 2;
              const d = sortable.mode === 'columns' ? Math.abs(cx - dcx) : Math.hypot(cx - dcx, cy - dcy);
              if (d < best) { best = d; k = i; } });
            placeGroup(this.slotGeom(insert(k), sortable.mode), k); sortable.engaged = true; sortable.k = k;
          } else {                           // pulled out: group compacts to fill the whole space
            placeGroup(this.slotGeom(sortable.group, sortable.mode), -1); sortable.engaged = false;
          }
          return;
        }
        if (moved) {   // FREE / cascade: snap to edges; the group stays put (void remains) until a snap rejoins
          const snap = this.snapDrag(win, x, y);
          lastSnapped = (snap.gx != null || snap.gy != null);
          this.showSnap(snap.gx, snap.gy); this.markSnapTargets(snap.targets);
          win.el.classList.remove('tiled');
          s.left = snap.x + 'px'; s.top = snap.y + 'px';
        } else { s.left = x + 'px'; s.top = y + 'px'; }
      };
      const up = () => {
        head.removeEventListener('pointermove', move);
        head.removeEventListener('pointerup', up);
        this.showSnap(null, null); this.markSnapTargets([]);
        if (sortable) {
          win.el.classList.remove('lifted');
          const N = sortable.group.length;
          if (!moved) {   // a click (no drag): expand a collapsed strip back into the pit; else no-op
            if (win.colCollapsed) { win.el.classList.add('reflow'); this.setColCollapsed(win, false); }
            this._clearReflow([win, ...sortable.group]);
            return;
          }
          if (sortable.engaged) {            // settle into the opened slot; the group keeps its room
            const items = sortable.group.slice(); items.splice(sortable.k, 0, win);   // dragged inserted at k
            const slots = this.slotGeom(items, sortable.mode), sl = slots[sortable.k];
            // dragging a window back into the band re-joins auto-arrange, so the icon
            // has to relight — it is the only indicator of this state now
            win.el.classList.add('reflow', 'tiled'); this.setArrange(win, true);
            Object.assign(s, { left: sl.left + 'px', top: sl.top + 'px', width: sl.w + 'px', height: sl.h + 'px' });
            placeGroup(slots, sortable.k);
            this.arranged = true;
            [win, ...sortable.group].forEach(w => this.saveGeo(w));
            this._clearReflow([win, ...sortable.group]);
          } else {                           // dropped out: stay free; the group holds its compacted layout
            win.el.classList.remove('tiled');
            placeGroup(this.slotGeom(sortable.group, sortable.mode), -1);
            this.arranged = N > 0;
            [win, ...sortable.group].forEach(w => this.saveGeo(w));
            this._clearReflow(sortable.group);
          }
          return;
        }
        // a click (no drag) on a collapsed column strip expands it — the strip IS the header
        if (!moved && win.colCollapsed) { this.setColCollapsed(win, false); return; }
        if (moved && lastSnapped && this.arranged && arrMode !== 'cascade') {   // FREE mode: snap rejoins the arrangement
          const affected = this.visibleWins();
          affected.forEach(w => w.el.classList.add('reflow'));
          s.left = (parseInt(s.left) - 1) + 'px'; s.top = (parseInt(s.top) - 1) + 'px';
          this.tile(null, { mode: arrMode });
          this._clearReflow(affected);
        } else {
          this.saveGeo(win);   // free drop: leave the window where it was dropped, void intact
        }
      };
      head.addEventListener('pointermove', move);
      head.addEventListener('pointerup', up);
    });
  },
  _clearReflow(wins) {   // drop the transition class after the settle animation so later drags are instant
    setTimeout(() => (wins || []).forEach(w => w.el && w.el.classList.remove('reflow')), 220);
  },
  // A tiled slot just came free (window popped out, closed or minimized): re-slot
  // the survivors so they fill the void, exactly as dragging one out does. No-op
  // in FREE drag mode (the void is the point) or cascade (there are no slots).
  // Returns true when the layout is still arranged afterwards.
  reflowGap(gone) {
    const st = Settings.load();
    const arrMode = st.arrangeMode || 'tiled';
    if (!shouldReflowGap(arrMode, st.dragMode)) return this.arranged;
    const group = this.visibleWins()
      .filter(w => w !== gone && w.el.classList.contains('tiled'))
      .sort(this.arrangeCmp(arrMode));
    if (group.length) {
      const slots = this.slotGeom(group, arrMode);   // pass windows so collapsed strips keep strip width
      group.forEach((o, j) => {
        o.el.classList.add('reflow');
        Object.assign(o.el.style, { left: slots[j].left + 'px', top: slots[j].top + 'px',
          width: slots[j].w + 'px', height: slots[j].h + 'px' });
        this.saveGeo(o);
      });
      this._clearReflow(group);
    }
    this.arranged = group.length > 0;
    return this.arranged;
  },
  resizify(win, handle, dirs) {
    handle.addEventListener('pointerdown', e => {
      e.stopPropagation(); e.preventDefault();
      this.arranged = false;   // manual resize breaks the arranged layout
      win.el.classList.remove('tiled');
      const s = win.el.style;
      const x0 = e.clientX, y0 = e.clientY;
      const L0 = parseInt(s.left), T0 = parseInt(s.top), W0 = parseInt(s.width), H0 = parseInt(s.height);
      handle.setPointerCapture(e.pointerId);
      const move = ev => {
        const dx = ev.clientX - x0, dy = ev.clientY - y0;
        let L = L0, T = T0, W = W0, H = H0;
        if (dirs.r) W = Math.max(380, W0 + dx);
        if (dirs.b) H = Math.max(220, H0 + dy);
        if (dirs.l) { W = Math.max(380, W0 - dx); L = L0 + (W0 - W); }
        if (dirs.t) { H = Math.max(220, H0 - dy); T = T0 + (H0 - H); if (T < 0) { H += T; T = 0; } }
        Object.assign(s, { left: L + 'px', top: T + 'px', width: W + 'px', height: H + 'px' });
      };
      const up = () => {
        handle.removeEventListener('pointermove', move);
        handle.removeEventListener('pointerup', up);
        this.saveGeo(win);
      };
      handle.addEventListener('pointermove', move);
      handle.addEventListener('pointerup', up);
    });
  },
  /* keep a spawning window from landing exactly on top of an open one (stagger so the
     underlying window's title bar stays visible instead of being fully hidden) */
  avoidOverlap(x, y) {
    const others = [...this.wins.values()].filter(o =>
      o.el && !o.min && o.el.style.display !== 'none');
    const collides = () => others.some(o =>
      Math.abs(parseInt(o.el.style.left) - x) < 40 && Math.abs(parseInt(o.el.style.top) - y) < 40);
    let guard = 0;
    while (collides() && guard++ < 12) {
      x += 42; y += 42;
      if (x > innerWidth - 160) x = 70;
      if (y > innerHeight - 200) y = 20;
    }
    return { x, y };
  },
  /* snap a dragged window's edges to the desktop bounds and to the nearest other-window
     edge (edge-to-edge). Returns the snapped position, guide-line coords, and the
     window(s) being snapped against so the caller can highlight them. */
  snapDrag(win, x, y) {
    const T = 18, x0 = x, y0 = y, DW = innerWidth, DH = innerHeight - 46;
    const W = parseInt(win.el.style.width), H = parseInt(win.el.style.height);
    const xs = [{ v: 0 }, { v: DW }], ys = [{ v: 0 }, { v: DH }];
    for (const o of this.wins.values()) {
      if (o === win || o.min || o.el.style.display === 'none') continue;
      const oL = parseInt(o.el.style.left), oT = parseInt(o.el.style.top);
      xs.push({ v: oL, w: o }, { v: oL + parseInt(o.el.style.width), w: o });
      ys.push({ v: oT, w: o }, { v: oT + parseInt(o.el.style.height), w: o });
    }
    let gx = null, gy = null, tx = null, ty = null, bx = T + 1, by = T + 1;
    for (const L of xs) {                                   // dragged left OR right edge -> line
      const dl = Math.abs(x0 - L.v);       if (dl < bx) { bx = dl; x = L.v; gx = L.v; tx = L.w || null; }
      const dr = Math.abs(x0 + W - L.v);   if (dr < bx) { bx = dr; x = L.v - W; gx = L.v; tx = L.w || null; }
    }
    for (const L of ys) {                                   // dragged top OR bottom edge -> line
      const dt = Math.abs(y0 - L.v);       if (dt < by) { by = dt; y = L.v; gy = L.v; ty = L.w || null; }
      const db = Math.abs(y0 + H - L.v);   if (db < by) { by = db; y = L.v - H; gy = L.v; ty = L.w || null; }
    }
    if (bx > T) { x = x0; gx = null; tx = null; }
    if (by > T) { y = y0; gy = null; ty = null; }
    return { x, y, gx, gy, targets: [tx, ty].filter((v, i, a) => v && a.indexOf(v) === i) };
  },
  showSnap(gx, gy) {
    let v = this._snapV, h = this._snapH;
    if (gx == null && gy == null) { v && (v.style.display = 'none'); h && (h.style.display = 'none'); return; }
    const desk = $('#desktop');
    if (!v) { v = this._snapV = el('div', 'snapline v'); desk.appendChild(v); }
    if (!h) { h = this._snapH = el('div', 'snapline h'); desk.appendChild(h); }
    if (gx == null) v.style.display = 'none'; else { v.style.display = 'block'; v.style.left = gx + 'px'; }
    if (gy == null) h.style.display = 'none'; else { h.style.display = 'block'; h.style.top = gy + 'px'; }
  },
  markSnapTargets(targets) {
    const set = new Set(targets || []);
    for (const o of this.wins.values()) o.el.classList.toggle('snap-target', set.has(o));
  },
  every(win, fn, ms) { const t = setInterval(fn, ms); win.timers.push(t); return t; },

  /* ---- v3: persist open windows across close/refresh ---- */
  persistLayout() {
    if (!this.ready) return;   // don't let boot/restore's transient window set clobber the saved layout
    const apps = [...this.wins.values()]
      .filter(w => !w.app.id.includes(':'))        // singleton apps only
      .map(w => ({ id: w.app.id, min: !!w.min, desktop: w.desktop || 1 }));
    const terms = [...this.wins.values()].filter(w => w.app.id.startsWith('term:'))
      .map(w => ({ id: w.app.id.slice(5), desktop: w.desktop || 1 }));
    try { localStorage.setItem('zen.layout', JSON.stringify({ apps, terms, activeDesktop: this.activeDesktop })); }
    catch (e) { /* quota */ }
  },
  async restoreLayout() {
    let layout = null;
    try { layout = JSON.parse(localStorage.getItem('zen.layout')); } catch (e) { /* corrupt */ }
    const exact = Settings.load().persistWindows !== false;   // honor saved positions verbatim
    // Live terminals are owned by the server (tmux sessions survive server restarts).
    // We ALWAYS reopen every live session, not just what the saved layout remembered —
    // a cleared/clobbered layout must never strand a running session again.
    const r = await apiSafe('/api/term/list', undefined, { silent: true });
    const live = (r?.terms || []).filter(t => t.status === 'live');
    const byId = new Map(live.map(t => [t.id, t]));
    const savedApps = (layout && layout.apps) || [];
    const savedTerms = (layout && layout.terms) || [];
    if (!savedApps.length && !savedTerms.length && !live.length) {
      this.open('dashboard'); this.open('feed'); this.ready = true; return;   // true first-run default
    }
    savedApps.forEach(a => {
      const app = APPS.find(x => x.id === a.id);
      if (app && app.drawer) return;   // drawers aren't persisted windows; don't auto-open on boot
      if (app && !Caps.appVisible(app)) return;   // Phase-3: hidden module → skip (drops from zen.layout on next persist)
      const w = this.open(a.id, { exact, restore: true, desktop: a.desktop || 1 });
      if (a.min && w) this.minimize(w);
    });
    const opened = new Set();
    savedTerms.forEach(entry => {   // re-attach remembered terminals in their saved spot
      const id = typeof entry === 'string' ? entry : entry.id;   // tolerate the old array-of-ids shape
      const desktop = typeof entry === 'string' ? 1 : (entry.desktop || 1);
      const t = byId.get(id);
      if (t && typeof openTermWindow === 'function') { openTermWindow(t, { exact, restore: true, desktop }); opened.add(id); }
    });
    // RECOVERY fallback: when the saved layout has NO terminal record at all (lost,
    // cleared, or first-run) but the server still has live tmux sessions, reopen them
    // so nothing is stranded — each in its own last-known spot (per-window geo lives
    // under zen.geo.term:<id>, independent of zen.layout, so positions survive a wipe).
    // When the layout DOES remember terminals we trust it verbatim above and do NOT
    // resurrect sessions whose windows the user deliberately closed.
    if (!savedTerms.length && typeof openTermWindow === 'function')
      live.forEach(t => {
        if (opened.has(t.id)) return;
        let desktop = 1;
        try { desktop = (JSON.parse(localStorage.getItem('zen.geo.term:' + t.id)) || {}).desktop || 1; } catch (e) { /* no saved geo */ }
        openTermWindow(t, { exact, restore: true, desktop });
      });
    this.activeDesktop = Math.min((layout && layout.activeDesktop) || 1, this.desktopCount());
    this.refreshSwitcher();
    this.applyDesktopVisibility();
    // Reflow ONLY previously-tiled windows into the current canvas (per desktop), so a tiled/columns
    // arrangement whose saved slots are now off-screen (narrower screen / more columns than fit) comes
    // back packed on-screen instead of popping to cascade. Windows the user placed by hand are FREE
    // (no tiled flag) and are deliberately left exactly where they were restored — a manual layout persists.
    const arrMode = Settings.load().arrangeMode || 'tiled';
    if (arrMode !== 'cascade') {
      const wasTiled = w => {
        try { return !!(JSON.parse(localStorage.getItem('zen.geo.' + (w.app.geoKey || w.app.id))) || {}).tiled; }
        catch (e) { return false; }
      };
      const keepDesk = this.activeDesktop;
      const desks = [...new Set([...this.wins.values()].filter(w => !w.min).map(w => w.desktop || 1))];
      for (const d of desks) {   // every desktop that actually holds windows (not just 1..desktopCount)
        const tiledOn = [...this.wins.values()].filter(w => !w.min && (w.desktop || 1) === d && wasTiled(w));
        if (tiledOn.length >= 2) {   // a real tiled arrangement to reflow; free windows are untouched
          this.activeDesktop = d; this.applyDesktopVisibility();
          this.tile((id, w) => wasTiled(w), { mode: arrMode });   // filter → only the tiled windows
        }
      }
      this.activeDesktop = keepDesk; this.applyDesktopVisibility();
    }
    this.ready = true;          // restore done — persistLayout may now save real state
    this.persistLayout();       // write back the reconciled layout (incl. re-adopted orphan terms)
  },

  /* ---- v2: tile & cascade ---- */
  // Space the floating dock occupies on each edge, so arranged/maximized windows
  // don't sit under it. The #desktop element is CSS-inset per dock position (see
  // index.html), so its content box IS the dock-free canvas — windows live inside
  // it 0-based, and a dock move shifts every window over automatically.
  desktopBox() {
    const d = document.getElementById('desktop');
    return { w: d ? d.clientWidth : innerWidth, h: d ? d.clientHeight : innerHeight - 46 };
  },
  // Pull any free (non-tiled) window back inside the canvas — e.g. after the dock moves and the
  // #desktop origin shifts, a window near the far edge would otherwise sit partly off-screen.
  clampFree() {
    const { w: W, h: H } = this.desktopBox();
    const moved = [];
    for (const win of this.wins.values()) {
      if (win.min || win.el.classList.contains('tiled')) continue;
      const st = win.el.style, ww = parseInt(st.width) || 200, wh = parseInt(st.height) || 120;
      const l0 = parseInt(st.left) || 0, t0 = parseInt(st.top) || 0;
      const l = Math.max(0, Math.min(l0, Math.max(0, W - ww)));
      const t = Math.max(0, Math.min(t0, Math.max(0, H - wh)));
      if (l !== l0 || t !== t0) { win.el.classList.add('reflow'); st.left = l + 'px'; st.top = t + 'px'; this.saveGeo(win); moved.push(win); }
    }
    if (moved.length) this._clearReflow(moved);
  },
  visibleWins(filterFn) {
    return [...this.wins.values()].filter(w => !w.min && (w.desktop || 1) === this.activeDesktop &&
      (!filterFn || filterFn(w.app.id, w)));
  },
  // Order windows by their current on-screen position: left-to-right for columns,
  // row-banded reading order for the grid. Used by tile() and the sortable drag so
  // re-arranging follows where things ARE, not the order they were created.
  arrangeCmp(mode) {
    return (a, b) => {
      const al = parseInt(a.el.style.left) || 0, at = parseInt(a.el.style.top) || 0;
      const bl = parseInt(b.el.style.left) || 0, bt = parseInt(b.el.style.top) || 0;
      return mode === 'columns' ? (al - bl || at - bt) : (Math.abs(at - bt) > 60 ? at - bt : al - bl);
    };
  },
  // Geometry for `count` windows in the dock-free canvas — mirrors tile()'s layout math.
  // The sortable drag uses this to lay out both the N-window (room-for-dragged) and the
  // N-1 (compacted, dragged pulled out) states live.
  // `spec` is a window count (all full-width) OR an ordered array of windows so collapsed column
  // strips get their 30px strip width and the rest share the remaining width (mirrors tile()).
  slotGeom(spec, mode) {
    const items = typeof spec === 'number' ? Array.from({ length: spec }, () => null) : spec;
    const count = items.length;
    const { w: W, h: H } = this.desktopBox();
    const out = [];
    if (count < 1) return out;
    if (mode === 'columns') {
      const stripW = 30;
      const nColl = items.filter(w => w && w.colCollapsed).length;
      const nExp = count - nColl;
      const expW = nExp > 0 ? Math.max(200, Math.floor((W - nColl * stripW) / nExp)) : stripW;
      let x = 0;
      for (let i = 0; i < count; i++) {
        const wpx = (items[i] && items[i].colCollapsed) ? stripW : expW;
        out.push({ left: x, top: 0, w: wpx, h: H });
        x += wpx;
      }
    } else {
      const cols = Math.ceil(Math.sqrt(count)), rows = Math.ceil(count / cols);
      const cw = Math.floor(W / cols), ch = Math.floor(H / rows);
      for (let i = 0; i < count; i++) { const c = i % cols, r = Math.floor(i / cols); out.push({ left: c * cw, top: r * ch, w: cw, h: ch }); }
    }
    return out;
  },
  tile(filterFn, opts) {
    opts = opts || {};
    const mode = opts.mode || Settings.load().arrangeMode || 'tiled';
    let wins = this.visibleWins(filterFn);
    if (!filterFn) {   // "arrange all" honors the per-view include-off setting
      // excluded windows stay OPEN and drop behind: tiled windows get fresh top z-indices below,
      // so an excluded reference window keeps floating where it is, behind the arranged grid.
      const includeOff = Settings.load().arrangeInclude?.[mode] === true;
      wins = filterForArrange(wins, includeOff);
    }
    if (!wins.length) return toast('no windows to arrange', 'err');
    wins.sort(this.arrangeCmp(mode));   // lay out by current position, not creation order
    this.arranged = true;   // desktop is now in an arranged layout; new windows squeeze in
    wins.forEach(w => { w._popped = null; if (w.collapsed) this.setCollapsed(w, false); }); // expand so they fit
    const { w: W, h: H } = this.desktopBox();   // dock-free canvas; positions are 0-based within it
    const gap = 0;
    const n = wins.length;
    if (mode === 'columns') {   // all windows as full-height vertical columns across the top
      const stripW = 30;
      const nColl = wins.filter(w => w.colCollapsed).length;
      const nExp = n - nColl;
      const expW = nExp > 0 ? Math.floor((W - gap * (n + 1) - nColl * stripW) / nExp) : stripW;
      let x = gap;
      wins.forEach(win => {
        win.maxed = null;
        win.el.classList.add('tiled');
        const wpx = win.colCollapsed ? stripW : Math.max(200, expW);
        Object.assign(win.el.style, {
          left: x + 'px', top: '0px', width: wpx + 'px', height: H + 'px', zIndex: ++this.z });
        x += wpx + gap;
        this.saveGeo(win);
      });
    } else {   // tiled grid
      const cols = Math.ceil(Math.sqrt(n));
      const rows = Math.ceil(n / cols);
      const cw = Math.floor((W - gap * (cols + 1)) / cols);
      const ch = Math.floor((H - gap * (rows + 1)) / rows);
      wins.forEach((win, idx) => {
        const c = idx % cols, r = Math.floor(idx / cols);
        win.maxed = null;
        win.el.classList.add('tiled');
        Object.assign(win.el.style, {
          left: (gap + c * (cw + gap)) + 'px',
          top: (gap + r * (ch + gap)) + 'px',
          width: cw + 'px', height: ch + 'px', zIndex: ++this.z });
        this.saveGeo(win);
      });
    }
    opts.label && toast('arranged ' + n + ' · ' + mode, 'ok');
  },
  cascade(filterFn) {
    let wins = this.visibleWins(filterFn);
    if (!filterFn) {
      const includeOff = Settings.load().arrangeInclude?.cascade === true;
      wins = filterForArrange(wins, includeOff);
    }
    if (!wins.length) return toast('no windows to cascade', 'err');
    this.arranged = false;   // cascade is a free layout, not a squeeze-in grid
    const { w: W, h: H } = this.desktopBox();
    wins.forEach((win, idx) => {
      win.maxed = null;
      win.el.classList.remove('tiled');   // free layout regains its normal min-size
      Object.assign(win.el.style, {
        left: (60 + idx * 34) + 'px', top: (16 + idx * 30) + 'px',
        width: Math.min(820, W - 140) + 'px',
        height: Math.min(560, H - 120) + 'px', zIndex: ++this.z });
      this.saveGeo(win);
    });
    toast('cascaded ' + wins.length, 'ok');
  },
  applyArrange(mode) {
    mode = mode || Settings.load().arrangeMode || 'tiled';
    if (!ARR_MODES.includes(mode)) mode = 'tiled';
    Settings.set('arrangeMode', mode);
    if (mode === 'cascade') this.cascade(null);
    else this.tile(null, { mode, label: 'all' });
    this.refreshArrangeBtn();
  },
  cycleArrange() {
    const cur = Settings.load().arrangeMode || 'tiled';
    const next = ARR_MODES[(ARR_MODES.indexOf(cur) + 1 + ARR_MODES.length) % ARR_MODES.length];
    this.applyArrange(next);
  },
  refreshArrangeBtn() {
    const b = document.getElementById('arrbtn'); if (!b) return;
    const m = Settings.load().arrangeMode || 'tiled';
    const cur = ARR_MODES.includes(m) ? m : 'tiled';
    const next = ARR_MODES[(ARR_MODES.indexOf(cur) + 1) % ARR_MODES.length];
    b.querySelector('.glyph').innerHTML = ARR_ICON[cur] || ARR_GLYPH[cur];
    b.querySelector('.lbl').textContent = ARR_LABEL[cur];
    const tip = b.querySelector('.arrtip');
    if (tip) tip.innerHTML = '<b>Click</b> — arrange as ' + ARR_LABEL[cur] + ' again (⌘⇧A)<br>'
      + '<b>Right-click</b> — switch to ' + ARR_LABEL[next] + ' layout';
    b.title = '';   // styled .arrtip replaces the native tooltip
  },

  /* ---- virtual desktops (workspaces) ---- */
  desktopCount() { return Math.max(1, Math.min(6, Settings.load().desktopCount || 1)); },
  desksWithWindows() {   // set of desktop numbers that hold at least one (non-minimized) window
    const s = new Set();
    for (const w of this.wins.values()) if (!w.min) s.add(w.desktop || 1);
    return s;
  },
  applyDesktopVisibility() {
    const a = this.activeDesktop;
    for (const w of this.wins.values())
      w.el.style.display = (w.min || (w.desktop || 1) !== a) ? 'none' : 'flex';
    Dock.update();
  },
  switchDesktop(n) {
    n = Math.max(1, Math.min(this.desktopCount(), n | 0));
    if (n === this.activeDesktop) { this.closeExpose(); return; }
    this.activeDesktop = n;
    this.arranged = false;                 // each desktop tracks its own arrange state loosely
    this.applyDesktopVisibility();
    this.refreshSwitcher();
    this.closeExpose();
    const top = this.visibleWins().sort((a, b) => (+b.el.style.zIndex || 0) - (+a.el.style.zIndex || 0))[0];
    // an EMPTY target desktop must still drop the focus ring from the window we left,
    // or it stays "focused" while invisible and every focus-conditional path misreads it
    if (top) this.focus(top);
    else { for (const w of this.wins.values()) w.el.classList.remove('focus'); this.focusedWin = null; }
    this.persistLayout();
  },
  stepDesktop(dir) {
    const c = this.desktopCount();
    this.switchDesktop(((this.activeDesktop - 1 + dir + c) % c) + 1);
  },
  setWinDesktop(win, n) {
    n = Math.max(1, Math.min(this.desktopCount(), n | 0));
    if (n === (win.desktop || 1)) return;
    win.desktop = n;
    this.refreshDeskControl(win);
    this.saveGeo(win);
    if (n !== this.activeDesktop) {        // it left the current desktop — hide + hand focus off
      win.el.style.display = 'none';
      const top = this.visibleWins().sort((a, b) => (+b.el.style.zIndex || 0) - (+a.el.style.zIndex || 0))[0];
      if (top) this.focus(top);
      toast(win.app.name + ' → desktop ' + n, 'ok');
    }
    this.refreshSwitcher();
    Dock.update();
    this.persistLayout();
  },
  sendFocusedToDesktop(n) {
    const w = this.focusedWin && !this.focusedWin.min && this.wins.has(this.focusedWin.app.id)
      ? this.focusedWin : this.visibleWins().slice(-1)[0];
    if (w) this.setWinDesktop(w, n);
  },
  setDesktopCount(n) {
    n = Math.max(1, Math.min(6, n | 0));
    Settings.set('desktopCount', n);
    for (const w of this.wins.values()) if ((w.desktop || 1) > n) w.desktop = n;   // fold orphaned windows in
    if (this.activeDesktop > n) this.activeDesktop = n;
    this.wins.forEach(w => this.refreshDeskControl(w));
    this.applyDesktopVisibility();
    this.refreshSwitcher();
    this.persistLayout();
  },
  // per-window title-bar 1·2·N control
  refreshDeskControl(win) {
    const ctl = win._deskCtl; if (!ctl) return;
    const c = this.desktopCount();
    if (c < 2) { ctl.replaceChildren(); ctl.style.display = 'none'; return; }
    ctl.style.display = 'inline-flex';
    ctl.replaceChildren();
    for (let i = 1; i <= c; i++) {
      const b = el('button', 'wd' + ((win.desktop || 1) === i ? ' on' : ''), String(i));
      b.title = 'Move to desktop ' + i;
      b.onclick = e => { e.stopPropagation(); this.setWinDesktop(win, i); };
      ctl.appendChild(b);
    }
  },
  // top-bar switcher
  refreshSwitcher() {
    const host = document.getElementById('wsc-desks'); if (!host) return;
    const c = this.desktopCount();
    if (c < 2) { host.replaceChildren(); host.style.display = 'none'; return; }
    host.style.display = 'inline-flex';
    host.replaceChildren();
    const has = this.desksWithWindows();
    for (let i = 1; i <= c; i++) {
      const b = el('button', 'dsw' + (i === this.activeDesktop ? ' on' : '') + (has.has(i) ? ' filled' : ''), String(i));
      b.title = 'Desktop ' + i + ' (⌃' + i + ')';
      b.onclick = () => this.switchDesktop(i);
      host.appendChild(b);
    }
    if (c < 6) { const add = el('button', 'dsw add', '＋'); add.title = 'Add a desktop';
      add.onclick = () => this.setDesktopCount(c + 1); host.appendChild(add); }
    const exp = el('button', 'dsw exp', '⊞'); exp.title = 'Show all desktops (⌃0)';
    exp.onclick = () => this.expose(); host.appendChild(exp);
  },
  // Exposé — zoom out to all desktops as proportional mini-maps
  expose() {
    if (this.desktopCount() < 2) return;
    if (document.getElementById('expose')) { this.closeExpose(); return; }
    const ov = el('div'); ov.id = 'expose';
    const { w: W, h: H } = this.desktopBox();
    const grid = el('div', 'exp-grid');
    for (let d = 1; d <= this.desktopCount(); d++) {
      const panel = el('div', 'exp-panel' + (d === this.activeDesktop ? ' on' : ''));
      const stage = el('div', 'exp-stage');
      for (const w of this.wins.values()) {
        if (w.min || (w.desktop || 1) !== d) continue;
        const s = w.el.style;
        const mini = el('div', 'exp-win', esc(w.app.name));
        mini.style.left = (parseInt(s.left) / W * 100) + '%';
        mini.style.top = (parseInt(s.top) / H * 100) + '%';
        mini.style.width = (parseInt(s.width) / W * 100) + '%';
        mini.style.height = (parseInt(s.height) / H * 100) + '%';
        if (w.app.accent) mini.style.setProperty('--acc', w.app.accent);
        mini.onclick = e => { e.stopPropagation(); this.focus(w); this.switchDesktop(d); };
        stage.appendChild(mini);
      }
      const label = el('div', 'exp-label', 'DESKTOP ' + d + (d === this.activeDesktop ? ' · active' : ''));
      panel.append(stage, label);
      panel.onclick = () => this.switchDesktop(d);   // switchDesktop closes the overlay
      grid.appendChild(panel);
    }
    ov.appendChild(grid);
    ov.onclick = e => { if (e.target === ov) this.closeExpose(); };
    document.body.appendChild(ov);
    requestAnimationFrame(() => ov.classList.add('show'));
  },
  closeExpose() {
    const ov = document.getElementById('expose');
    if (ov) { ov.classList.remove('show'); setTimeout(() => ov.remove(), 160); }
  }
};

/* ================= dock ================= */
const Dock = {
  render() {
    const d = $('#dock');
    d.innerHTML = '';
    // command-palette launcher at the dock's leading end (top when vertical, left when horizontal)
    const cmd = el('button', 'dock-item cmdk');
    cmd.style.setProperty('--acc', 'var(--acc)');
    cmd.innerHTML = I.command + '<span class="tip">Command palette&nbsp;&nbsp;⌘K</span>';
    cmd.onclick = () => Palette.open();
    d.appendChild(cmd);
    d.appendChild(el('div', 'dock-sep'));
    APPS.forEach(app => {
      if (!Caps.appVisible(app)) return;   // Phase-3: skip apps failing the composition rule (§5.2)
      if (app.sep) d.appendChild(el('div', 'dock-sep'));
      const b = el('button', 'dock-item');
      b.dataset.app = app.id;
      if (app.accent) b.style.setProperty('--acc', app.accent);
      b.innerHTML = app.icon + `<span class="tip">${esc(app.name)}</span>`;
      b.onclick = () => {
        if (app.drawer) { Drawer.toggle(app); return; }
        const w = WM.wins.get(app.id);
        // click-to-minimize applies ONLY to a window that is actually in front of you on
        // THIS desktop. A window living on another desktop keeps its stale .focus class
        // (switchDesktop only reassigns focus when the target desktop has a window), so
        // the old check minimized it instead of pulling it over — the icon looked like a
        // no-op and a running window "vanished". Elsewhere → always open (= bring here).
        const here = w && (w.desktop || 1) === WM.activeDesktop;
        if (w && !w.min && here && w.el.classList.contains('focus')) WM.minimize(w);
        else WM.open(app.id);
      };
      d.appendChild(b);
    });
    // workspace cluster at the dock's end: desktop pips + mode-cycle button (Option A)
    d.appendChild(el('div', 'dock-sep'));
    const wsc = el('div', 'wsc');
    const desks = el('div', ''); desks.id = 'wsc-desks';
    wsc.appendChild(desks);
    const arr = el('button', 'arrbtn'); arr.id = 'arrbtn';
    const g = el('span', 'glyph', ''); const l = el('span', 'lbl', '');
    const tip = el('span', 'arrtip', '');
    arr.appendChild(g); arr.appendChild(l); arr.appendChild(tip);
    arr.onclick = () => WM.applyArrange();                                   // re-arrange in the current mode
    arr.oncontextmenu = e => { e.preventDefault(); WM.cycleArrange(); };      // change layout
    wsc.appendChild(arr);
    d.appendChild(wsc);
    WM.refreshSwitcher();
    WM.refreshArrangeBtn();
    this.fit();
  },
  // Adaptive sizing: comfortable 47px on a roomy screen, shrinking icons + gap
  // toward a 30px floor (still distinct, never overlapping) until the whole
  // dock fits the viewport along its run. Runs on render, resize, dock move.
  fit() {
    const d = $('#dock'); if (!d) return;
    const vert = document.body.dataset.dock === 'left' || document.body.dataset.dock === 'right';
    const avail = (vert ? innerHeight : innerWidth) - 28;   // 14px breathing room each end
    const CEIL = 47, FLOOR = 30;
    const apply = v => {
      d.style.setProperty('--dsize', v + 'px');
      d.style.setProperty('--dgap', Math.max(3, Math.round(v * 0.13)) + 'px');
    };
    const span = () => vert ? d.offsetHeight : d.offsetWidth;
    let s = CEIL; apply(CEIL);
    while (s > FLOOR && span() > avail) { s -= 1; apply(s); }
  },
  update() {
    const termOpen = [...WM.wins.keys()].some(k => k.startsWith('term:'));
    document.querySelectorAll('.dock-item').forEach(b => {
      if (!b.dataset.app) return;
      const w = WM.wins.get(b.dataset.app);
      b.classList.toggle('open', (!!w && !w.min) || (b.dataset.app === 'terminal' && termOpen));
    });
  }
};

/* ============ left slide-out drawer (Settings) — closes on outside click / Esc ============ */
const Drawer = {
  el: null, scrim: null, app: null, _icon: null, _title: null, _body: null,
  ensure() {
    if (this.el) return;
    this.scrim = el('div'); this.scrim.id = 'scrim';
    this.el = el('div'); this.el.id = 'drawer';
    const head = el('div', 'drawer-head');
    this._icon = el('span', 'wicon');
    this._title = el('span', 'dtitle');
    const close = el('button', 'dclose', '✕'); close.title = 'Close (Esc)';
    head.append(this._icon, this._title, close);
    this._body = el('div', 'drawer-body');
    this.el.append(head, this._body);
    document.body.append(this.scrim, this.el);
    this.scrim.onclick = () => this.close();          // click anywhere outside the pane closes it
    close.onclick = () => this.close();
  },
  isOpen() { return this.el && this.el.classList.contains('show'); },
  toggle(app) { (this.isOpen() && this.app === app) ? this.close() : this.open(app); },
  open(app) {
    this.ensure();
    this.app = app;
    this._title.textContent = app.name;
    const ic = el('span', 'wicon', app.icon);         // fresh node; app.icon is trusted static SVG
    this._icon.replaceWith(ic); this._icon = ic;
    if (app.accent) this.el.style.setProperty('--acc', app.accent);
    // grow out from the settings dock button: origin = button center in the drawer's local coords.
    // drawer is fixed at left:0/top:46 (CSS), so use those constants — its live rect is transform-scaled here.
    const dbtn = document.querySelector('.dock-item[data-app="settings"]');
    if (dbtn) {
      const r = dbtn.getBoundingClientRect();
      this.el.style.transformOrigin = `${r.left + r.width / 2}px ${(r.top + r.height / 2) - 46}px`;
    }
    this._body.replaceChildren();
    try { app.render(this._body, { drawer: true }); }
    catch (e) { this._body.textContent = 'render error: ' + e.message; }
    void this.el.offsetWidth;   // commit the start state so the grow-in animates (no rAF dependency)
    this.scrim.classList.add('show'); this.el.classList.add('show');
    this._mark(true);
  },
  close() {
    if (!this.isOpen()) return;
    this.scrim.classList.remove('show'); this.el.classList.remove('show');
    this._mark(false); this.app = null;
  },
  _mark(on) {
    const btn = document.querySelector('.dock-item[data-app="settings"]');
    if (btn) btn.classList.toggle('open', on);
  }
};
document.addEventListener('keydown', e => { if (e.key === 'Escape' && Drawer.isOpen()) Drawer.close(); });

/* ================= state (shared caches) ================= */
const State = { projects: [], sessions: [], overview: {}, linkFails: 0 };

async function refreshState() {
  const ov = await apiSafe('/api/overview', undefined, { silent: true });
  if (ov) { State.overview = ov; State.linkFails = 0; }
  else State.linkFails++;
  const pj = await apiSafe('/api/projects', undefined, { silent: true });
  if (pj) State.projects = pj.projects;
  renderStats();
  renderLink();
}
function renderLink() {
  const led = document.querySelector('#link .led');
  if (!led) return;
  const down = State.linkFails > 0;
  led.classList.toggle('on', !down);
  led.classList.toggle('down', down);
  $('#link').title = down ? 'backend link DOWN (' + State.linkFails + ' failed polls)' : 'backend link OK';
}
function renderStats() {
  const o = State.overview;
  $('#stats').innerHTML = `
    <span class="stat"><b>${o.live_sessions ?? '–'}</b> LIVE</span>
    <span class="stat"><b>${o.jobs_running ?? 0}</b> JOBS</span>
    <span class="stat"><b>${o.terms_live ?? 0}</b> PTY</span>
    <span class="stat"><b>${fmtNum(o.sessions)}</b> SESSIONS</span>
    <span class="stat"><b>${o.memories ?? '–'}</b> MEM
      <span class="led ${o.nm_available ? 'on' : 'warn'}" title="NexusMind ${o.nm_available ? 'online' : 'offline'}"></span></span>`;
}

/* ================= event bus (app-to-app) ================= */
const Bus = {
  handlers: {},
  on(evt, fn, win) {
    (this.handlers[evt] = this.handlers[evt] || []).push({ fn, win });
  },
  emit(evt, data) {
    (this.handlers[evt] || []).forEach(entry => {
      if (entry.win && !WM.wins.has(entry.win.app.id)) return; // window closed
      try { entry.fn(data); } catch (e) { /* stale handler */ }
    });
  }
};

/* ================= wake / resume recovery =================
   Laptop sleep freezes every timer and kills all TCP sockets. On wake there's a
   window where fetch/WS fail while WiFi re-associates and the (also-suspended)
   server catches up. Nothing else proactively notices the resume, so each
   subsystem would otherwise grind through its own slow backoff (terminals land
   in the 5s "SERVER OFFLINE" overlay; the Sessions pane fails its load and bails).
   Wake fires ONE recovery pulse: re-poll state, reconnect every terminal, and let
   apps reload. The normal polling intervals remain as backstops if it fires early. */
const Wake = {
  last: Date.now(),
  pulse() {
    const now = Date.now();
    if (now - this.last < 2000) return;   // collapse near-simultaneous triggers (heartbeat + online + visibility)
    this.last = now;
    refreshState();
    Object.values(window.ZTERMS || {}).forEach(z => { try { z.reconnect && z.reconnect(); } catch (e) {} });
    Bus.emit('wake');
  }
};
// A frozen-then-resumed timer is the one signal that survives every sleep cause
// (lid close, network drop, tab throttle). online/visibility are fast-path extras.
let _wakeBeat = Date.now();
setInterval(() => {
  const now = Date.now();
  if (now - _wakeBeat > 6000) Wake.pulse();   // expected ~2s between beats → a big gap means we were suspended
  _wakeBeat = now;
}, 2000);
addEventListener('online', () => Wake.pulse());
document.addEventListener('visibilitychange', () => { if (!document.hidden) Wake.pulse(); });

/* ================= UI scale ================= */
function applyScale() {
  const pct = parseInt(localStorage.getItem('zen.scale') || '100');
  document.documentElement.style.fontSize = pct + '%';
  document.body.style.zoom = (pct / 100).toString();
}
function setScale(pct) {
  localStorage.setItem('zen.scale', String(pct));
  applyScale();
  toast('UI scale ' + pct + '%', 'ok');
}

/* ================= terminals (real PTYs over WebSocket) ================= */
window.ZTERMS = {};   // live xterm instances, keyed by pty id (also used for verification)

/* ================= voice input (Whisper + browser fallback) ================= */
// Streaming voice-to-text. onText(fullTranscript) is called repeatedly with the
// ENTIRE running transcript (replace-in-place, terminalx-style) so a composer can
// show words landing live. Two backends stream: browser SpeechRecognition with
// interimResults, and local Whisper with VAD pause-detection segmenting.
const Voice = {
  whisperOk: null, active: null,
  async checkHealth() {
    if (this.whisperOk !== null) return this.whisperOk;
    const r = await apiSafe('/api/whisper/health', undefined, { silent: true });
    this.whisperOk = !!(r && r.status === 'ok');
    return this.whisperOk;
  },
  async toggle(onText, btn) {
    if (this.active) { this.active.stop(); return; }       // single-record lock
    const mode = (typeof Settings !== 'undefined' && Settings.load().sttMode) || 'auto';
    if (mode === 'browser') return this.startBrowser(onText, btn);   // word-by-word live
    if (mode === 'whisper') {
      if (await this.checkHealth()) return this.startWhisper(onText, btn);
      toast('Whisper unavailable — falling back to browser speech', 'err');
      return this.startBrowser(onText, btn);
    }
    // auto: prefer local Whisper when healthy, else browser
    if (await this.checkHealth()) this.startWhisper(onText, btn);
    else this.startBrowser(onText, btn);
  },
  startBrowser(onText, btn) {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    // Browser SpeechRecognition needs a secure context (https or localhost). Served
    // over a plain-IP LAN (http://192.168.x.x) it's blocked — name the real fix
    // instead of the misleading "no browser API" message.
    if (!window.isSecureContext)
      return toast("Browser speech needs HTTPS or localhost — it's blocked over a plain-IP LAN. Open the localhost tab, or install Whisper for mic here.", 'err');
    if (!SR) return toast('no whisper + no browser speech API — try Chrome or install Whisper', 'err');
    const rec = new SR(); rec.continuous = true; rec.interimResults = true; rec.lang = 'en-US';
    let buffer = '';        // final text carried across the browser's session restarts
    let sessionFinal = '';  // final text within the current recognition session
    rec.onresult = e => {
      let interim = '', accumulated = '';
      for (let i = 0; i < e.results.length; i++) {
        if (e.results[i].isFinal) accumulated += e.results[i][0].transcript;
        else interim += e.results[i][0].transcript;
      }
      sessionFinal = accumulated;
      const sep = (buffer && !buffer.endsWith(' ')) ? ' ' : '';
      onText((buffer + sep + accumulated + interim).trimStart());
    };
    rec.onerror = () => {};
    rec.onend = () => {
      if (!this.active) return;                 // stopped by user
      if (sessionFinal.trim()) {                // fold this session into the carry buffer
        const sep = (buffer && !buffer.endsWith(' ')) ? ' ' : '';
        buffer = (buffer + sep + sessionFinal.trim());
      }
      sessionFinal = '';
      try { rec.start(); } catch (e) { /* browser will retry via the next onend */ }
    };
    try { rec.start(); } catch (e) { return toast('mic busy', 'err'); }
    btn.classList.add('rec');
    this.active = { stop: () => { this.active = null; rec.onend = null; try { rec.stop(); } catch (e) {} btn.classList.remove('rec'); } };
    toast('listening (browser speech) — text streams live', 'ok');
  },
  // 16kHz mono Float32 PCM → 16-bit WAV Blob (the Whisper endpoint takes .wav as-is).
  pcmToWav(pcm, rate) {
    const buf = new ArrayBuffer(44 + pcm.length * 2), v = new DataView(buf);
    const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    w(0, 'RIFF'); v.setUint32(4, 36 + pcm.length * 2, true); w(8, 'WAVE');
    w(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true);
    v.setUint16(22, 1, true); v.setUint32(24, rate, true);
    v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
    w(36, 'data'); v.setUint32(40, pcm.length * 2, true);
    for (let i = 0; i < pcm.length; i++) {
      const s = Math.max(-1, Math.min(1, pcm[i]));
      v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return new Blob([buf], { type: 'audio/wav' });
  },
  async startWhisper(onText, btn) {
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
    catch (e) { return toast('microphone permission denied', 'err'); }
    const RATE = 16000;
    const audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: RATE });
    const source = audioCtx.createMediaStreamSource(stream);
    const processor = audioCtx.createScriptProcessor(4096, 1, 1);  // deprecated but universal — raw PCM
    const SILENCE_THRESHOLD = 0.005, SILENCE_DURATION = 0.8, MIN_SPEECH = 0.3;
    const MAX_SEGMENT_SAMPLES = RATE * 10;       // force a flush after 10s of continuous speech
    let speechBuffer = [], isSpeaking = false, silenceStart = 0;
    let accumulated = '';                        // committed transcript across segments
    let gen = 0, stopped = false;                // gen invalidates in-flight requests after stop

    const transcribe = async pcm => {
      if (pcm.length < RATE * 0.3) return;       // skip < 0.3s
      const myGen = gen;
      btn.classList.add('busy');
      try {
        const fd = new FormData();
        fd.append('audio', this.pcmToWav(pcm, RATE), 'segment.wav');
        const r = await fetch('/api/whisper', { method: 'POST', body: fd }).then(x => x.json());
        if (myGen !== gen) return;               // a newer stop/segment superseded this one
        const seg = (r.text || '').trim();
        if (seg) { accumulated = accumulated ? accumulated + ' ' + seg : seg; onText(accumulated); }
        else if (r.error) toast('whisper: ' + r.error, 'err');
      } catch (e) { /* one segment failed; keep streaming */ }
      finally { if (!stopped) btn.classList.remove('busy'); }
    };
    const flush = () => {
      const total = speechBuffer.reduce((a, b) => a + b.length, 0);
      if (total >= RATE * MIN_SPEECH) {
        const combined = new Float32Array(total); let o = 0;
        for (const b of speechBuffer) { combined.set(b, o); o += b.length; }
        transcribe(combined);
      }
      speechBuffer = [];
    };
    processor.onaudioprocess = e => {
      if (stopped) return;
      const chunk = new Float32Array(e.inputBuffer.getChannelData(0));
      let sum = 0; for (let i = 0; i < chunk.length; i++) sum += chunk[i] * chunk[i];
      const rms = Math.sqrt(sum / chunk.length);
      if (rms > SILENCE_THRESHOLD) {             // speech
        isSpeaking = true; silenceStart = 0; speechBuffer.push(chunk);
        if (speechBuffer.reduce((a, b) => a + b.length, 0) >= MAX_SEGMENT_SAMPLES) flush();
      } else if (isSpeaking) {                   // trailing silence after speech
        speechBuffer.push(chunk);
        if (silenceStart === 0) silenceStart = audioCtx.currentTime;
        else if (audioCtx.currentTime - silenceStart > SILENCE_DURATION) { flush(); isSpeaking = false; silenceStart = 0; }
      }
    };
    source.connect(processor);
    processor.connect(audioCtx.destination);     // required for onaudioprocess to fire
    btn.classList.add('rec');
    toast('listening (Whisper) — text streams live', 'ok');
    this.active = { stop: () => {
      this.active = null; stopped = true;
      flush();                                   // transcribe any trailing speech (kept: still current gen)
      gen++;
      try { source.disconnect(); processor.disconnect(); } catch (e) {}
      stream.getTracks().forEach(t => t.stop());
      audioCtx.close().catch(() => {});
      btn.classList.remove('rec', 'busy');
    } };
  },
};

// ------------------------------------------------------------- prompt history
// "wait — what did I actually ask it?" A session's own prompts, without scrolling
// the transcript back. Two entry points, one panel: the PROMPTS button on a live
// terminal's titlebar, and the Sessions detail view.

// Copy that works on localhost (Clipboard API) AND over plain-IP LAN, where the API
// is undefined — there we select the text instead so ⌘C does the job.
function copyPrompt(txt, node) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(txt).then(() => true, () => false);
  }
  if (node) {
    const r = document.createRange();
    r.selectNodeContents(node);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(r);
  }
  return Promise.resolve(false);
}

// Click-to-arm: the first click turns the button into a confirm state for a few
// seconds, the second fires. Two deliberate clicks without a modal — the right
// weight for a destructive ROW action you may repeat several times in a row.
function armButton(btn, armedLabel, onFire, ms) {
  const label = btn.textContent;
  const color = btn.style.color, border = btn.style.borderColor;
  let armed = false, timer = null;
  const disarm = () => {
    armed = false; clearTimeout(timer);
    btn.textContent = label; btn.style.color = color; btn.style.borderColor = border;
  };
  btn.onclick = e => {
    e.stopPropagation();
    if (armed) { disarm(); onFire(); return; }
    armed = true;
    btn.textContent = armedLabel;
    btn.style.color = '#ff5d6c';
    btn.style.borderColor = '#ff5d6c';
    timer = setTimeout(disarm, ms || 3000);
  };
  btn.addEventListener('pointerleave', () => { if (armed) timer = setTimeout(disarm, 1200); });
  return disarm;
}

// Fast hover label for header controls. A real positioned element on <body> rather
// than a ::after, because .wtitle clips with overflow:hidden and would eat a pseudo-
// element — and the native title attribute's ~1s delay isn't configurable at all.
// `text` may be a function, so a label can track a title that changes after binding.
const TIP_DELAY = 250;
let _tipEl = null, _tipTimer = null;
function hideTip() {
  clearTimeout(_tipTimer);
  if (_tipEl) { _tipEl.remove(); _tipEl = null; }
}
function attachTip(node, text) {
  if (!node) return;
  node.removeAttribute('title');          // kill the slow native one
  node._tipText = text;
  if (node._tipBound) return;             // idempotent: re-calling just retargets
  node._tipBound = true;
  node.addEventListener('pointerenter', () => {
    clearTimeout(_tipTimer);
    _tipTimer = setTimeout(() => {
      const t = typeof node._tipText === 'function' ? node._tipText() : node._tipText;
      if (!t) return;
      hideTip();
      _tipEl = el('div', 'fasttip', esc(t));
      document.body.appendChild(_tipEl);
      const r = node.getBoundingClientRect(), w = _tipEl.offsetWidth;
      _tipEl.style.left = Math.max(6, Math.min(r.left + r.width / 2 - w / 2, innerWidth - w - 6)) + 'px';
      _tipEl.style.top = (r.bottom + 7) + 'px';
      requestAnimationFrame(() => _tipEl && _tipEl.classList.add('show'));
    }, TIP_DELAY);
  });
  node.addEventListener('pointerleave', hideTip);
  node.addEventListener('pointerdown', hideTip);
}

// seconds -> "45s" / "12m" / "3h" / "14d" (process ages, not wall-clock timestamps)
function fmtDur(s) {
  if (s == null) return '?';
  if (s < 60) return Math.round(s) + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm';
  if (s < 86400) return Math.floor(s / 3600) + 'h';
  return Math.floor(s / 86400) + 'd';
}

let _promptsPop = null;
function closePromptsPanel() {
  if (!_promptsPop) return;
  _promptsPop.remove();
  _promptsPop = null;
  document.removeEventListener('keydown', _promptsKey, true);
  document.removeEventListener('pointerdown', _promptsAway, true);
}
function _promptsKey(e) {
  if (e.key === 'Escape' && _promptsPop) { e.stopPropagation(); closePromptsPanel(); }
}
function _promptsAway(e) {
  if (_promptsPop && !_promptsPop.contains(e.target)) closePromptsPanel();
}

// src: {term:'<id>'} for whatever session a live terminal is driving, or
// {path:'<transcript>'} for any session. anchor = element to open beneath.
/* ---- context popover: the session's window occupancy, anchored to its own
   header button. Same interaction contract as the prompts pane (toggle, Esc,
   click-away) and deliberately the same shape of data as the Context tab, just
   condensed to what fits over a terminal. ---- */
let _ctxPop = null;
function closeCtxPanel() {
  if (!_ctxPop) return;
  _ctxPop.remove(); _ctxPop = null;
  document.removeEventListener('keydown', _ctxKey, true);
  document.removeEventListener('pointerdown', _ctxAway, true);
}
function _ctxKey(e) {
  if (e.key === 'Escape' && _ctxPop) { e.stopPropagation(); closeCtxPanel(); }
}
function _ctxAway(e) {
  if (_ctxPop && !_ctxPop.contains(e.target)) closeCtxPanel();
}
const _ctxTone = p => p >= 90 ? '#ff5d6c' : p >= 70 ? '#ffb45e' : '#4ef0a6';

async function openContextPanel(termId, anchorEl, label) {
  const tag = 'term:' + termId;
  if (_ctxPop && _ctxPop.dataset.src === tag) return closeCtxPanel();   // toggle
  closeCtxPanel(); closePromptsPanel();
  const pop = el('div', 'ctx-pop');
  pop.dataset.src = tag;
  pop.style.cssText = 'position:fixed;z-index:9000;width:430px;max-width:calc(100vw - 24px);'
    + 'max-height:min(70vh,540px);display:flex;flex-direction:column;overflow:hidden;'
    + 'background:var(--panel,#0a1118);border:1px solid var(--acc,#3fe3ff);border-radius:10px;'
    + 'box-shadow:0 20px 60px rgba(0,0,0,.6);font-size:12px';
  const head = el('div', '', `<span style="font-family:var(--disp,inherit);letter-spacing:.08em;
      color:var(--acc,#3fe3ff)">CONTEXT</span>
    <span class="csub" style="color:var(--faint,#5c7a8a);margin-left:6px"></span>
    <span style="flex:1"></span>
    <button class="btn ghost sm" data-a="refresh" title="Re-read the transcript">⟳</button>
    <button class="btn ghost sm" data-a="close" title="Close (Esc)">✕</button>`);
  head.style.cssText = 'display:flex;align-items:center;gap:4px;padding:8px 10px;flex:none;'
    + 'border-bottom:1px solid rgba(127,127,127,.22)';
  const body = el('div');
  body.style.cssText = 'overflow-y:auto;padding:10px;flex:1 1 auto';
  body.innerHTML = '<div class="empty">reading transcript…</div>';
  pop.append(head, body);
  document.body.appendChild(pop);
  _ctxPop = pop;
  const r = anchorEl && anchorEl.getBoundingClientRect();
  pop.style.left = Math.max(12, Math.min(r ? r.left - 200 : (innerWidth - 430) / 2,
                                         innerWidth - 442)) + 'px';
  pop.style.top = Math.max(12, Math.min(r ? r.bottom + 6 : 90, innerHeight - 200)) + 'px';
  head.querySelector('[data-a=close]').onclick = closeCtxPanel;

  const fmt = n => (n || 0).toLocaleString();
  const load = async () => {
    const c = await apiSafe('/api/context?term=' + encodeURIComponent(termId),
      undefined, { silent: true });
    if (!_ctxPop) return;                        // closed while fetching
    if (!c || !c.available) {
      body.innerHTML = `<div class="empty">${esc((c && c.reason)
        || 'no transcript for this terminal yet')}</div>`;
      return;
    }
    head.querySelector('.csub').textContent = (c.model || '').replace('claude-', '');
    const segs = [...Object.values(c.measured || {}), ...Object.values(c.derived || {})]
      .filter(x => x.tokens > 0);
    const perTurn = c.turns > 1 ? Math.round((c.current - c.baseline) / (c.turns - 1)) : 0;
    const left = perTurn > 0 ? Math.floor(c.free / perTurn) : null;
    const fill = m => m ? 'background:var(--cyan-soft,#8ceeff);opacity:.75'
      : 'background:repeating-linear-gradient(135deg,rgba(255,180,94,.5) 0 5px,rgba(255,180,94,.14) 5px 10px)';
    body.innerHTML = `
      <div style="display:flex;align-items:flex-end;gap:14px;margin-bottom:10px">
        <div style="font:700 26px var(--mono);font-variant-numeric:tabular-nums;
          color:${_ctxTone(c.pct)};line-height:1">${c.pct}%</div>
        <div style="color:var(--faint,#5c7a8a);font-size:11px;padding-bottom:3px">
          ${fmt(c.current)} / ${fmt(c.window)}<br>${fmt(c.free)} free${
            left !== null ? ` · ~${fmt(left)} turns` : ''}</div>
      </div>
      <div style="display:flex;height:22px;border:1px solid var(--line,rgba(127,127,127,.25));
        border-radius:6px;overflow:hidden;margin-bottom:10px">
        ${segs.map(x => `<div title="${esc(x.label)} — ${fmt(x.tokens)}"
          style="width:${(x.tokens / c.window * 100).toFixed(3)}%;${fill(x.measured)}"></div>`).join('')}
      </div>
      ${segs.map(x => `<div style="display:grid;grid-template-columns:10px minmax(0,max-content) 80px 52px;
        gap:9px;align-items:baseline;padding:4px 0">
        <span style="width:9px;height:9px;border-radius:2px;${fill(x.measured)}"></span>
        <span style="color:var(--text)">${esc(x.label)}</span>
        <span style="font:600 12px var(--mono);font-variant-numeric:tabular-nums;
          text-align:right">${fmt(x.tokens)}</span>
        <span style="font-family:var(--mono);color:var(--cyan-soft,#8ceeff);
          text-align:right">${(x.tokens / c.current * 100).toFixed(1)}%</span></div>`).join('')}
      <div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:10px;
        border-top:1px solid rgba(127,127,127,.18);padding-top:9px;color:var(--faint,#5c7a8a);font-size:11px">
        <span>${c.turns} turns</span><span>+${fmt(perTurn)}/turn</span>
        <span>peak ${fmt(c.peak)}</span></div>`;
  };
  head.querySelector('[data-a=refresh]').onclick = () => {
    body.innerHTML = '<div class="empty">re-reading…</div>'; load();
  };
  load();
  document.addEventListener('keydown', _ctxKey, true);
  document.addEventListener('pointerdown', _ctxAway, true);
}

async function openPromptsPanel(src, anchor, label) {
  const tag = JSON.stringify(src);
  if (_promptsPop && _promptsPop.dataset.src === tag) return closePromptsPanel();  // toggle
  closePromptsPanel();
  const pop = el('div', 'prompts-pop');
  pop.dataset.src = tag;
  pop.style.cssText = 'position:fixed;z-index:9000;width:460px;max-width:calc(100vw - 24px);'
    + 'max-height:min(70vh,560px);display:flex;flex-direction:column;overflow:hidden;'
    + 'background:var(--panel,#0a1118);border:1px solid var(--acc,#3fe3ff);border-radius:10px;'
    + 'box-shadow:0 20px 60px rgba(0,0,0,.6);font-size:12px';
  const head = el('div', '', `<span style="font-family:var(--disp,inherit);letter-spacing:.08em;
      color:var(--acc,#3fe3ff)">PROMPTS</span>
    <span class="pcount" style="color:var(--faint,#5c7a8a);margin-left:6px"></span>
    <span style="flex:1"></span>
    <button class="btn ghost sm" data-a="refresh" title="Re-read the transcript">⟳</button>
    <button class="btn ghost sm" data-a="close" title="Close (Esc)">✕</button>`);
  head.style.cssText = 'display:flex;align-items:center;gap:4px;padding:8px 10px;flex:none;'
    + 'border-bottom:1px solid rgba(127,127,127,.22)';
  if (label) {
    const sub = el('div', 'd', esc(label));
    sub.style.cssText = 'padding:0 10px 7px;flex:none;color:var(--faint,#5c7a8a);'
      + 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
    head.after(sub);
  }
  const filter = el('input');
  filter.type = 'search';
  filter.placeholder = 'filter prompts…';
  filter.style.cssText = 'margin:8px 10px;flex:none;padding:5px 8px;border-radius:6px;font:inherit;'
    + 'background:rgba(127,127,127,.10);border:1px solid rgba(127,127,127,.22);color:inherit';
  const list = el('div');
  list.style.cssText = 'overflow-y:auto;padding:0 10px 10px;flex:1 1 auto';
  pop.append(head, filter, list);
  document.body.appendChild(pop);
  _promptsPop = pop;

  const r = anchor && anchor.getBoundingClientRect();
  pop.style.left = Math.max(12, Math.min(r ? r.left : (innerWidth - 460) / 2, innerWidth - 472)) + 'px';
  pop.style.top = Math.max(12, Math.min(r ? r.bottom + 6 : 90, innerHeight - 200)) + 'px';

  let all = [];
  const card = (p, latest) => {
    const c = el('div', 'card');
    c.style.cssText = 'margin:8px 0 0;cursor:pointer;'
      + (latest ? 'border-color:var(--acc,#3fe3ff)' : '');
    const meta = el('div', 'd', (latest ? '<b style="color:var(--acc,#3fe3ff)">LATEST</b> · ' : '')
      + esc(timeAgo(p.ts))
      + (p.queued ? ' · <span class="chip">queued mid-turn</span>' : ''));
    meta.style.cssText = 'margin-bottom:4px';
    const body = el('div', '', esc(p.text));
    body.style.cssText = 'user-select:text;white-space:pre-wrap;word-break:break-word;'
      + (latest ? 'max-height:11em;overflow:auto' : 'display:-webkit-box;-webkit-line-clamp:3;'
        + '-webkit-box-orient:vertical;overflow:hidden');
    c.append(meta, body);
    c.title = 'Click to copy this prompt';
    c.onclick = () => copyPrompt(p.text, body).then(ok =>
      toast(ok ? 'prompt copied' : 'selected — press ⌘C to copy', 'ok'));
    return c;
  };
  const render = () => {
    const q = filter.value.trim().toLowerCase();
    const hits = q ? all.filter(p => p.text.toLowerCase().includes(q)) : all;
    list.replaceChildren();
    head.querySelector('.pcount').textContent = q
      ? `${hits.length}/${all.length}` : String(all.length);
    if (!hits.length) {
      list.appendChild(el('div', 'empty', all.length ? 'no prompt matches that' : 'no prompts yet'));
      return;
    }
    // newest first — the whole point is not having to scroll to find the last one
    hits.slice().reverse().forEach((p, i) => list.appendChild(card(p, i === 0 && !q)));
  };
  const load = async () => {
    list.replaceChildren(el('div', 'empty', 'READING TRANSCRIPT…'));
    const qs = src.term ? 'term=' + encodeURIComponent(src.term)
      : 'path=' + encodeURIComponent(src.path);
    const res = await apiSafe('/api/prompts?' + qs, undefined, { silent: true });
    if (!_promptsPop) return;                       // closed while we were fetching
    all = (res && res.prompts) || [];
    if (res && res.detail && !all.length) {
      list.replaceChildren(el('div', 'empty', esc(res.detail)));
      head.querySelector('.pcount').textContent = '0';
      return;
    }
    render();
  };
  head.querySelector('[data-a=close]').onclick = closePromptsPanel;
  head.querySelector('[data-a=refresh]').onclick = load;
  filter.oninput = render;
  pop.addEventListener('pointerdown', e => e.stopPropagation());
  document.addEventListener('keydown', _promptsKey, true);
  setTimeout(() => document.addEventListener('pointerdown', _promptsAway, true), 0);
  await load();
  filter.focus();
}

function openTermWindow(t, opts) {
  if (typeof Terminal === 'undefined') return toast('xterm.js not loaded (offline?)', 'err');
  WM.spawn({
    id: 'term:' + t.id, name: t.label, icon: I.term, w: 880, h: 540,
    cls: 'term-win', geoKey: 'term:' + t.id, accent: '#3fe3ff',
    render(body, win) {
      const holder = el('div', 'termhost');
      body.appendChild(holder);
      const th = CUR_THEME || THEMES[0];
      const acc = th.a, accSoft = th.s;
      const term = new Terminal({
        fontFamily: "'IBM Plex Mono',monospace", fontSize: 12.5, cursorBlink: true,
        scrollback: 8000,
        rightClickSelectsWord: true,            // right-click grabs the word under the cursor
        macOptionClickForcesSelection: true,    // ⌥-drag selects even while an app owns the mouse (tmux/vim)
        theme: { background: th.bg, foreground: th.fg, cursor: acc,
          cursorAccent: th.bg, selectionBackground: `rgba(${hexRGB(acc)},.30)`,
          black: '#0a141e', red: '#ff5d6c', green: '#4ef0a6', yellow: '#ffb45e',
          blue: '#3fa8ff', magenta: '#9d8cff', cyan: acc, white: '#c6dbe6',
          brightBlack: '#39525f', brightRed: '#ff8c96', brightGreen: '#8cf7c8',
          brightYellow: '#ffd39e', brightBlue: '#7cc4ff', brightMagenta: '#c0b5ff',
          brightCyan: accSoft, brightWhite: '#eaf7fc' } });
      const fit = new FitAddon.FitAddon();
      term.loadAddon(fit);
      term.open(holder);
      const ro = new ResizeObserver(() => { try { fit.fit(); } catch (err) { /* mid-teardown */ } });
      ro.observe(holder);
      body.addEventListener('pointerdown', () => term.focus());
      // refocus the xterm textarea whenever this window is activated (any path), so image/text
      // paste (⌘V → Claude reads the Mac clipboard) works regardless of how you raised the window.
      win.onActivate = () => { try { term.focus(); } catch (e) { /* mid-teardown */ } };

      // ✦ / ❯ — start a sibling session in this terminal's own directory.
      // t.cwd is the LAUNCH dir (the server records it at spawn); a `cd` inside the
      // shell doesn't move it, which is what you want — it stays the project root.
      // Runs BEFORE the ❝ button so .win-cwd exists for it to join.
      if (t.cwd) WM.setWinCwd(win, t.cwd);

      // ❝ prompts — the session's own prompts, so you never have to scroll the
      // scrollback back up to find what you last asked. Accented because it is the
      // one header control you reach for mid-task; the panel is shared with Sessions.
      if (win.head) {
        const bp = el('button', 'wc-ico wprompts', '❝');
        attachTip(bp, 'Prompts — what you\'ve asked in this session (click one to copy)');
        bp.onclick = e => { e.stopPropagation(); openPromptsPanel({ term: t.id }, bp, t.cwd || t.label); };
        const box = win.head.querySelector('.win-cwd');
        const btns = win.head.querySelector('.win-btns');
        if (box) box.appendChild(bp);          // joins the ✦/❯ group
        else if (btns) btns.before(bp);
        else win.head.appendChild(bp);

        // ◔ context — how full this session's window is. The glyph itself is the
        // readout: it tints green→amber→red with occupancy, so a session filling
        // up announces itself without being clicked. Click opens the detail.
        const bc = el('button', 'wc-ico wctx', '◔');
        attachTip(bc, 'Context — window occupancy for this session');
        bc.onclick = e => { e.stopPropagation(); openContextPanel(t.id, bc, t.cwd || t.label); };
        if (box) box.appendChild(bc); else if (btns) btns.before(bc);
        else win.head.appendChild(bc);
        // Poll rarely: a context window moves on the scale of turns, not seconds,
        // and this runs per terminal window.
        const tickCtx = async () => {
          if (win.min || !bc.isConnected) return;
          const c = await apiSafe('/api/context?term=' + encodeURIComponent(t.id),
            undefined, { silent: true });
          if (!c || !c.available || !bc.isConnected) return;
          const tone = c.pct >= 90 ? '#ff5d6c' : c.pct >= 70 ? '#ffb45e'
            : c.pct >= 40 ? 'var(--acc,#3fe3ff)' : 'var(--faint,#5c7a8a)';
          bc.style.color = tone;
          bc.style.borderColor = c.pct >= 70 ? tone : '';
          // the glyph fills as the window does — ◔◑◕● reads as a gauge at 12px
          bc.textContent = c.pct >= 88 ? '●' : c.pct >= 63 ? '◕' : c.pct >= 38 ? '◑' : '◔';
          attachTip(bc, `Context — ${c.pct}% of ${(c.window || 0).toLocaleString()} `
            + `(${(c.free || 0).toLocaleString()} free)`);
        };
        tickCtx();
        win.timers.push(setInterval(tickCtx, 45000));
      }

      // --- select & copy ---------------------------------------------------
      // Plain drag selects in a shell; when an app owns the mouse (tmux/vim, mouse mode on)
      // hold ⇧ (or ⌥) to select. Whatever you select is copied automatically on release,
      // and ⌘/Ctrl+⇧+C copies on demand. A small pill confirms the copy.
      const flashCopied = n => {
        const pill = el('div', 'term-copied', '✓ COPIED' + (n ? ` · ${n} char${n === 1 ? '' : 's'}` : ''));
        body.appendChild(pill);
        requestAnimationFrame(() => pill.classList.add('show'));
        setTimeout(() => { pill.classList.remove('show'); setTimeout(() => pill.remove(), 200); }, 1100);
      };
      // Copy that works in BOTH a secure context (localhost → Clipboard API) and a non-secure one
      // (plain-IP LAN → the Clipboard API is undefined). The non-secure fallback uses a one-shot `copy`
      // event handler + execCommand — it does NOT create/focus a textarea, so the terminal keeps its own
      // selection intact (a temp-textarea stole focus and cleared the highlight on mouse-release).
      const copyToClipboard = txt => {
        if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(txt);
        return new Promise((resolve, reject) => {
          const onCopy = e => { e.clipboardData.setData('text/plain', txt); e.preventDefault(); };
          document.addEventListener('copy', onCopy);
          let ok = false;
          try { ok = document.execCommand('copy'); } catch (e) { /* blocked */ }
          document.removeEventListener('copy', onCopy);
          ok ? resolve() : reject(new Error('copy blocked'));
        });
      };
      const copySelection = () => {
        const s = term.getSelection();
        if (!s) return false;
        copyToClipboard(s)
          .then(() => flashCopied(s.length))
          .catch(() => toast('copy blocked by the browser — open ZENITH at http://127.0.0.1:8777 (the clipboard needs a secure context)', 'err'));
        return true;
      };
      holder.addEventListener('mouseup', () => { if (term.hasSelection()) copySelection(); });
      // tmux runs with `mouse on`, so a drag-select is captured by tmux (not xterm) and copied to
      // tmux's buffer — the yellow highlight vanishes on release and never reaches the OS clipboard.
      // tmux `set-clipboard on` emits the selection as an OSC 52 sequence; catch it here and write it
      // to the browser/system clipboard, so a NORMAL drag-copy inside a tmux terminal just works.
      try {
        term.parser.registerOscHandler(52, data => {
          const semi = data.indexOf(';');
          const b64 = (semi >= 0 ? data.slice(semi + 1) : data).trim();
          if (!b64 || b64 === '?') return true;                 // clipboard *query* → ignore
          let text = '';
          try { text = new TextDecoder().decode(Uint8Array.from(atob(b64), c => c.charCodeAt(0))); }
          catch (e) { return true; }
          if (text) copyToClipboard(text).then(() => flashCopied(text.length)).catch(() => {});
          return true;                                          // handled — don't let it print
        });
      } catch (e) { /* older xterm without parser.registerOscHandler */ }
      // Image paste (⌘V of an image): the browser captures it and image bytes can't travel the text-only
      // PTY, so Claude Code's native ⌘V never receives it. Bridge it — upload the image to THIS Mac, save
      // a file, and type its path so Claude Code loads it from disk. Text paste falls through untouched.
      holder.addEventListener('paste', async ev => {
        const items = (ev.clipboardData && ev.clipboardData.items) || [];
        let file = null;
        for (const it of items) if (it.type && it.type.startsWith('image/')) { file = it.getAsFile(); break; }
        if (!file) return;                              // not an image → let text paste proceed normally
        ev.preventDefault(); ev.stopPropagation();
        const pill = el('div', 'term-copied', '⤴ uploading image…');
        body.appendChild(pill); requestAnimationFrame(() => pill.classList.add('show'));
        try {
          const resp = await fetch('/api/term/paste-image',
            { method: 'POST', headers: { 'Content-Type': file.type || 'image/png' }, body: file });
          const r = await resp.json().catch(() => ({}));
          if (resp.ok && r.path) { term.paste(r.path); pill.textContent = '✓ image · ' + r.path.split('/').pop(); }
          else { pill.textContent = '✗ ' + (r.error || 'image paste failed'); }
        } catch (e) { pill.textContent = '✗ image paste failed'; }
        setTimeout(() => { pill.classList.remove('show'); setTimeout(() => pill.remove(), 200); }, 1500);
      }, true);
      term.attachCustomKeyEventHandler(ev => {
        if (ev.type !== 'keydown') return true;
        const k = ev.key.toLowerCase();
        // ⌘+C (mac) or Ctrl+⇧+C: copy the selection instead of sending it to the PTY
        if (k === 'c' && ((ev.metaKey && !ev.ctrlKey) || (ev.ctrlKey && ev.shiftKey)) && term.hasSelection()) {
          copySelection(); return false;
        }
        // Ctrl+V (or Ctrl+⇧+V): paste clipboard TEXT into the PTY (term.paste honors bracketed-paste).
        // ⌘V is deliberately NOT intercepted — it stays native so xterm's paste + Claude Code's own
        // clipboard read (IMAGE paste, since ZENITH runs on the same Mac) keep working.
        if (k === 'v' && ev.ctrlKey && !ev.metaKey) {
          if (navigator.clipboard?.readText) {
            navigator.clipboard.readText().then(txt => { if (txt) term.paste(txt); }).catch(() => {});
            return false;
          }
        }
        return true;
      });

      // --- clickable file paths --------------------------------------------
      // Any file path printed by the shell or by Claude becomes a link: docs/text/code open in
      // the Files viewer window; html/pdf/images/svg open rendered in a new browser tab.
      const RENDER_EXT = ['html', 'htm', 'svg', 'pdf', 'png', 'jpg', 'jpeg', 'gif', 'webp'];
      // Must contain a slash — rooted (/ ~/ ./ ../) or a dir/ prefix. A bare "foo.md" is NOT linkified:
      // it can't be resolved reliably (a filename in prose resolves against cwd → wrong dir). Matches terminalx.
      const PATH_RE = /((?:\/|~\/|\.{1,2}\/|[\w.\-]*\/)[\w.\-/]*[\w\-]\.(?:md|markdown|txt|html?|json|ya?ml|jsx?|tsx?|py|css|scss|svg|pdf|png|jpe?g|gif|webp|sh|zsh|go|rs|rb|toml|ini|cfg|conf|log|csv|xml))(?::\d+(?::\d+)?)?/gi;
      const URL_RE = /https?:\/\/[^\s)>\]"'`]+/gi;   // clickable web links too
      // A relative path in output isn't always relative to the shell cwd — Claude may print a path
      // relative to a parent/project dir (e.g. cwd=…/workspace but "SomeProject/specs/x.md" lives one
      // level up). So try the cwd AND its ancestors, and open whichever actually exists.
      const candidatePaths = raw => {
        const p = raw.replace(/:\d+(?::\d+)?$/, '');          // drop :line:col suffix
        if (p[0] === '/' || p[0] === '~') return [p];         // absolute (or home) — as-is
        const rel = p.replace(/^\.\//, '');
        const cwd = (t.cwd && t.cwd !== '~') ? t.cwd.replace(/\/+$/, '') : null;
        if (!cwd) return [];
        const bases = []; let d = cwd;
        for (let i = 0; i < 6 && d && d !== '/'; i++) { bases.push(d); d = d.slice(0, d.lastIndexOf('/')); }
        return bases.map(b => b + '/' + rel);
      };
      const existsPath = async abs => {
        try { const c = new AbortController(); const r = await fetch('/raw?path=' + encodeURIComponent(abs), { signal: c.signal }); c.abort(); return r.ok; }
        catch (e) { return false; }
      };
      const openTermPath = async raw => {
        const cands = candidatePaths(raw);
        const name = raw.replace(/:\d+(?::\d+)?$/, '').split('/').pop() || raw;
        if (!cands.length) return toast('can’t resolve ' + raw + ' (unknown cwd)', 'err');
        const ext = (name.split('.').pop() || '').toLowerCase();
        const render = RENDER_EXT.includes(ext);
        // for a new-tab render, open the blank tab synchronously (popup-blocker friendly) before we await
        const tab = render ? window.open('', '_blank') : null;
        let found = null;
        for (const abs of cands) { if (await existsPath(abs)) { found = abs; break; } }
        if (!found) {   // couldn't resolve any candidate — search the filename in Files so the user can pick it
          if (tab) tab.close();
          toast('couldn’t locate ' + name + ' — searching Files');
          WM.open('files'); setTimeout(() => Bus.emit('files:search', name), 80);
          return;
        }
        if (render) {
          const url = '/raw?path=' + encodeURIComponent(found);
          if (tab) tab.location = url; else window.open(url, '_blank');
        } else {   // docs/text/code → in-OS Files viewer
          WM.open('files'); setTimeout(() => Bus.emit('files:openfile', found), 80);
        }
      };
      const openLink = h => h.kind === 'url' ? window.open(h.raw, '_blank', 'noopener') : openTermPath(h.raw);
      // Reconstruct the full LOGICAL line that buffer row `row0` (0-based) belongs to — a path/URL that
      // wraps a narrow pane is otherwise split across rows and never matched.
      const logicalLine = row0 => {
        const buf = term.buffer.active, cols = term.cols;
        let first = row0;
        while (first > 0 && buf.getLine(first)?.isWrapped) first--;
        let text = '';
        for (let r = first; r < buf.length; r++) {
          const ln = buf.getLine(r);
          if (!ln || (r > first && !ln.isWrapped)) break;
          let s = ln.translateToString(false);
          s = s.length < cols ? s.padEnd(cols, ' ') : s.slice(0, cols);
          text += s;
        }
        return { text, first, cols };
      };
      const scanText = text => {   // all file-path + URL hits in a logical line
        const out = [];
        for (const m of text.matchAll(PATH_RE)) out.push({ raw: m[0], idx: m.index, kind: 'path' });
        for (const m of text.matchAll(URL_RE)) out.push({ raw: m[0], idx: m.index, kind: 'url' });
        return out;
      };
      // xterm's own link handling — gives the hover underline + click when NO app owns the mouse (plain shell).
      term.registerLinkProvider({
        provideLinks(y, cb) {
          const { text, first, cols } = logicalLine(y - 1);
          const at = i => ({ x: (i % cols) + 1, y: first + Math.floor(i / cols) + 1 });
          const links = scanText(text).map(h => ({ text: h.raw, range: { start: at(h.idx), end: at(h.idx + h.raw.length) }, activate: () => openLink(h) }));
          cb(links.length ? links : undefined);
        }
      });
      // When an app owns the mouse (Claude TUI, vim, tmux mouse), xterm forwards clicks to it and the
      // link provider never fires. Intercept a click over a link here first (capture phase) so a plain
      // single click still opens it — no modifier — and show a pointer over links.
      const linkAt = ev => {
        const screen = term.element && term.element.querySelector('.xterm-screen');
        if (!screen) return null;
        const rect = screen.getBoundingClientRect();
        if (ev.clientX < rect.left || ev.clientX > rect.right || ev.clientY < rect.top || ev.clientY > rect.bottom) return null;
        const dim = term._core._renderService.dimensions.css.cell;
        const col = Math.floor((ev.clientX - rect.left) / dim.width);
        const rowInView = Math.floor((ev.clientY - rect.top) / dim.height);
        const bufRow = term.buffer.active.viewportY + rowInView;
        const { text, first, cols } = logicalLine(bufRow);
        const clickIdx = (bufRow - first) * cols + col;
        for (const h of scanText(text)) if (clickIdx >= h.idx && clickIdx < h.idx + h.raw.length) return h;
        return null;
      };
      holder.addEventListener('mousemove', ev => {   // pointer cursor over a link (set on the screen el for specificity)
        const screen = term.element && term.element.querySelector('.xterm-screen');
        if (screen) screen.style.cursor = linkAt(ev) ? 'pointer' : '';
      });
      holder.addEventListener('mousedown', ev => { if (ev.button === 0 && linkAt(ev)) { ev.preventDefault(); ev.stopPropagation(); } }, true);
      holder.addEventListener('click', ev => { const h = linkAt(ev); if (h) { ev.preventDefault(); ev.stopPropagation(); openLink(h); } }, true);

      // local status strip (mode · path · live/tmux) — the window's own statusline
      const mode = (t.mode || 'shell').replace('claude-', 'claude ');
      const effTag = (t.effort && t.mode !== 'shell') ? `<span style="color:var(--acc)">effort:${esc(t.effort)}</span>` : '';
      const strip = el('div', 'term-strip',
        `<span class="sled"></span><span>${esc(mode)}</span>${effTag}
         <span class="spath">${esc(t.cwd || '~')}</span>
         <span style="margin-left:auto">${t.persist ? 'tmux' : 'pty'} · ${esc((t.id || '').slice(0, 8))}</span>`);
      body.appendChild(strip);
      const setLed = state => { const l = strip.querySelector('.sled'); if (!l) return;
        const c = state === 'live' ? 'var(--green)' : state === 'reconnect' ? 'var(--amber)' : 'var(--red)';
        l.style.background = c; l.style.boxShadow = '0 0 6px ' + c; };
      let overlay = null;
      const clearOverlay = () => { if (overlay) { overlay.remove(); overlay = null; } };
      const showOverlay = (cls, txt) => { clearOverlay();
        overlay = el('div', 'term-dead' + (cls ? ' ' + cls : ''), esc(txt)); body.appendChild(overlay); };

      // --- WebSocket with auto-reconnect. The PTY + a 250KB scrollback buffer live
      // server-side, so a dropped socket (tab throttle, brief server bounce, network
      // blip) silently re-attaches and replays; only a truly gone session gives up. ---
      let ws = null, closed = false, exited = false, firstConnect = true, attempts = 0, reconnectT = null;
      // While the server replays its scrollback buffer, xterm re-parses old DA/DSR *queries* embedded
      // in that history and auto-answers them. Those replies must NOT be echoed to the PTY — the shell
      // is at a prompt and they land as junk like "1;2c0;276;0c". Gate onData off during replay.
      let replaying = false, replayT = null;
      const armReplay = () => { replaying = true; clearTimeout(replayT); replayT = setTimeout(() => { replaying = false; }, 700); };
      const endReplay = () => { replaying = false; clearTimeout(replayT); };
      const sendResize = () => { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ t: 'resize', cols: term.cols, rows: term.rows })); };
      // is the session still alive server-side? 'alive' | 'gone' | 'server-down'
      const termAlive = async id => {
        try {
          const r = await fetch('/api/term/list', { signal: AbortSignal.timeout(2500) }).then(x => x.json());
          return (r.terms || []).some(x => x.id === id && x.status === 'live') ? 'alive' : 'gone';
        } catch (e) { return 'server-down'; }
      };
      const shortCwd = (t.cwd || '~').split('/').filter(Boolean).pop() || '~';
      const showReconnecting = label => {
        clearOverlay();
        overlay = el('div', 'term-dead reconnect');
        overlay.innerHTML = `<div style="text-align:center"><div style="margin-bottom:10px">${esc(label)}…</div>
          <button class="btn ghost sm" data-r="now">↻ RECONNECT NOW</button></div>`;
        body.appendChild(overlay);
        overlay.querySelector('[data-r=now]').onclick = () => { clearTimeout(reconnectT); attempts = 0; clearOverlay(); connect(); };
      };
      // session is genuinely gone — offer to reconnect / resume the claude conversation / new shell
      const showRecovery = () => {
        setLed('down'); clearOverlay();
        overlay = el('div', 'term-dead recover');
        overlay.innerHTML = `<div style="text-align:center">
          <div style="margin-bottom:12px;letter-spacing:.2em">SESSION ENDED</div>
          <div style="display:flex;gap:8px;justify-content:center;flex-wrap:wrap">
            <button class="btn acc sm" data-r="reconnect">↻ RECONNECT</button>
            <button class="btn acc sm" data-r="continue">▸ CONTINUE CLAUDE</button>
            <button class="btn ghost sm" data-r="shell">⌗ NEW SHELL</button></div>
          <div style="margin-top:11px;color:var(--faint);font-size:10px;letter-spacing:.05em;text-transform:none">
            continue = resume the last claude conversation in <b>${esc(shortCwd)}</b></div></div>`;
        body.appendChild(overlay);
        overlay.querySelector('[data-r=reconnect]').onclick = () => { attempts = 0; clearOverlay(); connect(); };
        overlay.querySelector('[data-r=continue]').onclick = () => { WM.close(win); launchTerm(t.cwd, 'claude-continue'); };
        overlay.querySelector('[data-r=shell]').onclick = () => { WM.close(win); launchTerm(t.cwd, 'shell'); };
      };
      const connect = () => {
        ws = new WebSocket(`${location.protocol==='https:'?'wss:':'ws:'}//${location.host}/ws/term?id=${t.id}`);
        ws.binaryType = 'arraybuffer';
        if (ZTERMS[t.id]) ZTERMS[t.id].ws = ws;
        ws.onopen = () => {
          attempts = 0; clearOverlay(); setLed('live');
          if (!firstConnect) { term.reset(); armReplay(); }   // reconnect: server replays scrollback; swallow its query-replies
          firstConnect = false;
          fit.fit(); sendResize(); term.focus();
        };
        ws.onmessage = e => {
          if (typeof e.data === 'string') {
            try { const m = JSON.parse(e.data);
              if (m.t === 'exit') { exited = true; setLed('down'); showOverlay('', 'PROCESS ENDED'); }
              else if (m.t === 'replay') armReplay();   // server marks the historical snapshot frame
            } catch (err) { /* not a control frame */ }
            return;
          }
          const wasReplaying = replaying;
          term.write(new Uint8Array(e.data), () => { if (wasReplaying) endReplay(); });
        };
        ws.onclose = async () => {
          setLed('down');
          if (closed || exited || !WM.wins.has('term:' + t.id)) return;
          attempts++;
          // Fast auto-retry for the first ~12 tries (covers a normal server bounce).
          if (attempts <= 12) {
            setLed('reconnect'); showOverlay('reconnect', 'RECONNECTING…');
            reconnectT = setTimeout(connect, Math.min(3000, 300 * attempts));
            return;
          }
          // Still failing — is the session actually gone, or is the server just down?
          // tmux-backed sessions live forever, so we NEVER dead-end while it exists.
          const state = await termAlive(t.id);
          if (state === 'gone') { showRecovery(); return; }   // truly gone → offer resume/continue
          setLed('reconnect');                                 // alive or server-down → keep trying
          showReconnecting(state === 'server-down' ? 'SERVER OFFLINE — retrying' : 'RECONNECTING');
          reconnectT = setTimeout(connect, 5000);
        };
        ws.onerror = () => { /* onclose follows and drives the reconnect */ };
      };
      connect();
      term.onData(d => { if (replaying) return;   // don't echo replay-triggered DA/DSR replies back to the PTY
        if (ws && ws.readyState === 1) ws.send(JSON.stringify({ t: 'in', d })); });
      term.onResize(sendResize);
      const hbT = setInterval(() => { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ t: 'ping' })); }, 25000);
      // voice input — words stream live into an editable composer; Enter sends to the
      // PTY, Esc cancels. (terminalx model: nothing hits the shell until you commit.)
      const sendToPTY = txt => { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ t: 'in', d: txt })); };
      const mic = el('button', 'term-mic', '🎙');
      mic.title = 'Voice input (Whisper / browser speech)';
      const vbox = el('div', 'term-voice');
      const vta = el('textarea', 'tv-text'); vta.rows = 2; vta.spellcheck = false;
      const vhint = el('div', 'tv-hint', '');
      vbox.append(vta, vhint);
      let userEdited = false;                     // stop streaming from clobbering manual edits
      // grow the composer upward (it's anchored bottom) to fit the whole transcript, up to a cap
      const autoGrow = () => { vta.style.height = 'auto'; vta.style.height = Math.min(vta.scrollHeight, 320) + 'px'; };
      vta.addEventListener('input', () => { userEdited = true; autoGrow(); });
      const closeVoice = () => { if (Voice.active) Voice.active.stop();
        vbox.classList.remove('show'); vta.value = ''; vta.style.height = ''; userEdited = false; mic.classList.remove('rec', 'busy'); };
      mic.onclick = e => { e.stopPropagation();
        if (Voice.active) { Voice.active.stop(); vhint.textContent = 'Enter send · Esc cancel · 🎙 redo'; vta.focus(); return; }
        vbox.classList.add('show'); vta.value = ''; vta.style.height = ''; userEdited = false; vta.focus();
        vhint.textContent = 'listening… · Enter send · Esc cancel';
        Voice.toggle(full => { if (!userEdited) { vta.value = full; autoGrow(); vta.scrollTop = vta.scrollHeight; } }, mic);
      };
      vta.addEventListener('keydown', ev => {
        if (ev.key === 'Enter' && !ev.shiftKey) {
          ev.preventDefault();
          const txt = vta.value.trim();
          closeVoice();
          if (txt) sendToPTY(txt + '\r');
          term.focus();
        } else if (ev.key === 'Escape') { ev.preventDefault(); closeVoice(); term.focus(); }
      });
      body.append(mic, vbox);
      // wake/resume: force an immediate reconnect (reset backoff) unless the socket is already live
      const wakeReconnect = () => {
        if (closed || exited || !WM.wins.has('term:' + t.id)) return;
        if (ws && ws.readyState === 1) return;                 // already connected — leave it
        clearTimeout(reconnectT); attempts = 0; clearOverlay(); connect();
      };
      ZTERMS[t.id] = { term, ws, reconnect: wakeReconnect };
      win.cleanup = () => { closed = true; clearTimeout(reconnectT); clearInterval(hbT);
        ro.disconnect(); try { ws && ws.close(); } catch (err) {} term.dispose();
        if (Voice.active) Voice.active.stop(); delete ZTERMS[t.id]; };
    }
  }, opts);
}

// Render a local file (HTML/SVG/PDF/image) in an in-OS browser window via /raw.
function openRendered(path, title) {
  const src = '/raw?path=' + encodeURIComponent(path);
  WM.spawn({
    id: 'view:' + path, name: title || path.split('/').pop(), icon: I.globe,
    w: 1040, h: 720, cls: 'view-win', geoKey: 'view', accent: '#9d8cff',
    render(body) {
      const bar = el('div', 'viewbar');
      const url = el('span', 'viewurl', esc(path));
      const reload = el('button', 'btn ghost sm', '⟳');
      const tab = el('button', 'btn ghost sm', '↗ NEW TAB');
      bar.append(url, reload, tab);
      const frame = el('iframe', 'viewframe');
      frame.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms allow-popups allow-modals');
      frame.src = src;
      reload.onclick = () => { frame.src = 'about:blank'; setTimeout(() => frame.src = src, 30); };
      tab.onclick = () => window.open(src, '_blank');
      body.append(bar, frame);
    }
  });
}

// recent-projects tracking (TerminalX-style), persisted in localStorage
function getRecents() {
  try { return JSON.parse(localStorage.getItem('zen.recents')) || []; } catch (e) { return []; }
}
function trackRecentProject(cwd, mode) {
  if (!cwd) return;
  const name = cwd.split('/').filter(Boolean).pop() || cwd;
  let list = getRecents().filter(r => r.path !== cwd);
  list.unshift({ name, path: cwd, mode: mode || 'shell', usedAt: Date.now() });
  list = list.slice(0, 12);
  try { localStorage.setItem('zen.recents', JSON.stringify(list)); } catch (e) { /* quota */ }
  Bus.emit('recents:changed', list);
}

function defaultEffort() { return localStorage.getItem('zen.effort') || 'high'; }
// persist defaults ON (tmux, survives restarts) unless the caller passes an explicit boolean
function defaultPersist() { return localStorage.getItem('zen.persist') !== '0'; }
// `worktree` is the prime-agent mode's remote work dir on the GPU box; ignored by every
// other mode, and the server re-validates it (it lands in a remote shell command).
async function launchTerm(cwd, mode, persist, effort, worktree) {
  if (cwd) { localStorage.setItem('zen.lastcwd', cwd); trackRecentProject(cwd, mode); }
  const eff = effort || defaultEffort();
  const p = (persist === undefined) ? defaultPersist() : !!persist;
  const body = { cwd: cwd || null, mode: mode || 'shell', persist: p, effort: eff };
  if (worktree) body.worktree = worktree;
  const r = await apiSafe('/api/term', { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body) });
  if (r) openTermWindow(r.term);
  return r;
}

/* ---- interactive-agent launch (P4): split button + default agent ----
   Agent id === launch mode (1:1): 'claude'|'codex'|'aider'. AGENTS_ENABLED is a
   module-level cache of the enabled /api/agents2 ids, refreshed by refreshEnabledAgents()
   (calls loadEnabledAgents() from apps.js); used to gate the ▾ menu and ⌘K actions.
   Built with safe DOM methods (textContent) — no innerHTML with dynamic ids. */
let AGENTS_ENABLED = ['claude'];                // safe default until /api/agents2 answers
const AGENT_ACCENT = { claude: 'var(--cyan)', codex: 'var(--green)', aider: 'var(--violet)' };
const AGENT_LABEL = { claude: 'Claude', codex: 'Codex', aider: 'aider' };
async function refreshEnabledAgents() {
  try {
    if (typeof loadEnabledAgents !== 'function') return AGENTS_ENABLED;
    const list = await loadEnabledAgents();     // [{id,label,...}]
    const ids = (list || []).map(a => a.id).filter(Boolean);
    AGENTS_ENABLED = ids.length ? ids : ['claude'];
  } catch (e) { /* keep last-known */ }
  return AGENTS_ENABLED;
}
function defaultAgent() {
  const d = Settings.load().defaultAgent || 'claude';
  return AGENTS_ENABLED.includes(d) ? d : (AGENTS_ENABLED[0] || 'claude');
}
// close any open agent menu (single-open model; also wired to click-outside)
function closeAgentMenus() { document.querySelectorAll('.agent-menu.show').forEach(m => m.remove()); }
/* Returns a split-button DOM element that launches defaultAgent() from its primary part
   and offers the enabled agents in a ▾ popup. Shared by every project launch surface. */
function agentLaunchButton(cwd, opts) {
  const o = opts || {};
  const def = defaultAgent();
  const wrap = el('span', o.sm ? 'agent-split sm' : 'agent-split');
  wrap.style.setProperty('--acc', AGENT_ACCENT[def] || 'var(--cyan)');   // both parts pick up the agent accent
  const primary = el('button', 'btn acc agsm agent-primary');
  primary.textContent = '▸ ' + (AGENT_LABEL[def] || def).toUpperCase();
  primary.title = 'launch ' + (AGENT_LABEL[def] || def);
  primary.onclick = e => { e.stopPropagation(); closeAgentMenus(); launchTerm(cwd, defaultAgent()); };
  const caret = el('button', 'btn acc agsm agent-caret');
  caret.textContent = '▾'; caret.title = 'choose agent';
  caret.onclick = e => {
    e.stopPropagation();
    const already = wrap.querySelector('.agent-menu');
    closeAgentMenus();
    if (already) return;                        // toggle off if it was already open
    const cur = defaultAgent();
    const menu = el('div', 'agent-menu show');
    AGENTS_ENABLED.forEach(id => {
      const item = el('button', 'agent-item');
      item.style.setProperty('--acc', AGENT_ACCENT[id] || 'var(--cyan)');
      const dot = el('span', 'agent-dot');
      const ck = el('span', 'agent-ck'); ck.textContent = id === cur ? '✓' : '';
      item.appendChild(dot);
      item.appendChild(document.createTextNode('▸ ' + (AGENT_LABEL[id] || id)));
      item.appendChild(ck);
      item.onclick = ev => { ev.stopPropagation(); closeAgentMenus(); launchTerm(cwd, id); };
      menu.appendChild(item);
    });
    wrap.appendChild(menu);
  };
  wrap.appendChild(primary); wrap.appendChild(caret);
  return wrap;
}
// dismiss agent menus on any outside click (registered once)
document.addEventListener('click', e => {
  if (!e.target.closest || !e.target.closest('.agent-split')) closeAgentMenus();
});

// Sessions v2: resume a Claude session by id in its own cwd
async function resumeSession(sess) {
  const r = await apiSafe('/api/term', { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cwd: sess.cwd || null, mode: 'claude-resume', resume_id: sess.id,
      persist: defaultPersist(), effort: defaultEffort() }) });   // tmux-backed so it survives server restarts
  if (r) { if (sess.cwd) trackRecentProject(sess.cwd, 'claude-resume');
    openTermWindow(r.term); toast('resuming ' + (sess.title || sess.id).slice(0, 40), 'ok'); }
  return r;
}

/* ================= command palette ================= */
const Palette = {
  sel: 0, items: [],
  open() {
    $('#pal').classList.add('show');
    const inp = $('#palinput');
    inp.value = ''; inp.focus();
    this.query('');
  },
  close() { $('#pal').classList.remove('show'); },
  systemCommands() {
    return [
      { icon: I.tile, label: 'Auto-arrange windows', kw: 'tile all window arrange', run: () => WM.tile(null, { label: 'all' }) },
      { icon: I.tile, label: 'Arrange: vertical columns', kw: 'arrange columns vertical layout', run: () => WM.applyArrange('columns') },
      { icon: I.tile, label: 'Arrange: tiled grid', kw: 'arrange tile grid layout', run: () => WM.applyArrange('tiled') },
      { icon: I.tile, label: 'Arrange: cascade', kw: 'cascade window stagger', run: () => WM.applyArrange('cascade') },
      { icon: I.tile, label: 'Cycle arrange mode', kw: 'cycle arrange columns tiles cascade', run: () => WM.cycleArrange() },
      { icon: I.tile, label: 'Tile terminals only', kw: 'tile terminal', run: () => WM.tile(id => id.startsWith('term:'), { label: 'terminals' }) },
      { icon: I.tile, label: 'Show all desktops (Exposé)', kw: 'expose desktop workspace show all overview', run: () => { if (WM.desktopCount() < 2) WM.setDesktopCount(2); WM.expose(); } },
      { icon: I.tile, label: 'New desktop', kw: 'new desktop workspace add virtual screen', run: () => { WM.setDesktopCount(WM.desktopCount() + 1); WM.switchDesktop(WM.desktopCount()); } },
      { icon: I.tile, label: 'Next desktop', kw: 'next desktop workspace switch', run: () => WM.stepDesktop(1) },
      { icon: I.tile, label: 'Previous desktop', kw: 'previous desktop workspace switch', run: () => WM.stepDesktop(-1) },
      { icon: I.term, label: 'New terminal (last dir)', kw: 'new terminal shell', run: () => launchTerm(localStorage.getItem('zen.lastcwd') || '', 'shell') },
      { icon: I.gear, label: 'Dock: bottom (restore)', kw: 'dock position bottom show restore unhide', run: () => Settings.set('dockPos', 'bottom') },
      { icon: I.gear, label: 'Dock: left', kw: 'dock position left', run: () => Settings.set('dockPos', 'left') },
      { icon: I.gear, label: 'Dock: right', kw: 'dock position right', run: () => Settings.set('dockPos', 'right') },
      { icon: I.gear, label: 'Dock: top', kw: 'dock position top', run: () => Settings.set('dockPos', 'top') },
      { icon: I.gear, label: 'Dock: auto-hide', kw: 'dock position autohide hide', run: () => Settings.set('dockPos', 'autohide') },
      { icon: I.gear, label: 'UI scale 90%', kw: 'ui scale zoom size', run: () => setScale(90) },
      { icon: I.gear, label: 'UI scale 100%', kw: 'ui scale zoom size', run: () => setScale(100) },
      { icon: I.gear, label: 'UI scale 110%', kw: 'ui scale zoom size', run: () => setScale(110) },
      { icon: I.gear, label: 'UI scale 125%', kw: 'ui scale zoom size', run: () => setScale(125) },
    ];
  },
  async query(q) {
    const items = [];
    APPS.forEach(a => { if (!Caps.appVisible(a)) return;   // Phase-3: hide launchers for gated/hidden modules
      items.push({ icon: a.icon, label: a.name, kind: 'app',
      score: a.name.toLowerCase().indexOf(q) === 0 ? 3 : a.name.toLowerCase().includes(q) ? 2 : q ? 0 : 1,
      run: () => WM.open(a.id) }); });
    this.systemCommands().forEach(c => {
      const hit = !q || c.label.toLowerCase().includes(q) || (c.kw || '').includes(q);
      items.push({ icon: c.icon, label: c.label, kind: 'command',
        score: q ? (hit ? (c.label.toLowerCase().includes(q) ? 2.4 : 1.4) : 0) : 0.4, run: c.run });
    });
    State.projects.forEach(p => {
      const s = p.name.toLowerCase().includes(q) ? (p.sessions ? 2.5 : 1.5) : 0;
      if (s || !q) items.push({ icon: I.files, label: p.name, sub: p.sessions + ' sessions', kind: 'project',
        score: q ? s : 0.5,
        run: () => { WM.open('files'); setTimeout(() => Bus.emit('files:open', p.path), 60); } });
      if (s) items.push({ icon: I.ops, label: 'Summarize ' + p.name, kind: 'action', score: s - 0.4,
        run: () => { WM.open('ops'); setTimeout(() => Bus.emit('ops:prefill', { project: p.path, preset: 'summarize' }), 60); } });
      if (s) items.push({ icon: I.term, label: 'Terminal in ' + p.name, kind: 'action', score: s - 0.3,
        run: () => launchTerm(p.path, 'shell') });
      if (s) items.push({ icon: I.term, label: 'Claude in ' + p.name, kind: 'action', score: s - 0.35,
        run: () => launchTerm(p.path, 'claude') });
      // interactive Codex / aider — only when enabled (AGENTS_ENABLED cache, warmed at boot)
      if (s && AGENTS_ENABLED.includes('codex')) items.push({ icon: I.term, label: 'Codex in ' + p.name,
        kind: 'action', score: s - 0.36, run: () => launchTerm(p.path, 'codex') });
      if (s && AGENTS_ENABLED.includes('aider')) items.push({ icon: I.term, label: 'Aider in ' + p.name,
        kind: 'action', score: s - 0.37, run: () => launchTerm(p.path, 'aider') });
    });
    State.sessions.slice(0, 24).forEach(s => {
      const label = s.title || s.first_prompt || s.id;
      if (!q || label.toLowerCase().includes(q)) {
        items.push({ icon: I.sessions, label: label.slice(0, 70), sub: s.project_name, kind: 'session',
          score: q ? 1.8 : 0.2, run: () => WM.open('sessions') });
        items.push({ icon: I.term, label: 'Resume: ' + label.slice(0, 60), sub: s.project_name, kind: 'resume',
          score: q ? 1.7 : 0.15, run: () => resumeSession(s) });
      }
    });
    if (typeof Chats !== 'undefined') Chats.all().slice(0, 12).forEach(ch => {
      const label = ch.title || 'chat';
      if (!q || label.toLowerCase().includes(q)) items.push({ icon: I.chat, label: 'Chat: ' + label.slice(0, 50),
        sub: modelLabel(ch.model), kind: 'chat', score: q ? 1.6 : 0.2,
        run: () => openChatWindow({ id: ch.id, providerId: ch.provider, providerName: ch.providerName,
          model: ch.model, messages: ch.messages, title: ch.title }) });
    });
    this.items = items.filter(i => i.score > 0).sort((a, b) => b.score - a.score).slice(0, 16);
    if (q.length > 2 && Caps.tabVisible('library', 'memory')) {   // Phase-3: no Memory results when its tab is hidden
      const r = await apiSafe('/api/memory?q=' + encodeURIComponent(q) + '&limit=5', undefined, { silent: true });
      (r?.memories || []).forEach(m => this.items.push({ icon: I.mem, label: m.title || m.key,
        sub: 'memory · ' + m.namespace, kind: 'memory', score: 1,
        run: () => { WM.open('memory'); setTimeout(() => {
          const w = WM.wins.get('memory'); const inp = w && w.body.querySelector('#nmq');
          if (inp) { inp.value = q; inp.dispatchEvent(new Event('input')); } }, 80); } }));
    }
    this.sel = 0;
    this.render();
  },
  render() {
    $('#palresults').innerHTML = this.items.map((it, i) => `
      <div class="pal-item ${i === this.sel ? 'sel' : ''}" data-i="${i}">
        ${it.icon}<span class="pt">${esc(it.label)}${it.sub ? `<small>${esc(it.sub)}</small>` : ''}</span>
        <span class="pk">${esc(it.kind)}</span></div>`).join('');
    document.querySelectorAll('.pal-item').forEach(e => {
      e.onclick = () => this.choose(+e.dataset.i);
      e.onmousemove = () => { this.sel = +e.dataset.i; this.render(); };
    });
  },
  choose(i) {
    const it = this.items[i];
    if (it) { this.close(); it.run(); }
  }
};
$('#palinput').addEventListener('input', e => Palette.query(e.target.value.toLowerCase()));
$('#palinput').addEventListener('keydown', e => {
  if (e.key === 'ArrowDown') { Palette.sel = Math.min(Palette.items.length - 1, Palette.sel + 1); Palette.render(); e.preventDefault(); }
  if (e.key === 'ArrowUp') { Palette.sel = Math.max(0, Palette.sel - 1); Palette.render(); e.preventDefault(); }
  if (e.key === 'Enter') Palette.choose(Palette.sel);
});
$('#pal').addEventListener('mousedown', e => { if (e.target.id === 'pal') Palette.close(); });

/* auto-hide dock: reveal it whenever the pointer nears the bottom edge (robust
   replacement for CSS :hover, which is unreliable on an off-screen element). */
addEventListener('pointermove', e => {
  if ((Settings.load().dockPos || 'bottom') !== 'autohide') return;
  document.body.classList.toggle('dock-peek', e.clientY > innerHeight - 64);
});
/* click-to-pin: double-click the dock's own chrome (not an app icon) to hold an
   auto-hidden dock open; double-click again to release. Persisted across reloads. */
function setDockPin(on) {
  document.body.classList.toggle('dock-pinned', on);
  try { localStorage.setItem('zen.dockpin', on ? '1' : '0'); } catch (e) { /* quota */ }
}
function restoreDockPin() {
  setDockPin((Settings.load().dockPos || 'bottom') === 'autohide'
    && localStorage.getItem('zen.dockpin') === '1');
}
addEventListener('DOMContentLoaded', () => {
  const dock = document.getElementById('dock');
  if (dock) dock.addEventListener('dblclick', e => {
    if ((Settings.load().dockPos || 'bottom') !== 'autohide') return;
    if (e.target.closest('.dock-item')) return;   // let icons handle their own clicks
    setDockPin(!document.body.classList.contains('dock-pinned'));
    toast(document.body.classList.contains('dock-pinned') ? 'dock pinned open' : 'dock unpinned', 'ok');
  });
  restoreDockPin();
});

/* ================= global keyboard shortcuts ================= */
// normalized modifier set for the current event, e.g. "alt+ctrl" (sorted, key excluded)
function evMods(e) {
  return [e.ctrlKey && 'ctrl', e.altKey && 'alt', e.shiftKey && 'shift', e.metaKey && 'meta']
    .filter(Boolean).sort().join('+');
}
addEventListener('keydown', e => {
  const k = e.key.toLowerCase();
  if ((e.metaKey || e.ctrlKey) && k === 'k' && !e.shiftKey) { e.preventDefault();
    $('#pal').classList.contains('show') ? Palette.close() : Palette.open(); return; }
  if (e.key === 'Escape') { if (document.getElementById('expose')) WM.closeExpose(); Palette.close(); return; }
  // ---- virtual-desktop navigation (Control scheme by default, editable in Settings) ----
  if (WM.desktopCount() > 1) {
    const base = Settings.load().deskScheme === 'ctrl+alt' ? 'alt+ctrl' : 'ctrl';   // sorted
    const sendBase = Settings.load().deskScheme === 'ctrl+alt' ? 'alt+ctrl+shift' : 'ctrl+shift';
    const mods = evMods(e);
    const digit = /^Digit([0-9])$/.exec(e.code);
    if (mods === base && digit) {
      const d = +digit[1];
      if (d === 0) { e.preventDefault(); WM.expose(); return; }
      if (d <= WM.desktopCount()) { e.preventDefault(); WM.switchDesktop(d); return; }
    }
    // Arrow-step modifier is its own setting (deskArrowMod: meta|alt|shift|ctrl, default ⌘ for mac).
    // ⌘ works even inside a focused terminal — xterm consumes plain ⌥+arrow for word-nav but passes ⌘
    // through. Real text inputs (chat/rename/search) are skipped so <mod>+arrow cursor-nav works there.
    const arrowMod = Settings.load().deskArrowMod || 'meta';
    if (mods === arrowMod && (e.code === 'ArrowLeft' || e.code === 'ArrowRight')) {
      const ae = document.activeElement;
      const inTextInput = ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.isContentEditable)
        && !(ae.classList && ae.classList.contains('xterm-helper-textarea'));
      if (!inTextInput) { e.preventDefault(); WM.stepDesktop(e.code === 'ArrowRight' ? 1 : -1); return; }
    }
    if (mods === sendBase && digit && +digit[1] >= 1 && +digit[1] <= WM.desktopCount()) {
      e.preventDefault(); WM.sendFocusedToDesktop(+digit[1]); return;
    }
  }
  if ((e.metaKey || e.ctrlKey) && e.shiftKey) {
    if (k === 'a') { e.preventDefault(); WM.applyArrange(); }
    else if (k === 't') { e.preventDefault(); WM.tile(id => id.startsWith('term:'), { label: 'terminals' }); }
    else if (k === 's') { e.preventDefault(); WM.tile(id => id.startsWith('term:') || id === 'terminal', { label: 'terminals + sessions' }); }
    else if (k === 'c') { e.preventDefault(); WM.cascade(null); }
    else if (k === 'n') { e.preventDefault(); launchTerm(localStorage.getItem('zen.lastcwd') || '', 'shell'); }
  }
});

/* ================= starfield ================= */
/* ================= themes (accent schemes, TerminalX-style) ================= */
// a = UI/terminal accent, s = soft, d = dim, fg = terminal text (primary), bg = terminal background
const THEMES = [
  { id: 'zenith',  name: 'Zenith Cyan', a: '#3fe3ff', s: '#8ceeff', d: '#1e8fa8', fg: '#c6dbe6', bg: '#020609' },
  { id: 'ice',     name: 'Ice Blue',    a: '#7cc4ff', s: '#b5deff', d: '#3a72b0', fg: '#cfe4f5', bg: '#04080f' },
  { id: 'emerald', name: 'Emerald',     a: '#4ef0a6', s: '#8cf7c8', d: '#1f8a5c', fg: '#b8f0d4', bg: '#03100a' },
  { id: 'summit',  name: 'Summit',      a: '#3ecf8e', s: '#7fe6b8', d: '#2a9668', fg: '#2bc42b', bg: '#0f1117' },
  { id: 'mint',    name: 'Mint',        a: '#5fe0c8', s: '#a6f0e2', d: '#2a8a78', fg: '#bff0e6', bg: '#04110e' },
  { id: 'gold',    name: 'Solar Gold',  a: '#ffd36e', s: '#ffe6a8', d: '#b8912a', fg: '#f0e2c0', bg: '#0f0c05' },
  { id: 'amber',   name: 'Amber Flux',  a: '#ffb45e', s: '#ffd39e', d: '#b6702a', fg: '#f0dcc0', bg: '#100b05' },
  { id: 'crimson', name: 'Crimson',     a: '#ff5d6c', s: '#ff9aa3', d: '#b03038', fg: '#f5cdd2', bg: '#100407' },
  { id: 'rose',    name: 'Rose Nebula', a: '#ff6b9d', s: '#ffa6c4', d: '#b03a63', fg: '#f5cfe0', bg: '#100610' },
  { id: 'violet',  name: 'Violet Haze', a: '#9d8cff', s: '#c0b5ff', d: '#5b4fb0', fg: '#d8d0f5', bg: '#08060f' },
];
let ACCENT_RGB = '63,227,255';   // read live by the starfield so stars match the theme
function hexRGB(h) { const n = parseInt(h.slice(1), 16); return `${(n >> 16) & 255},${(n >> 8) & 255},${n & 255}`; }
function hslHex(h, s, l) {   // h 0-360, s/l 0-100 → #rrggbb
  s /= 100; l /= 100;
  const f = n => { const k = (n + h / 30) % 12;
    const c = l - s * Math.min(l, 1 - l) * Math.max(-1, Math.min(k - 3, 9 - k, 1));
    return Math.round(255 * c).toString(16).padStart(2, '0'); };
  return '#' + f(0) + f(8) + f(4);
}
/* ---- three independent colour axes -------------------------------------------
   The old model bundled everything into one "theme": UI accent, terminal text and
   terminal background moved together, and the hue slider only reached --cyan* —
   leaving 68 hardcoded cyan values (every border, scrollbar, focus ring, selection)
   behind. Those now derive from --acc via color-mix, so ONE variable drives the whole
   chrome, and the three things people actually want to control move separately:

     BASE     --acc + --cyan family  → all chrome
     UI TEXT  --text/--bright/--dim/--faint
     TERMINAL fg/bg inside xterm, independent of both

   A theme is now just a preset that sets all three at once; each axis can be
   overridden afterwards and the override sticks. */

const ACCENT_DEF = { h: 189, s: 100, l: 62 };     // the stock cyan, as HSL
const UITEXT_DEF = { h: 200, s: 40, l: 90 };      // stock cool-grey UI text

function applyAccent(h, s, l) {
  const H = (h % 360 + 360) % 360, S = Math.max(0, Math.min(100, s)), L = Math.max(0, Math.min(100, l));
  const a = hslHex(H, S, L);
  const soft = hslHex(H, Math.min(100, S), Math.min(93, L + 17));
  const dim = hslHex(H, S * 0.72, Math.max(12, L * 0.6));
  const r = document.documentElement.style;
  r.setProperty('--acc', a);                       // chrome follows this one
  r.setProperty('--cyan', a); r.setProperty('--cyan-soft', soft); r.setProperty('--cyan-dim', dim);
  ACCENT_RGB = hexRGB(a);                          // canvas FX read this live
  // terminals: retint the ACCENT colours only. Their fg/bg is its own axis below, so
  // that changing the base colour can never make terminal text unreadable.
  Object.values(window.ZTERMS || {}).forEach(z => {
    if (z && z.term) z.term.options.theme = Object.assign({}, z.term.options.theme,
      { cursor: a, selectionBackground: `rgba(${hexRGB(a)},.30)`, cyan: a, brightCyan: soft });
  });
}
// back-compat: older saved settings and any caller that only knows about a hue
function applyAccentHue(h) { applyAccent(h, ACCENT_DEF.s, ACCENT_DEF.l); }

function applyUIText(h, s, l) {
  const H = (h % 360 + 360) % 360, S = Math.max(0, Math.min(100, s)), L = Math.max(30, Math.min(100, l));
  const r = document.documentElement.style;
  // one ramp, four stops — keeps the contrast relationships that the UI was designed
  // around instead of letting each level drift independently
  r.setProperty('--text', hslHex(H, S * 0.45, L));
  r.setProperty('--bright', hslHex(H, S * 0.30, Math.min(99, L + 7)));
  r.setProperty('--dim', hslHex(H, S * 0.55, Math.max(28, L - 26)));
  r.setProperty('--faint', hslHex(H, S * 0.60, Math.max(20, L - 43)));
}

function applyTermColors(fg, bg) {
  Object.values(window.ZTERMS || {}).forEach(z => {
    if (!z || !z.term) return;
    const patch = {};
    if (fg) patch.foreground = fg;
    if (bg) { patch.background = bg; patch.cursorAccent = bg; }
    if (Object.keys(patch).length) z.term.options.theme = Object.assign({}, z.term.options.theme, patch);
  });
}

// Resolve all three axes from settings and push them to the DOM/terminals. Called by
// Settings.apply(), so any change to any axis lands through one path.
function applyColors() {
  const s = Settings.load();
  applyAccent(s.accentHue != null ? +s.accentHue : ACCENT_DEF.h,
    s.accentSat != null ? +s.accentSat : ACCENT_DEF.s,
    s.accentLum != null ? +s.accentLum : ACCENT_DEF.l);
  applyUIText(s.textHue != null ? +s.textHue : UITEXT_DEF.h,
    s.textSat != null ? +s.textSat : UITEXT_DEF.s,
    s.textLum != null ? +s.textLum : UITEXT_DEF.l);
  applyTermColors(s.termFg || null, s.termBg || null);
}

const Theme = {
  current() { return localStorage.getItem('zen.theme') || 'zenith'; },
  apply(id) {
    const t = THEMES.find(x => x.id === id) || THEMES[0];
    const r = document.documentElement.style;
    r.setProperty('--cyan', t.a); r.setProperty('--cyan-soft', t.s); r.setProperty('--cyan-dim', t.d);
    r.setProperty('--acc', t.a);
    r.setProperty('--glow', `0 0 24px color-mix(in srgb,var(--acc) 20%,transparent)`);
    ACCENT_RGB = hexRGB(t.a);
    CUR_THEME = t;
    // the theme is primarily for the terminal session text — retint foreground/bg + accent (no reload)
    Object.values(window.ZTERMS || {}).forEach(z => {
      if (z && z.term) z.term.options.theme = Object.assign({}, z.term.options.theme,
        { background: t.bg, foreground: t.fg, cursor: t.a, cursorAccent: t.bg,
          selectionBackground: `rgba(${hexRGB(t.a)},.30)`, cyan: t.a, brightCyan: t.s });
    });
    try { localStorage.setItem('zen.theme', id); } catch (e) { /* quota */ }
  },
};
let CUR_THEME = THEMES[0];

/* ================= visual FX / settings ================= */
const FX_DEFAULTS = {
  stars: true, shootingStars: true, nebula: true, horizon: true,
  scanlines: true, vignette: true, grain: false, glow: true,
  // --- opt-in ambient layer (see initFX). ALL default false: an existing install
  // must render exactly what it rendered before, and each is individually switchable.
  deepNebula: false, solarSystem: false, aurora: false, constellations: false,
  codeRain: false, circuit: false, radar: false,
  fxIntensity: 1,      // master opacity for the ambient layers, 0.3–1.6
  // --- whimsy: something crosses the screen every now and then ---
  flybys: false, flybysBack: true,   // 'flybysBack' is the back-canvas half of the same
  // effect: FX_BACK/FX_FORE each hold a stub and the 'layer' param decides which draws,
  // so flipping layer never needs a second toggle the user has to remember to match.
  flybyCast: { bunny: true, ufo: true, satellite: true, whale: true, comet: true, rocket: true },
  flybyCustom: [],     // user sprites: {id,name,file,src,on}
  fxMedia: [],         // custom GIF/image background layers: {id,name,file,url,on,pos,size,opacity,blend,motion,speed}
  fxp: {},             // per-effect parameters, defaults in FX_SPECS
  // --- CSS overlays (front of everything, no canvas cost) ---
  sweep: false, flicker: false, hud: false, glitch: false,
  glowLevel: 1,        // active-window glow intensity, continuous 0–2 (0 = none, 1 = normal)
  starDensity: 1.5, starBrightness: 1.4, arrangeMode: 'tiled', dockPos: 'bottom',
  arrangeInclude: { columns: false, tiled: false, cascade: true },
  dragMode: 'reflow',     // in-arrangement drag: 'reflow' (push & fill) or 'free' (snap to rejoin)
  persistWindows: true,   // restore each window's exact position/size on reload
  sttMode: 'auto',        // mic speech-to-text source: auto | browser | whisper
  desktopCount: 1,        // virtual desktops (workspaces), 1–6
  deskScheme: 'ctrl',     // desktop digit/send shortcut modifier: 'ctrl' or 'ctrl+alt'
  deskArrowMod: 'meta',   // desktop ←/→ step modifier: 'meta'(⌘) | 'alt'(⌥) | 'shift' | 'ctrl'
  defaultWinSize: { mode: 'preset', preset: 'app' },  // new-window size: preset app|quarter|third|half, or {mode:'capture',fw,fh}
  // --- colour, three independent axes (see applyColors) ---
  accentHue: null, accentSat: null, accentLum: null,   // BASE: all chrome. null = theme's
  textHue: null, textSat: null, textLum: null,         // UI TEXT ramp
  termFg: null, termBg: null,                          // TERMINAL, independent of both
  motionLevel: 'full',    // UI animation level: full | calm (no ambient loops) | off
  cornerRadius: 10,       // window/panel corner radius, px 0–18
  panelAlpha: 0.92,       // glass panel opacity 0.5–1
  panelBlur: 20,          // glass backdrop blur, px 0–40
  uiFont: 'default',      // UI typeface pairing: default | system | orbitron | rajdhani
  density: 'comfortable', // UI whitespace density: comfortable | compact
  clock24: true,          // topbar clock: 24-hour
  clockSec: true,         // topbar clock: show seconds
  uiSounds: false,        // subtle audio blips on actions/toasts
  defaultAgent: 'claude', // primary agent for the launch split-button ('claude'|'codex'|'aider')
  defaultProjectsFolder: '~/claudeProjects', // where "+ New Project" creates folders (under HOME)
};
// interface settings a visual preset must NOT clobber (presets only touch look/FX)
const KEEP_ON_PRESET = ['arrangeMode', 'arrangeInclude', 'dragMode', 'dockPos', 'persistWindows', 'sttMode',
  'desktopCount', 'deskScheme', 'deskArrowMod', 'defaultWinSize', 'uiFont', 'density',
  'accentHue', 'motionLevel', 'cornerRadius', 'panelAlpha', 'panelBlur',
  'clock24', 'clockSec', 'uiSounds', 'defaultAgent', 'defaultProjectsFolder',
  // user content + tuning no preset knows about: dropping fxMedia/flybyCustom would
  // orphan the uploaded files on disk and silently delete every per-layer setting
  'fxMedia', 'flybyCustom', 'flybyCast', 'fxp'];
const FX_PRESETS = {
  cinematic: { stars: true, shootingStars: true, nebula: true, horizon: true, scanlines: true,
    vignette: true, grain: true, glow: true, glowLevel: 1.6, starDensity: 2.2, starBrightness: 1.7 },
  balanced: { ...FX_DEFAULTS },
  minimal: { stars: true, shootingStars: false, nebula: false, horizon: true, scanlines: false,
    vignette: true, grain: false, glow: false, glowLevel: 0.5, starDensity: 0.8, starBrightness: 1.1 },
  off: { stars: false, shootingStars: false, nebula: false, horizon: false, scanlines: false,
    vignette: false, grain: false, glow: false, glowLevel: 0, starDensity: 1, starBrightness: 1 },
};
const Settings = {
  data: null,
  load() {
    if (this.data) return this.data;
    let s = {};
    try { s = JSON.parse(localStorage.getItem('zen.settings')) || {}; } catch (e) { /* corrupt */ }
    this.data = Object.assign({}, FX_DEFAULTS, s);
    return this.data;
  },
  set(k, v) { this.load()[k] = v; this.save(); this.apply(); },
  preset(name) { const cur = this.load(); const keep = {};
    KEEP_ON_PRESET.forEach(k => { keep[k] = cur[k]; });   // preserve interface prefs across a visual preset
    // FX_DEFAULTS as the floor: a preset lists only the effects it has an opinion about,
    // and with nothing underneath it every other key lands UNDEFINED. That reads as off
    // for internal flags like flybysBack which have no toggle to turn them back on.
    this.data = Object.assign({}, FX_DEFAULTS, FX_PRESETS[name] || {}, keep);
    this.save(); this.apply(); },
  save() { try { localStorage.setItem('zen.settings', JSON.stringify(this.data)); } catch (e) { /* quota */ } },
  apply() {
    const s = this.load();
    const disp = (id, on) => { const e = $('#' + id); if (e) e.style.display = on ? 'block' : 'none'; };
    disp('stars', s.stars); disp('nebula', s.nebula); disp('horizon', s.horizon);
    disp('scan', s.scanlines); disp('vig', s.vignette); disp('grain', s.grain);
    document.body.classList.toggle('fx-noglow', !s.glow);
    // CSS overlays — pure class flips, the canvas layers are driven by initFX's loop
    document.body.classList.toggle('fx-sweep', !!s.sweep);
    document.body.classList.toggle('fx-flicker', !!s.flicker);
    document.body.classList.toggle('fx-hud', !!s.hud);
    document.body.classList.toggle('fx-glitch', !!s.glitch);
    const gi = s.glow ? (s.glowLevel != null ? +s.glowLevel : 1) : 0;   // master glow off => no window glow
    document.body.style.setProperty('--glow-i', gi);
    document.body.classList.toggle('glow-on', gi > 0);
    const newDock = s.dockPos || 'bottom';
    if (document.body.dataset.dock !== newDock) {   // dock moved → the canvas origin shifts
      document.body.dataset.dock = newDock;
      requestAnimationFrame(() => WM.clampFree());  // pull free windows back on-screen after insets settle
    }
    Dock.fit();                                   // dock run changed → refit icons
    // shape + glass + motion (Settings → Appearance/Effects)
    const r = document.documentElement.style;
    r.setProperty('--radius', (s.cornerRadius != null ? +s.cornerRadius : 10) + 'px');
    r.setProperty('--blur', (s.panelBlur != null ? +s.panelBlur : 20) + 'px');
    r.setProperty('--panel', `rgba(8,17,26,${s.panelAlpha != null ? +s.panelAlpha : .92})`);
    document.body.dataset.motion = s.motionLevel || 'full';
    applyColors();   // base / UI text / terminal — each axis independent (applyColors)
    applyFont(s.uiFont);   // display/mono typeface
    document.body.dataset.density = s.density || 'comfortable';   // whitespace density
    if (typeof renderFXMedia === 'function') renderFXMedia();   // custom GIF/image layers
    if (typeof restoreDockPin === 'function') restoreDockPin();    // clear pin when leaving autohide
  },
};

// UI typeface pairings (display + mono). Non-default/-system ones lazy-load from Google Fonts.
const UI_FONTS = {
  default: { label: 'Chakra', disp: "'Chakra Petch',sans-serif", mono: "'IBM Plex Mono',monospace", url: null },
  system: { label: 'System', disp: "system-ui,-apple-system,'Segoe UI',sans-serif", mono: "ui-monospace,'SF Mono',Menlo,monospace", url: null },
  orbitron: { label: 'Orbitron', disp: "'Orbitron',sans-serif", mono: "'Space Mono',monospace", url: 'family=Orbitron:wght@400;500;600;700&family=Space+Mono:wght@400;700' },
  rajdhani: { label: 'Rajdhani', disp: "'Rajdhani',sans-serif", mono: "'JetBrains Mono',monospace", url: 'family=Rajdhani:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600' },
};
function applyFont(key) {
  const f = UI_FONTS[key] || UI_FONTS.default;
  if (f.url) {   // lazy-load the family once
    const id = 'uifont-' + key;
    if (!document.getElementById(id)) {
      const l = document.createElement('link'); l.id = id; l.rel = 'stylesheet';
      l.href = 'https://fonts.googleapis.com/css2?' + f.url + '&display=swap';
      document.head.appendChild(l);
    }
  }
  const r = document.documentElement.style;
  r.setProperty('--disp', f.disp); r.setProperty('--mono', f.mono);
}

function initStars() {
  const c = $('#stars'), ctx = c.getContext('2d');
  let stars = [], shooters = [], lastDensity = null;
  const build = () => {
    const dens = Settings.load().starDensity || 1;
    const n = Math.floor(innerWidth / 6 * dens);
    stars = Array.from({ length: n }, () => ({
      x: Math.random() * c.width, y: Math.random() * c.height,
      z: Math.pow(Math.random(), 1.7) * 0.95 + 0.05,   // skewed: many faint, few bright
      tw: Math.random() * Math.PI * 2,
      hue: Math.random() < 0.34 ? 'c' : (Math.random() < 0.5 ? 'v' : 'w') }));
    lastDensity = dens;
  };
  const resize = () => { c.width = innerWidth; c.height = innerHeight; build(); };
  resize();
  addEventListener('resize', resize);
  (function frame(t) {
    const s = Settings.load();
    if ((s.starDensity || 1) !== lastDensity) build();
    ctx.clearRect(0, 0, c.width, c.height);
    const COL = { c: ACCENT_RGB, v: '175,150,255', w: '212,230,242' };   // 'c' tracks the theme
    const still = s.motionLevel === 'off';   // Settings → Effects → Motion OFF freezes drift/twinkle
    if (s.stars) {
      const bright = s.starBrightness || 1;
      for (const st of stars) {
        if (!still) st.x -= st.z * 0.10;
        if (st.x < 0) { st.x = c.width; st.y = Math.random() * c.height; }
        let a = (0.20 + 0.62 * st.z * (0.55 + 0.45 * Math.sin((still ? 0 : t) / 850 + st.tw))) * bright;
        a = Math.min(1, a);
        const col = COL[st.hue];
        if (s.glow && st.z > 0.8) { ctx.shadowBlur = 6; ctx.shadowColor = `rgba(${col},${a})`; }
        else ctx.shadowBlur = 0;
        const size = st.z > 0.82 ? 2.1 : st.z > 0.5 ? 1.4 : 0.9;
        ctx.fillStyle = `rgba(${col},${a.toFixed(3)})`;
        ctx.beginPath(); ctx.arc(st.x, st.y, size, 0, 6.283); ctx.fill();
      }
      ctx.shadowBlur = 0;
    }
    if (s.shootingStars && !still) {
      if (shooters.length < 2 && Math.random() < 0.005) {
        shooters.push({ x: Math.random() * c.width, y: Math.random() * c.height * 0.55,
          vx: -(5 + Math.random() * 5), vy: 2 + Math.random() * 2.5, life: 1 });
      }
      for (const sh of shooters) {
        sh.x += sh.vx; sh.y += sh.vy; sh.life -= 0.018;
        const g = ctx.createLinearGradient(sh.x, sh.y, sh.x - sh.vx * 9, sh.y - sh.vy * 9);
        g.addColorStop(0, `rgba(150,240,255,${Math.max(0, sh.life)})`);
        g.addColorStop(1, 'rgba(150,240,255,0)');
        ctx.strokeStyle = g; ctx.lineWidth = 2; ctx.lineCap = 'round';
        ctx.beginPath(); ctx.moveTo(sh.x, sh.y); ctx.lineTo(sh.x - sh.vx * 9, sh.y - sh.vy * 9); ctx.stroke();
      }
      shooters = shooters.filter(sh => sh.life > 0 && sh.x > -80);
    } else if (shooters.length) shooters = [];
    requestAnimationFrame(frame);
  })(0);
}

/* ================= clock ================= */
function startClock() {
  const tick = () => {
    const d = new Date(), s = Settings.load();
    const opts = { hour12: s.clock24 === false, hour: '2-digit', minute: '2-digit' };
    if (s.clockSec !== false) opts.second = '2-digit';
    $('#clock').innerHTML = d.toLocaleTimeString('en-US', opts) +   // trusted: locale time string only
      `<small>${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}-${String(d.getFullYear()).slice(2)}</small>`;
  };
  tick(); setInterval(tick, 1000);
}

/* ================= ambient FX engine =================
   A second pair of canvases (one behind the windows, one in front) hosting the
   opt-in effects. Kept out of initStars() on purpose: the starfield is the always-on
   baseline and must not pay for anything here.

   Everything defaults OFF, one rAF loop drives both canvases, and the loop returns
   immediately when nothing is enabled — an untouched install renders exactly what it
   rendered before. Also honours Settings → Effects → Motion (calm freezes drift,
   off skips the layer entirely) and stops while the tab is hidden.

   Backdrop layers are drawn at devicePixelRatio 1 regardless of screen: they are soft,
   blurred, low-alpha washes where the extra samples buy nothing and cost 4× the fill
   on a retina display. */

// ---- per-effect parameters -------------------------------------------------
// One schema drives three things: the stored default, the control the settings pane
// renders, and the value the draw function reads. Adding a knob is one line here.
//   num  {k,label,min,max,step,def,fmt}      opt {k,label,opts:[[val,label]],def}
const X1 = v => v.toFixed(1) + '×', X2 = v => v.toFixed(2) + '×', PCT = v => Math.round(v * 100) + '%';
const FX_SPECS = {
  deepNebula: [
    { k: 'clouds', label: 'Clouds', min: 2, max: 12, step: 1, def: 5, fmt: v => v },
    { k: 'size', label: 'Size', min: 0.4, max: 2.2, step: 0.05, def: 1, fmt: X2 },
    { k: 'drift', label: 'Drift', min: 0, max: 3, step: 0.1, def: 1, fmt: X1 },
    { k: 'opacity', label: 'Opacity', min: 0.2, max: 2, step: 0.05, def: 1, fmt: X2 },
    { k: 'palette', label: 'Palette', def: 'deep',
      opts: [['deep', 'Deep space'], ['accent', 'Accent'], ['ember', 'Ember'], ['ice', 'Ice']] },
  ],
  solarSystem: [
    { k: 'planets', label: 'Planets', min: 2, max: 8, step: 1, def: 7, fmt: v => v },
    { k: 'size', label: 'Scale', min: 0.4, max: 2, step: 0.05, def: 1, fmt: X2 },
    { k: 'speed', label: 'Orbit speed', min: 0.1, max: 4, step: 0.1, def: 1, fmt: X1 },
    { k: 'opacity', label: 'Opacity', min: 0.2, max: 2, step: 0.05, def: 1, fmt: X2 },
    { k: 'orbits', label: 'Orbit rings', def: 1, opts: [[1, 'Show'], [0, 'Hide']] },
    { k: 'moons', label: 'Moons', def: 1, opts: [[1, 'Show'], [0, 'Hide']] },
    { k: 'pos', label: 'Position', def: 'br',
      opts: [['br', 'Bottom right'], ['bl', 'Bottom left'], ['tr', 'Top right'], ['c', 'Centre']] },
  ],
  aurora: [
    { k: 'curtains', label: 'Curtains', min: 1, max: 6, step: 1, def: 3, fmt: v => v },
    { k: 'height', label: 'Height', min: 0.15, max: 0.95, step: 0.05, def: 0.55, fmt: PCT },
    { k: 'top', label: 'Base line', min: 0.1, max: 0.8, step: 0.02, def: 0.34, fmt: PCT },
    { k: 'speed', label: 'Speed', min: 0.1, max: 3, step: 0.1, def: 1, fmt: X1 },
    { k: 'rays', label: 'Ray detail', min: 3, max: 20, step: 1, def: 9, fmt: v => v },
    { k: 'brightness', label: 'Brightness', min: 0.2, max: 2.5, step: 0.05, def: 1, fmt: X2 },
    { k: 'palette', label: 'Palette', def: 'classic',
      opts: [['classic', 'Classic green'], ['solar', 'Solar storm'], ['arctic', 'Arctic'], ['accent', 'Accent']] },
  ],
  constellations: [
    { k: 'nodes', label: 'Stars', min: 15, max: 160, step: 5, def: 60, fmt: v => v },
    { k: 'link', label: 'Link range', min: 60, max: 320, step: 10, def: 170, fmt: v => v + 'px' },
    { k: 'speed', label: 'Drift', min: 0, max: 3, step: 0.1, def: 1, fmt: X1 },
    { k: 'opacity', label: 'Opacity', min: 0.2, max: 2, step: 0.05, def: 1, fmt: X2 },
  ],
  codeRain: [
    { k: 'density', label: 'Columns', min: 0.3, max: 2.5, step: 0.1, def: 1, fmt: X1 },
    { k: 'speed', label: 'Fall speed', min: 0.2, max: 3, step: 0.1, def: 1, fmt: X1 },
    { k: 'length', label: 'Trail length', min: 0.4, max: 2.5, step: 0.1, def: 1, fmt: X1 },
    { k: 'opacity', label: 'Opacity', min: 0.2, max: 2, step: 0.05, def: 1, fmt: X2 },
    { k: 'glyphs', label: 'Glyphs', def: 'kana',
      opts: [['kana', 'Katakana'], ['hex', 'Hex'], ['bin', 'Binary'], ['code', 'Code symbols']] },
  ],
  circuit: [
    { k: 'traces', label: 'Traces', min: 4, max: 44, step: 2, def: 16, fmt: v => v },
    { k: 'speed', label: 'Pulse speed', min: 0.2, max: 3, step: 0.1, def: 1, fmt: X1 },
    { k: 'opacity', label: 'Opacity', min: 0.2, max: 2, step: 0.05, def: 1, fmt: X2 },
  ],
  radar: [
    { k: 'size', label: 'Size', min: 0.15, max: 0.9, step: 0.05, def: 0.34, fmt: PCT },
    { k: 'speed', label: 'Sweep speed', min: 0.2, max: 3, step: 0.1, def: 1, fmt: X1 },
    { k: 'contacts', label: 'Contacts', min: 0, max: 30, step: 1, def: 7, fmt: v => v },
    { k: 'opacity', label: 'Opacity', min: 0.2, max: 2, step: 0.05, def: 1, fmt: X2 },
    { k: 'pos', label: 'Position', def: 'bl',
      opts: [['bl', 'Bottom left'], ['br', 'Bottom right'], ['tl', 'Top left'], ['c', 'Centre']] },
  ],
  flybys: [
    { k: 'rate', label: 'How often', min: 0.25, max: 6, step: 0.25, def: 1, fmt: X1 },
    { k: 'scale', label: 'Size', min: 0.4, max: 3, step: 0.1, def: 1, fmt: X2 },
    { k: 'speed', label: 'Speed', min: 0.2, max: 3, step: 0.1, def: 1, fmt: X1 },
    { k: 'opacity', label: 'Opacity', min: 0.2, max: 1, step: 0.05, def: 1, fmt: X2 },
    { k: 'layer', label: 'Layer', def: 'fore',
      opts: [['fore', 'In front of windows'], ['back', 'Behind windows']] },
    { k: 'maxOn', label: 'At once', min: 1, max: 6, step: 1, def: 3, fmt: v => v },
  ],
};
// resolved params for one effect: spec defaults with the user's overrides on top
function fxp(key) {
  const saved = (Settings.load().fxp || {})[key] || {};
  const out = {};
  for (const p of FX_SPECS[key] || []) out[p.k] = saved[p.k] !== undefined ? saved[p.k] : p.def;
  return out;
}

// cheap 1-D value noise + fbm. Used for aurora ray structure and nebula shape —
// Math.random() per frame would boil, and a real Perlin is more than this needs.
function _h1(n) { const s = Math.sin(n * 127.1) * 43758.5453; return s - Math.floor(s); }
function _vn(x) {
  const i = Math.floor(x), f = x - i, u = f * f * (3 - 2 * f);
  return _h1(i) * (1 - u) + _h1(i + 1) * u;
}
function _fbm(x) { return _vn(x) * 0.55 + _vn(x * 2.13) * 0.26 + _vn(x * 4.37) * 0.13 + _vn(x * 8.9) * 0.06; }

// key -> draw(ctx, w, h, t, alpha) for the layer BEHIND the windows
const FX_BACK = {};
// key -> draw(...) for the layer IN FRONT. Kept separate so a flyby can pass over a
// window while a nebula never does.
const FX_FORE = {};

function _fxRGB(mix) {          // accent, optionally pulled toward white
  const [r, g, b] = ACCENT_RGB.split(',').map(Number);
  const m = mix || 0;
  return `${Math.round(r + (255 - r) * m)},${Math.round(g + (255 - g) * m)},${Math.round(b + (255 - b) * m)}`;
}

/* --- distant nebulae -----------------------------------------------------------
   Each cloud is THREE offset radial gradients rather than one circle: a single
   gradient always reads as a perfect disc, and real nebulae are lumpy. A darker
   fourth lobe carves a dust lane so the shape has some internal structure. */
const NEB_PAL = {
  deep: ['150,110,255', '255,110,170', '90,180,255', '120,90,230', '200,120,255'],
  ember: ['255,140,80', '255,90,90', '255,190,110', '230,80,140', '255,120,60'],
  ice: ['120,200,255', '150,240,255', '90,160,255', '190,220,255', '110,255,235'],
};
let _nebSeeds = null, _nebN = null;
FX_BACK.deepNebula = (x, w, h, t, a) => {
  const P = fxp('deepNebula'), n = P.clouds;
  if (!_nebSeeds || _nebN !== n) {
    _nebSeeds = Array.from({ length: n }, (_, i) => ({
      x: Math.random(), y: Math.random() * 0.85, i,
      r: 0.20 + Math.random() * 0.26, sp: 0.005 + Math.random() * 0.012,
      ph: Math.random() * 6.283,
      lobes: Array.from({ length: 3 }, () => ({
        dx: (Math.random() - 0.5) * 0.7, dy: (Math.random() - 0.5) * 0.7,
        s: 0.45 + Math.random() * 0.55 })),
    }));
    _nebN = n;
  }
  const pal = P.palette === 'accent' ? null : (NEB_PAL[P.palette] || NEB_PAL.deep);
  const A = 0.16 * a * P.opacity;
  x.save(); x.globalCompositeOperation = 'lighter';
  for (const s of _nebSeeds) {
    const col = pal ? pal[s.i % pal.length] : _fxRGB(0.1 + (s.i % 3) * 0.12);
    const cx = (s.x + Math.sin(t * s.sp * P.drift + s.ph) * 0.06) * w;
    const cy = (s.y + Math.cos(t * s.sp * 0.7 * P.drift + s.ph) * 0.04) * h;
    const R = s.r * Math.min(w, h) * P.size;
    for (const L of s.lobes) {
      const lx = cx + L.dx * R * 0.55, ly = cy + L.dy * R * 0.55, lr = R * L.s;
      const g = x.createRadialGradient(lx, ly, 0, lx, ly, lr);
      g.addColorStop(0, `rgba(${col},${A})`);
      g.addColorStop(0.4, `rgba(${col},${A * 0.42})`);
      g.addColorStop(1, `rgba(${col},0)`);
      x.fillStyle = g; x.beginPath(); x.arc(lx, ly, lr, 0, 7); x.fill();
    }
  }
  x.restore();
  // dust lane: a soft dark wedge subtracted from the brightest cloud
  const s0 = _nebSeeds[0];
  if (s0) {
    const cx = s0.x * w, cy = s0.y * h, R = s0.r * Math.min(w, h) * P.size;
    const g = x.createLinearGradient(cx - R, cy, cx + R, cy + R * 0.4);
    g.addColorStop(0, 'rgba(2,6,10,0)');
    g.addColorStop(0.5, `rgba(2,6,10,${0.30 * a})`);
    g.addColorStop(1, 'rgba(2,6,10,0)');
    x.fillStyle = g; x.beginPath(); x.ellipse(cx, cy, R, R * 0.32, 0.5, 0, 7); x.fill();
  }
};

/* --- aurora ---------------------------------------------------------------------
   Actual northern lights, not sine ribbons. The physics that matters visually:
   emission is FIELD-ALIGNED, so the light forms vertical rays standing on a
   serpentine footprint — the curtain seen edge-on. Colour is altitude-banded:
   557.7nm oxygen green low down, 630nm red/magenta far above, a violet-blue hem at
   the very bottom. The bottom edge is sharp, the top diffuses to nothing.

   So: one vertical gradient per curtain (built once — it is the same ramp for every
   column), then columns drawn as alpha-varied rects whose alpha comes from fbm.
   That fbm is what produces the ray structure; drawing a gradient per column instead
   would be ~640 gradients a frame and look no better. 'lighter' compositing makes
   overlapping rays bloom the way real ones do. */
const AURORA_PAL = {                    // [hem, low, mid, high]
  classic: ['80,120,255', '60,255,140', '90,255,190', '255,70,180'],
  solar: ['255,60,90', '255,180,60', '255,120,70', '255,60,140'],
  arctic: ['120,160,255', '120,255,235', '150,220,255', '190,140,255'],
  accent: null,
};
FX_BACK.aurora = (x, w, h, t, a) => {
  const P = fxp('aurora');
  let pal = AURORA_PAL[P.palette];
  if (!pal) { const c = _fxRGB(0); pal = [_fxRGB(0.45), c, _fxRGB(0.2), _fxRGB(0.6)]; }
  const bri = P.brightness * a, step = 4;
  x.save();
  x.globalCompositeOperation = 'lighter';
  for (let c = 0; c < P.curtains; c++) {
    const depth = 1 - c / (P.curtains + 0.6);          // nearer curtain = taller, brighter
    const yBase = h * (P.top + c * 0.05);
    const H = h * P.height * (0.55 + 0.45 * depth);
    const ph = c * 2.7 + 1.3;
    // one ramp per curtain; per-column bend shifts it by <4% of h, which is invisible
    // against a gradient this soft and buys ~640 fewer gradient allocations a frame.
    const g = x.createLinearGradient(0, yBase, 0, yBase - H);
    g.addColorStop(0.00, `rgba(${pal[0]},0)`);
    g.addColorStop(0.04, `rgba(${pal[0]},${0.30 * bri})`);   // violet hem, sharp bottom
    g.addColorStop(0.13, `rgba(${pal[1]},${0.50 * bri})`);   // brightest green band
    g.addColorStop(0.38, `rgba(${pal[2]},${0.26 * bri})`);
    g.addColorStop(0.72, `rgba(${pal[3]},${0.10 * bri})`);   // red/magenta crown
    g.addColorStop(1.00, `rgba(${pal[3]},0)`);
    x.fillStyle = g;
    for (let px = 0; px <= w; px += step) {
      const u = px / w;
      // serpentine footprint — two folds at different scales, slowly evolving
      const bend = Math.sin(u * 3.1 + t * 0.16 * P.speed + ph) * h * 0.030
        + Math.sin(u * 7.7 - t * 0.10 * P.speed + ph * 1.7) * h * 0.014;
      // ray intensity. pow() sharpens the fbm into discrete rays instead of a wash.
      let n = _fbm(u * P.rays + t * 0.22 * P.speed + ph * 5);
      n = Math.pow(Math.max(0, n * 1.7 - 0.25), 1.7);
      if (n < 0.015) continue;
      x.globalAlpha = Math.min(1, n) * depth;
      x.fillRect(px, yBase + bend - H, step + 1, H);
    }
    x.globalAlpha = 1;
  }
  x.restore();
};

/* --- solar system ---------------------------------------------------------------
   Orbits are elliptical with real eccentricity and the sun sits at a focus, not the
   centre. Planets are shaded: a lit crescent facing the sun and a dark terminator,
   which is what stops them reading as flat dots. */
const _PLANETS = [
  { d: 0.10, r: 1.9, sp: 1.00, e: 0.14, c: '190,180,170' },
  { d: 0.15, r: 3.1, sp: 0.62, e: 0.05, c: '235,180,120' },
  { d: 0.21, r: 3.4, sp: 0.44, e: 0.03, c: '110,185,255', moon: 1 },
  { d: 0.27, r: 2.5, sp: 0.34, e: 0.11, c: '255,120,90', moon: 1 },
  { d: 0.37, r: 6.4, sp: 0.19, e: 0.06, c: '235,205,150', moon: 2 },
  { d: 0.47, r: 5.5, sp: 0.13, e: 0.08, c: '225,210,165', ring: true },
  { d: 0.57, r: 4.0, sp: 0.09, e: 0.05, c: '150,225,240' },
];
FX_BACK.solarSystem = (x, w, h, t, a) => {
  const P = fxp('solarSystem');
  const POS = { br: [0.80, 0.72], bl: [0.20, 0.72], tr: [0.80, 0.30], c: [0.50, 0.50] }[P.pos] || [0.80, 0.72];
  const cx = w * POS[0], cy = h * POS[1], R = Math.min(w, h) * 0.62 * P.size;
  const A = a * P.opacity, tilt = 0.42, sun = _fxRGB(0.35);
  x.save(); x.globalCompositeOperation = 'lighter';
  const g = x.createRadialGradient(cx, cy, 0, cx, cy, R * 0.16);
  g.addColorStop(0, `rgba(${sun},${0.60 * A})`);
  g.addColorStop(0.35, `rgba(${sun},${0.12 * A})`);
  g.addColorStop(1, `rgba(${sun},0)`);
  x.fillStyle = g; x.beginPath(); x.arc(cx, cy, R * 0.16, 0, 7); x.fill();
  x.restore();
  x.fillStyle = `rgba(${_fxRGB(0.6)},${0.85 * A})`;
  x.beginPath(); x.arc(cx, cy, 3.6 * P.size, 0, 7); x.fill();
  for (let i = 0; i < Math.min(P.planets, _PLANETS.length); i++) {
    const p = _PLANETS[i];
    const rx = R * p.d, ry = R * p.d * tilt, fx = rx * p.e;   // sun at a focus
    if (P.orbits) {
      x.strokeStyle = `rgba(${_fxRGB(0)},${0.10 * A})`; x.lineWidth = 1;
      x.beginPath(); x.ellipse(cx - fx, cy, rx, ry, 0, 0, 7); x.stroke();
    }
    const ang = t * p.sp * 0.28 * P.speed + p.d * 19;
    const px = cx - fx + Math.cos(ang) * rx, py = cy + Math.sin(ang) * ry;
    const pr = p.r * P.size;
    if (p.ring) {
      x.strokeStyle = `rgba(${p.c},${0.45 * A})`; x.lineWidth = 1.5 * P.size;
      x.beginPath(); x.ellipse(px, py, pr * 2.2, pr * 0.72, -0.5, 0, 7); x.stroke();
    }
    // lit side faces the sun; the far limb falls into shadow
    const toSun = Math.atan2(cy - py, cx - px);
    const pg = x.createRadialGradient(px + Math.cos(toSun) * pr * 0.5, py + Math.sin(toSun) * pr * 0.5,
      pr * 0.1, px, py, pr);
    pg.addColorStop(0, `rgba(${p.c},${0.95 * A})`);
    pg.addColorStop(0.6, `rgba(${p.c},${0.55 * A})`);
    pg.addColorStop(1, `rgba(${p.c},${0.10 * A})`);
    x.fillStyle = pg; x.beginPath(); x.arc(px, py, pr, 0, 7); x.fill();
    if (P.moons && p.moon) {
      for (let m = 0; m < p.moon; m++) {
        const ma = t * (1.6 + m * 0.7) * P.speed + m * 2.1 + i;
        const md = pr * (2.4 + m * 1.1);
        x.fillStyle = `rgba(220,225,235,${0.7 * A})`;
        x.beginPath(); x.arc(px + Math.cos(ma) * md, py + Math.sin(ma) * md * tilt, 1.1 * P.size, 0, 7); x.fill();
      }
    }
  }
};

/* --- constellations: drifting nodes that wire up when they get close --- */
let _cNodes = null, _cN = null;
FX_BACK.constellations = (x, w, h, t, a) => {
  const P = fxp('constellations');
  if (!_cNodes || _cNodes._w !== w || _cN !== P.nodes) {
    _cNodes = Array.from({ length: P.nodes }, () => ({
      x: Math.random() * w, y: Math.random() * h, m: 0.5 + Math.random() * 1.4,
      tw: Math.random() * 6.283,
      vx: (Math.random() - 0.5) * 0.16, vy: (Math.random() - 0.5) * 0.16 }));
    _cNodes._w = w; _cN = P.nodes;
  }
  const rgb = _fxRGB(0.1), LINK = P.link, A = a * P.opacity;
  for (const p of _cNodes) {
    p.x += p.vx * P.speed; p.y += p.vy * P.speed;
    if (p.x < 0 || p.x > w) p.vx *= -1;
    if (p.y < 0 || p.y > h) p.vy *= -1;
  }
  x.lineWidth = 1;
  for (let i = 0; i < _cNodes.length; i++) {
    const p = _cNodes[i];
    for (let j = i + 1; j < _cNodes.length; j++) {
      const q = _cNodes[j], d = Math.hypot(p.x - q.x, p.y - q.y);
      if (d > LINK) continue;
      x.strokeStyle = `rgba(${rgb},${Math.pow(1 - d / LINK, 1.6) * 0.24 * A})`;
      x.beginPath(); x.moveTo(p.x, p.y); x.lineTo(q.x, q.y); x.stroke();
    }
    const tw = 0.72 + 0.28 * Math.sin(t * 1.7 + p.tw);
    x.fillStyle = `rgba(${rgb},${0.55 * A * tw})`;
    x.beginPath(); x.arc(p.x, p.y, p.m, 0, 7); x.fill();
  }
};

/* --- code rain --- */
const RAIN_SETS = {
  kana: 'アイウエオカキクケコサシスセソタチツテト',
  hex: '0123456789ABCDEF',
  bin: '01',
  code: '<>/\\[]{}()=+*&^%$#@!|~;:',
};
let _rain = null, _rainD = null;
FX_BACK.codeRain = (x, w, h, t, a) => {
  const P = fxp('codeRain'), step = Math.round(15 / P.density);
  const G = RAIN_SETS[P.glyphs] || RAIN_SETS.kana;
  if (!_rain || _rain._w !== w || _rainD !== P.density) {
    _rain = Array.from({ length: Math.ceil(w / step) }, () => ({
      y: Math.random() * h, sp: 26 + Math.random() * 64,
      len: Math.round((6 + Math.random() * 14) * P.length) }));
    _rain._w = w; _rain._last = t; _rainD = P.density;
  }
  const dt = Math.min(0.05, t - (_rain._last || t)); _rain._last = t;
  const rgb = _fxRGB(0.05), A = a * P.opacity;
  x.font = '12px "IBM Plex Mono",monospace'; x.textBaseline = 'top';
  _rain.forEach((c, i) => {
    c.y += c.sp * dt * P.speed;
    if (c.y - c.len * 14 > h) { c.y = -Math.random() * 120; c.sp = 26 + Math.random() * 64; }
    for (let k = 0; k < c.len; k++) {
      const yy = c.y - k * 14;
      if (yy < -14 || yy > h) continue;
      x.fillStyle = k === 0 ? `rgba(${_fxRGB(0.75)},${0.9 * A})`
        : `rgba(${rgb},${Math.pow(1 - k / c.len, 1.4) * 0.34 * A})`;
      x.fillText(G[(i * 7 + k * 13 + (t * 6 | 0)) % G.length], i * step, yy);
    }
  });
};

/* --- circuit: Manhattan routing with pulses and vias --- */
let _traces = null, _traceN = null;
FX_BACK.circuit = (x, w, h, t, a) => {
  const P = fxp('circuit');
  if (!_traces || _traces._w !== w || _traceN !== P.traces) {
    _traces = Array.from({ length: P.traces }, () => {
      const horiz = Math.random() < 0.5, pts = [], steps = 3 + (Math.random() * 3 | 0);
      let px = Math.random() * w, py = Math.random() * h;
      pts.push([px, py]);
      for (let i = 0; i < steps; i++) {
        if ((i % 2 === 0) === horiz) px += (Math.random() - 0.5) * w * 0.4;
        else py += (Math.random() - 0.5) * h * 0.4;
        pts.push([px, py]);
      }
      return { pts, off: Math.random(), sp: 0.05 + Math.random() * 0.13 };
    });
    _traces._w = w; _traceN = P.traces;
  }
  const rgb = _fxRGB(0.05), A = a * P.opacity;
  for (const tr of _traces) {
    x.strokeStyle = `rgba(${rgb},${0.13 * A})`; x.lineWidth = 1;
    x.beginPath(); x.moveTo(tr.pts[0][0], tr.pts[0][1]);
    for (let i = 1; i < tr.pts.length; i++) x.lineTo(tr.pts[i][0], tr.pts[i][1]);
    x.stroke();
    const f = (tr.off + t * tr.sp * P.speed) % 1, seg = f * (tr.pts.length - 1);
    const i0 = Math.floor(seg), k = seg - i0;
    const p0 = tr.pts[i0], p1 = tr.pts[Math.min(i0 + 1, tr.pts.length - 1)];
    const px = p0[0] + (p1[0] - p0[0]) * k, py = p0[1] + (p1[1] - p0[1]) * k;
    const g = x.createRadialGradient(px, py, 0, px, py, 10);
    g.addColorStop(0, `rgba(${_fxRGB(0.55)},${0.8 * A})`);
    g.addColorStop(1, `rgba(${rgb},0)`);
    x.fillStyle = g; x.beginPath(); x.arc(px, py, 10, 0, 7); x.fill();
    for (const p of tr.pts) {
      x.strokeStyle = `rgba(${rgb},${0.30 * A})`; x.lineWidth = 1;
      x.beginPath(); x.arc(p[0], p[1], 2.2, 0, 7); x.stroke();      // via, not a blob
    }
  }
};

/* --- radar --- */
let _blips = null, _blipN = null;
FX_BACK.radar = (x, w, h, t, a) => {
  const P = fxp('radar');
  const POS = { bl: [0.14, 0.80], br: [0.86, 0.80], tl: [0.14, 0.28], c: [0.5, 0.5] }[P.pos] || [0.14, 0.80];
  const cx = w * POS[0], cy = h * POS[1], R = Math.min(w, h) * P.size;
  const rgb = _fxRGB(0.05), A = a * P.opacity;
  x.strokeStyle = `rgba(${rgb},${0.13 * A})`; x.lineWidth = 1;
  for (let i = 1; i <= 3; i++) { x.beginPath(); x.arc(cx, cy, R * i / 3, 0, 7); x.stroke(); }
  for (let i = 0; i < 4; i++) {                                    // bearing ticks
    const ang = i * Math.PI / 2;
    x.beginPath(); x.moveTo(cx + Math.cos(ang) * R * 0.94, cy + Math.sin(ang) * R * 0.94);
    x.lineTo(cx + Math.cos(ang) * R, cy + Math.sin(ang) * R); x.stroke();
  }
  const ang = t * 0.7 * P.speed % 6.283;
  if (x.createConicGradient) {
    const g = x.createConicGradient(ang, cx, cy);
    g.addColorStop(0, `rgba(${rgb},${0.32 * A})`);
    g.addColorStop(0.11, `rgba(${rgb},0)`);
    g.addColorStop(1, `rgba(${rgb},0)`);
    x.fillStyle = g; x.beginPath(); x.arc(cx, cy, R, 0, 7); x.fill();
  }
  x.strokeStyle = `rgba(${_fxRGB(0.45)},${0.5 * A})`;
  x.beginPath(); x.moveTo(cx, cy); x.lineTo(cx + Math.cos(ang) * R, cy + Math.sin(ang) * R); x.stroke();
  if (!_blips || _blipN !== P.contacts) {
    _blips = Array.from({ length: P.contacts }, () => ({ a: Math.random() * 6.283, d: 0.15 + Math.random() * 0.8 }));
    _blipN = P.contacts;
  }
  for (const b of _blips) {
    const da = (ang - b.a + 6.283) % 6.283;         // time since the beam passed
    const f = Math.max(0, 1 - da / 2.4);
    if (f <= 0) continue;
    const px = cx + Math.cos(b.a) * R * b.d, py = cy + Math.sin(b.a) * R * b.d;
    x.fillStyle = `rgba(${_fxRGB(0.5)},${f * 0.85 * A})`;
    x.beginPath(); x.arc(px, py, 2.4, 0, 7); x.fill();
    x.strokeStyle = `rgba(${_fxRGB(0.5)},${f * 0.30 * A})`;
    x.beginPath(); x.arc(px, py, 2.4 + (1 - f) * 9, 0, 7); x.stroke();
  }
};

/* --- flybys ---------------------------------------------------------------------
   Vector-drawn cast plus any images the user adds (Settings → Effects → Flybys).
   Custom images are downscaled to <=128px before they are stored, because these live
   in localStorage next to every other setting and a full-size PNG would blow the
   ~5MB quota and take the whole settings object down with it. */
const FLYBY_CAST = {
  bunny: (x) => {
    x.fillStyle = '#e9eef5'; x.lineWidth = 1.2;
    x.beginPath(); x.ellipse(0, 0, 13, 10, 0, 0, 7); x.fill();
    x.beginPath(); x.ellipse(9, -12, 2, 6, -0.35, 0, 7); x.fill();
    x.beginPath(); x.ellipse(13, -12.5, 2, 6.5, 0.15, 0, 7); x.fill();
    x.beginPath(); x.ellipse(-13, 2, 4, 5, 0, 0, 7); x.fill();
    x.beginPath(); x.ellipse(-6, 8, 4.5, 3, 0.4, 0, 7); x.fill();
    x.beginPath(); x.arc(11, -5, 7.5, 0, 7); x.fill();
    const g = x.createRadialGradient(9, -7, 1, 11, -5, 6.5);
    g.addColorStop(0, 'rgba(190,245,255,.85)'); g.addColorStop(1, 'rgba(60,150,200,.55)');
    x.fillStyle = g; x.beginPath(); x.arc(11, -5, 6.2, 0, 7); x.fill();
    x.strokeStyle = 'rgba(255,255,255,.65)';
    x.beginPath(); x.arc(11, -5, 7.5, 0, 7); x.stroke();
  },
  ufo: (x) => {
    const g0 = x.createLinearGradient(0, 4, 0, 34);
    g0.addColorStop(0, 'rgba(120,220,255,.38)'); g0.addColorStop(1, 'rgba(120,220,255,0)');
    x.fillStyle = g0;
    x.beginPath(); x.moveTo(-6, 4); x.lineTo(6, 4); x.lineTo(15, 34); x.lineTo(-15, 34); x.fill();
    const dg = x.createLinearGradient(0, -12, 0, 2);
    dg.addColorStop(0, 'rgba(190,245,255,.9)'); dg.addColorStop(1, 'rgba(90,160,200,.7)');
    x.fillStyle = dg; x.beginPath(); x.ellipse(0, -5, 9, 6.5, 0, Math.PI, 0); x.fill();
    const bg = x.createLinearGradient(0, -3, 0, 5);
    bg.addColorStop(0, 'rgba(215,225,240,.98)'); bg.addColorStop(1, 'rgba(120,135,155,.95)');
    x.fillStyle = bg; x.beginPath(); x.ellipse(0, 0, 18, 5, 0, 0, 7); x.fill();
    for (let i = -2; i <= 2; i++) {
      x.fillStyle = `rgba(255,220,120,${0.45 + 0.55 * Math.abs(Math.sin(Date.now() / 260 + i))})`;
      x.beginPath(); x.arc(i * 6, 2.4, 1.5, 0, 7); x.fill();
    }
  },
  satellite: (x) => {
    x.fillStyle = 'rgba(205,215,230,.96)'; x.fillRect(-5, -4, 10, 8);
    for (const sx of [-20, 7]) {
      x.fillStyle = 'rgba(70,120,205,.92)'; x.fillRect(sx, -3.5, 13, 7);
      x.strokeStyle = 'rgba(150,190,240,.5)'; x.lineWidth = 0.6;
      for (let i = 1; i < 4; i++) { x.beginPath(); x.moveTo(sx + i * 3.2, -3.5); x.lineTo(sx + i * 3.2, 3.5); x.stroke(); }
    }
    x.strokeStyle = 'rgba(200,210,225,.85)'; x.lineWidth = 1.2;
    x.beginPath(); x.moveTo(-7, 0); x.lineTo(-5, 0); x.moveTo(5, 0); x.lineTo(7, 0); x.stroke();
    x.beginPath(); x.moveTo(0, -4); x.lineTo(0, -9); x.stroke();
    x.fillStyle = `rgba(255,90,90,${0.35 + 0.65 * Math.abs(Math.sin(Date.now() / 380))})`;
    x.beginPath(); x.arc(0, -10, 1.7, 0, 7); x.fill();
  },
  whale: (x) => {
    const g = x.createLinearGradient(0, -11, 0, 11);
    g.addColorStop(0, 'rgba(140,175,255,.92)'); g.addColorStop(0.55, 'rgba(105,135,225,.9)');
    g.addColorStop(1, 'rgba(175,205,255,.85)');
    x.fillStyle = g;
    x.beginPath(); x.ellipse(0, 0, 26, 10, 0, 0, 7); x.fill();
    x.beginPath(); x.moveTo(-24, 0); x.lineTo(-40, -10); x.quadraticCurveTo(-33, 0, -40, 10); x.fill();
    x.beginPath(); x.moveTo(2, -9); x.quadraticCurveTo(9, -19, 15, -8); x.fill();
    x.beginPath(); x.ellipse(-2, 7, 9, 4, 0.25, 0, 7); x.fill();
    x.strokeStyle = 'rgba(200,220,255,.45)'; x.lineWidth = 0.8;
    for (let i = 0; i < 4; i++) { x.beginPath(); x.moveTo(6 + i * 4, 4); x.lineTo(4 + i * 4, 9); x.stroke(); }
    x.fillStyle = 'rgba(255,255,255,.95)'; x.beginPath(); x.arc(19, -3, 1.8, 0, 7); x.fill();
    x.fillStyle = 'rgba(20,30,60,.9)'; x.beginPath(); x.arc(19.4, -3, 0.9, 0, 7); x.fill();
  },
  comet: (x) => {
    const g = x.createLinearGradient(0, 0, -80, 0);
    g.addColorStop(0, 'rgba(255,255,255,.95)');
    g.addColorStop(0.25, `rgba(${_fxRGB(0.35)},.5)`);
    g.addColorStop(1, `rgba(${_fxRGB(0.35)},0)`);
    x.fillStyle = g;
    x.beginPath(); x.moveTo(0, -4.5); x.quadraticCurveTo(-40, -2, -80, 0);
    x.quadraticCurveTo(-40, 2, 0, 4.5); x.fill();
    const cg = x.createRadialGradient(0, 0, 0, 0, 0, 7);
    cg.addColorStop(0, 'rgba(255,255,255,1)'); cg.addColorStop(1, `rgba(${_fxRGB(0.4)},0)`);
    x.fillStyle = cg; x.beginPath(); x.arc(0, 0, 7, 0, 7); x.fill();
  },
  rocket: (x) => {
    const fg = x.createLinearGradient(-6, 0, -34, 0);
    fg.addColorStop(0, 'rgba(255,240,180,.95)'); fg.addColorStop(0.4, 'rgba(255,170,60,.75)');
    fg.addColorStop(1, 'rgba(255,90,40,0)');
    x.fillStyle = fg;
    const flick = 1 + Math.sin(Date.now() / 45) * 0.18;
    x.beginPath(); x.moveTo(-6, -3.5); x.lineTo(-30 * flick, 0); x.lineTo(-6, 3.5); x.fill();
    x.fillStyle = 'rgba(235,70,70,.95)';
    x.beginPath(); x.moveTo(-4, -5); x.lineTo(-13, -12); x.lineTo(-5, -2.5); x.fill();
    x.beginPath(); x.moveTo(-4, 5); x.lineTo(-13, 12); x.lineTo(-5, 2.5); x.fill();
    const bg = x.createLinearGradient(0, -6, 0, 6);
    bg.addColorStop(0, 'rgba(250,252,255,.98)'); bg.addColorStop(1, 'rgba(180,190,205,.95)');
    x.fillStyle = bg;
    x.beginPath(); x.moveTo(17, 0); x.quadraticCurveTo(4, -6.5, -6, -6); x.lineTo(-6, 6);
    x.quadraticCurveTo(4, 6.5, 17, 0); x.fill();
    x.fillStyle = 'rgba(120,205,255,.9)'; x.beginPath(); x.arc(6, 0, 2.4, 0, 7); x.fill();
  },
};
const _flyImgs = {};                      // id -> HTMLImageElement (lazy, cached)
function flybyImage(c) {
  if (!_flyImgs[c.id]) { const im = new Image(); im.src = c.src; _flyImgs[c.id] = im; }
  return _flyImgs[c.id];
}
/* Size/Speed/How-often are read LIVE below, never baked into a sprite at spawn. A flyby
   crosses in seconds but the gap between them runs to a minute, so a value frozen at
   spawn leaves the slider looking broken long after you let go of it. Same reason
   flybyNow() exists: the panel puts one on screen at once so a change proves itself. */
let _flying = [], _flyAt = 0, _flyGap = 4, _flyNow = false;
function flybyNow(want) { _flyNow = want || true; }   // 'want': a cast key or custom sprite id
function flybyForget(id, evict) {   // custom sprite switched off: drop anything of it mid-flight.
  if (evict) delete _flyImgs[id];  // deleted for good — only then release the cached image, or a
  _flying = _flying.filter(f => f.kind !== 'img' || f.img.id !== id);   // toggle would re-fetch it
}
function drawFlybys(x, w, h, t, a) {
  const s = Settings.load(), P = fxp('flybys');
  const custom = (s.flybyCustom || []).filter(c => c.on !== false);
  const cast = Object.keys(FLYBY_CAST).filter(k => (s.flybyCast || {})[k] !== false);
  const pool = cast.map(k => ({ kind: k })).concat(custom.map(c => ({ kind: 'img', img: c })));
  if (!pool.length) return;
  if (!_flyAt) _flyAt = t;
  if (flybyDue(t, _flyAt, _flyGap, P.rate, _flyNow, _flying.length, P.maxOn)) {
    const pick = flybyPick(pool, _flyNow, Math.random());
    _flyNow = false;
    const dir = Math.random() < 0.5 ? 1 : -1;
    const base = pick.kind === 'comet' ? 1 : 0.75 + Math.random() * 0.7;
    _flying.push({ ...pick, dir, scale: base,
      x: dir > 0 ? -120 : w + 120,
      y: h * (0.08 + Math.random() * 0.66),
      sp: (pick.kind === 'comet' ? 320 : pick.kind === 'whale' ? 34 : 70 + Math.random() * 90) * dir,
      bob: Math.random() * 6.283, spin: (Math.random() - 0.5) * 0.25 });
    // rare on purpose — it stops being fun once you can predict it. Stored unscaled;
    // flybyDue() applies the rate, so 'How often' retimes a wait already running.
    _flyAt = t; _flyGap = 18 + Math.random() * 52;
  }
  const dt = Math.min(0.05, x._dt || 0.016);
  _flying = _flying.filter(f => {
    f.x += f.sp * P.speed * dt;   // P.speed is always > 0, so the sign (= direction) holds
    if ((f.sp > 0 && f.x > w + 160) || (f.sp < 0 && f.x < -160)) return false;
    x.save();
    x.translate(f.x, f.y + Math.sin(t * 0.9 + f.bob) * 7);
    if (f.dir < 0) x.scale(-1, 1);
    x.scale(f.scale * P.scale, f.scale * P.scale);
    x.rotate(Math.sin(t * 0.5 + f.bob) * f.spin);
    x.globalAlpha = a * P.opacity;
    try {
      if (f.kind === 'img') {
        const im = flybyImage(f.img);
        if (im.complete && im.naturalWidth) {
          const s2 = 46 / Math.max(im.naturalWidth, im.naturalHeight);
          x.drawImage(im, -im.naturalWidth * s2 / 2, -im.naturalHeight * s2 / 2,
            im.naturalWidth * s2, im.naturalHeight * s2);
        }
      } else FLYBY_CAST[f.kind](x);
    } catch (e) { /* one bad sprite never kills the loop */ }
    x.restore();
    return true;
  });
}
// flybys render on whichever canvas the user picked; the other one no-ops
FX_FORE.flybys = (x, w, h, t, a) => { if (fxp('flybys').layer !== 'back') drawFlybys(x, w, h, t, a); };
FX_BACK.flybysBack = (x, w, h, t, a) => { if (fxp('flybys').layer === 'back') drawFlybys(x, w, h, t, a); };


/* --- custom media layers --------------------------------------------------------
   User-supplied GIFs/images as background effects. These are DOM <img> elements, not
   canvas draws, for one hard reason: drawImage() paints only the FIRST frame of a GIF
   — canvas has no notion of animated image playback — so a GIF layer has to be a real
   element to move at all. Config lives in settings; the bytes live on the server (see
   fx_media_save) because a GIF would blow the localStorage quota on its own. */
const FXMEDIA_POS = {
  full: 'inset:0;width:100%;height:100%;object-fit:cover',
  contain: 'inset:0;width:100%;height:100%;object-fit:contain',
  tl: 'top:6%;left:4%', tr: 'top:6%;right:4%',
  bl: 'bottom:6%;left:4%', br: 'bottom:6%;right:4%',
  c: 'inset:0;margin:auto',   // margin centring, not translate — the motion keyframes own transform
};
function renderFXMedia() {
  const host = $('#fxmedia');
  if (!host) return;
  const layers = (Settings.load().fxMedia || []).filter(m => m.on !== false && m.url);
  const have = new Map([...host.children].map(n => [n.dataset.id, n]));
  for (const m of layers) {
    let im = have.get(m.id);
    have.delete(m.id);
    if (!im) { im = el('img'); im.dataset.id = m.id; im.alt = ''; host.appendChild(im); }
    if (im.getAttribute('src') !== m.url) im.src = m.url;   // touching src replays the GIF from frame 0
    const pos = m.pos || 'full';
    // Fill/Fit already span the viewport, so Size becomes a zoom: --fxs feeds the inline
    // transform and every motion keyframe. Placed positions size the box directly.
    const sized = ['full', 'contain'].includes(pos)
      ? `--fxs:scale(${m.size || 1});`
      : `width:${Math.round((m.size || 1) * 26)}vmin;height:auto;`;
    im.style.cssText = 'position:fixed;z-index:1;pointer-events:none;'
      + (FXMEDIA_POS[pos] || FXMEDIA_POS.full) + ';' + sized
      + `opacity:${m.opacity != null ? m.opacity : 0.35};`
      + `mix-blend-mode:${m.blend || 'screen'};`
      + 'transform:var(--fxs);'
      + (m.motion && m.motion !== 'none'
        ? `animation:fxm-${m.motion} ${Math.max(3, 16 / (m.speed || 1))}s ${m.motion === 'spin' ? 'linear' : 'ease-in-out'} infinite;` : '');
  }
  have.forEach(n => n.remove());   // layers deleted from settings
}

function initFX() {
  const back = $('#fxback'), fore = $('#fxfore');
  if (!back || !fore) return;
  const bx = back.getContext('2d'), fx = fore.getContext('2d');
  const resize = () => {
    for (const c of [back, fore]) { c.width = innerWidth; c.height = innerHeight; }
    _cNodes = _rain = _traces = null;      // rebuild anything sized to the viewport
  };
  resize();
  addEventListener('resize', resize);
  let last = 0;
  (function frame(ms) {
    requestAnimationFrame(frame);
    const t = ms / 1000, dt = last ? t - last : 0.016; last = t;
    if (document.hidden) return;                       // background tab: draw nothing
    const s = Settings.load();
    if (s.motionLevel === 'off') {                     // motion off => no ambient layer at all
      if (back.width) { bx.clearRect(0, 0, back.width, back.height); fx.clearRect(0, 0, fore.width, fore.height); }
      return;
    }
    const alpha = s.fxIntensity != null ? +s.fxIntensity : 1;
    const backOn = Object.keys(FX_BACK).filter(k => s[k]);
    const foreOn = Object.keys(FX_FORE).filter(k => s[k]);
    if (!backOn.length && !foreOn.length) {            // nothing enabled: cost is one branch
      if (back._dirty) { bx.clearRect(0, 0, back.width, back.height); back._dirty = false; }
      if (fore._dirty) { fx.clearRect(0, 0, fore.width, fore.height); fore._dirty = false; }
      return;
    }
    bx.clearRect(0, 0, back.width, back.height); back._dirty = true;
    fx.clearRect(0, 0, fore.width, fore.height); fore._dirty = true;
    bx._dt = fx._dt = dt;
    for (const k of backOn) {
      try { FX_BACK[k](bx, back.width, back.height, t, alpha); }
      catch (e) { /* a broken effect must not take the others down */ }
    }
    for (const k of foreOn) {
      try { FX_FORE[k](fx, fore.width, fore.height, t, alpha); } catch (e) { /* ditto */ }
    }
  })(0);
}

/* ================= boot ================= */
const BOOT_LINES = [
  ['probing ~/.claude/projects', 'TRANSCRIPTS'],
  ['linking NexusMind store', 'MEMORY'],
  ['scanning claudeProjects', 'PROJECTS'],
  ['reading coordination bus', 'FEED'],
  ['loading agent roster + skills', 'CAPABILITIES'],
  ['arming headless claude runner', 'OPS'],
  ['mounting loop scheduler', 'LOOPS'],
  ['calibrating mission dashboard', 'DASHBOARD'],
];
/* ================= capability gating (Phase 3) =================
   Loaded ONCE in boot() before Dock.render()/WM.restoreLayout(). Hides dock icons, module tabs,
   palette entries and cross-app routing for integrations the server reports inactive. Fails OPEN:
   if /api/capabilities is unavailable (old server / network blip) data stays null and every answer
   defaults to "visible" — degrade to today's behavior, never a blank dock. The REQUIRES map
   (tab→integration) lives in apps.js next to MODULE_OF, since that file owns the UI structure. */
const Caps = {
  data: null,                                   // last /api/capabilities payload (null ⇒ fail open)
  async load() {
    this.data = await apiSafe('/api/capabilities', undefined, { silent: true });
    return this.data;
  },
  apply(payload) {                              // phases 4-5 call this after a POST /api/config
    this.data = payload || null;
    Bus.emit('caps:changed');
  },
  intActive(id) {                               // integrations[id].active; default TRUE if unknown
    const ints = this.data && this.data.integrations;
    if (!ints || !ints[id]) return true;
    return ints[id].active !== false;
  },
  moduleVisible(appId) {                         // user modules map; 'settings' always visible; absent = visible
    if (appId === 'settings') return true;
    const mods = this.data && this.data.modules;
    if (!mods || !(appId in mods)) return true;
    return mods[appId] !== false;
  },
  tabVisible(modId, tabKey) {                    // REQUIRES lookup ∧ intActive (ungated tab = always visible)
    const req = (typeof REQUIRES !== 'undefined') && REQUIRES[modId + '.' + tabKey];
    if (!req) return true;
    return this.intActive(req);
  },
  // composition rule (§5.2): app shown = moduleVisible(id) ∧ (non-module OR ≥1 visible tab)
  appVisible(app) {
    if (!app || app.id === 'settings') return true;
    if (!this.moduleVisible(app.id)) return false;
    const tabs = app.tabs;
    if (!tabs || !tabs.length) return true;      // standalone app (no tab bar) → visible
    return tabs.some(t => this.tabVisible(app.id, t.key));
  },
};
// Live toggling (§5.4): a config save stores fresh caps + emits caps:changed. Re-render the dock and
// respawn open module windows whose visible-tab set changed (or that just became fully hidden),
// preserving geometry/desktop and keeping the active tab if still visible else the first visible one.
Bus.on('caps:changed', () => {
  Dock.render();
  [...WM.wins.values()].forEach(win => {
    const app = win.app;
    if (!app || !app.tabs) return;                                     // only module windows have a tab bar
    const visNow = app.tabs.filter(t => Caps.tabVisible(app.id, t.key)).map(t => t.key);
    const prev = win._visTabs || app.tabs.map(t => t.key);
    const setChanged = visNow.length !== prev.length || visNow.some((k, i) => k !== prev[i]);
    const hiddenNow = !Caps.appVisible(app);
    if (!setChanged && !hiddenNow) return;                             // unaffected → leave it untouched
    const keepTab = (win._activeTab && visNow.includes(win._activeTab)) ? win._activeTab : visNow[0];
    const desktop = win.desktop, wasMin = win.min;
    WM.close(win);
    if (hiddenNow || !visNow.length) return;                          // module gone entirely → stay closed
    const nw = WM.open(app.id, { exact: true, restore: true, desktop });
    if (nw && nw._showTab && keepTab) nw._showTab(keepTab);
    if (nw && wasMin) WM.minimize(nw);
  });
});

async function boot() {
  applyScale();
  Theme.apply(Theme.current());
  Settings.apply();
  initStars();
  initFX();          // opt-in ambient layer; its loop no-ops until something is enabled
  renderFXMedia();   // user-supplied GIF/image layers (DOM, not canvas)
  const log = $('#bootlog'), bar = $('#bootbar i');
  const statePromise = refreshState();
  for (let i = 0; i < BOOT_LINES.length; i++) {
    await new Promise(r => setTimeout(r, 150 + Math.random() * 150));
    log.appendChild(el('div', '', `<b>▸ OK</b>  ${BOOT_LINES[i][0]} <span style="float:right;color:var(--faint)">${BOOT_LINES[i][1]}</span>`));
    bar.style.width = ((i + 1) / BOOT_LINES.length * 100) + '%';
  }
  await statePromise;
  await Caps.load();          // fetch /api/capabilities ONCE before dock/restore gate on it (fails open)
  await new Promise(r => setTimeout(r, 220));
  $('#topbar').style.display = 'flex';
  $('#desktop').style.display = 'block';
  $('#dock').style.display = 'flex';
  Dock.render();
  let _dockFitT;
  addEventListener('resize', () => { clearTimeout(_dockFitT); _dockFitT = setTimeout(() => Dock.fit(), 100); });
  WM.refreshSwitcher();
  startClock();
  $('#boot').classList.add('done');
  setTimeout(() => { const b = $('#boot'); if (b) b.remove(); }, 800);
  WM.restoreLayout();   // reopen whatever windows were open last session (+ live terminals)
  // First-run setup wizard (§7): only on a truly fresh install (config seeded first_run_done:false).
  // It's a modal GUIDE over the already-booted desktop — never a gate — so we fire it after restore.
  // Existing installs seed first_run_done:true, so first_run is false and this never triggers for them.
  if (Caps.data && Caps.data.first_run === true && typeof openSetupWizard === 'function') {
    try { openSetupWizard(); } catch (e) { /* wizard must never block boot */ }
  }
  setInterval(refreshState, 12000);
  // preload recent sessions for the palette (id + cwd used for resume)
  apiSafe('/api/sessions?limit=25', undefined, { silent: true }).then(r => { if (r) State.sessions = r.sessions; });
  refreshEnabledAgents();   // warm the enabled-agents cache for the split-button + ⌘K actions
  checkForUpdate();         // notify (once) if a newer release has been published
}

// Update notifier: the server compares the running VERSION to the latest GitHub
// release (/api/update, cached server-side). Dormant on a private repo (the API
// 404s → available:false) until it's public. Purely a nudge — no auto-install.
async function checkForUpdate() {
  const r = await apiSafe('/api/update', undefined, { silent: true });
  if (!r || !r.available || !r.url) return;
  if (sessionStorage.getItem('zen.updateSeen') === r.latest) return;   // don't re-nag this session
  const bar = el('div', 'update-banner',
    `<span>ZENITH <b>v${esc(r.latest)}</b> available <span style="color:var(--faint)">(on v${esc(r.current || '?')})</span></span>
     <a href="${esc(r.url)}" target="_blank" rel="noopener" style="color:#7fe;font-weight:600;text-decoration:none">Download ↗</a>
     <button class="ub-x" title="Dismiss" style="background:none;border:0;color:#9cf;cursor:pointer;font-size:13px">✕</button>`);
  bar.style.cssText = 'position:fixed;top:8px;left:50%;transform:translateX(-50%);z-index:99999;' +
    'display:flex;gap:12px;align-items:center;padding:7px 14px;border-radius:10px;' +
    'background:rgba(18,28,44,.94);border:1px solid var(--cyan-soft,#49f);color:#dff;' +
    'font-size:12px;box-shadow:0 6px 24px rgba(0,0,0,.45);backdrop-filter:blur(8px)';
  bar.querySelector('.ub-x').onclick = () => { sessionStorage.setItem('zen.updateSeen', r.latest); bar.remove(); };
  document.body.appendChild(bar);
}
// ---- Tailscale re-auth guard ----------------------------------------------
// Fleet ops (GPU dispatch, remote-model chat) route over the tailnet; if Tailscale
// has de-authed they fail. Call ZTailscale.onFailure() after a remote op errors — it
// pops a re-auth prompt ONLY when Tailscale is the cause, instead of a cryptic error.
const ZTailscale = {
  async status() { return await apiSafe('/api/tailscale/status', undefined, { silent: true }); },
  async ok() { const s = await this.status(); if (s && s.needs_reauth) { this.prompt(s); return false; } return true; },
  async onFailure() { const s = await this.status(); if (s && s.needs_reauth) { this.prompt(s); return true; } return false; },
  prompt(s) {
    if (document.getElementById('ts-reauth')) return;
    const ov = el('div'); ov.id = 'ts-reauth';
    ov.style.cssText = 'position:fixed;inset:0;z-index:100000;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.55);backdrop-filter:blur(4px)';
    const health = ((s && s.health) || []).join(' · ');
    ov.innerHTML = `<div style="max-width:460px;background:rgba(18,26,40,.98);border:1px solid var(--amber,#e6b450);border-radius:12px;padding:20px 22px;box-shadow:0 12px 44px rgba(0,0,0,.55)">
      <div style="font-size:15px;font-weight:700;color:var(--amber,#e6b450);margin-bottom:8px">Tailscale needs re-authentication</div>
      <div style="color:#cdd8e6;font-size:13px;line-height:1.5">The remote machines (mini, gpu-node) route over your tailnet, and Tailscale isn't logged in — so fleet actions (GPU jobs, remote model chat) will keep failing until you re-authenticate.${health ? '<div style="color:var(--dim);font-size:11px;margin-top:8px">' + esc(health) + '</div>' : ''}</div>
      <div style="display:flex;gap:8px;margin-top:16px;justify-content:flex-end">
        <button class="btn ghost sm" data-a="dismiss">Dismiss</button>
        <button class="btn ghost sm" data-a="recheck">↻ Recheck</button>
        <button class="btn sm acc" data-a="auth">Re-authenticate</button></div></div>`;
    document.body.appendChild(ov);
    const close = () => ov.remove();
    ov.querySelector('[data-a=dismiss]').onclick = close;
    ov.querySelector('[data-a=auth]').onclick = async () => {
      const r = await apiSafe('/api/tailscale/reauth', undefined, { silent: true });
      if (r && r.auth_url) window.open(r.auth_url, '_blank');
      else if (r && r.note) toast(r.note, 'ok');
    };
    ov.querySelector('[data-a=recheck]').onclick = async () => {
      const s2 = await this.status();
      if (!s2 || !s2.needs_reauth) { close(); toast('Tailscale reconnected', 'ok'); }
      else toast('still not authenticated — finish logging in, then Recheck', 'err');
    };
  }
};
window.ZTailscale = ZTailscale;
$('#boot').addEventListener('click', () => $('#boot').classList.add('done'));

/* ================= debug handle (verification) ================= */
window.ZDEBUG = { WM, State, Bus, Dock, Palette, setScale, Settings, Theme, THEMES };
