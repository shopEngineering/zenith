// Cross-file check: every CSS-driven effect knob must have the SAME value in two places.
//
//   index.html   var(--neb-blur, 22px)   <- the fallback, used when nothing is saved
//   app.js       { k: 'blur', def: 22 }  <- the slider's default, written by applyFXVars
//
// If those drift apart, an install that has never touched the panel renders differently
// from one that opened it and changed nothing — a difference nobody would think to look
// for, and which no amount of staring at either file on its own would reveal.
import assert from 'node:assert';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, 'index.html'), 'utf8');
const appjs = readFileSync(join(here, 'app.js'), 'utf8');

// cssVar -> [effect, param key, unit applyFXVars appends]
const BOUND = {
  '--neb-a': ['nebula', 'a', ''], '--neb-blur': ['nebula', 'blur', 'px'], '--neb-t': ['nebula', 'secs', 's'],
  '--hor-h': ['horizon', 'h', 'vh'], '--hor-gap': ['horizon', 'gap', 'px'],
  '--hor-tilt': ['horizon', 'tilt', 'deg'], '--hor-a': ['horizon', 'a', ''],
  '--scan-a': ['scanlines', 'a', ''], '--scan-pitch': ['scanlines', 'pitch', 'px'],
  '--vig-a': ['vignette', 'a', ''], '--vig-size': ['vignette', 'size', '%'],
  '--grain-a': ['grain', 'a', ''], '--grain-t': ['grain', 'secs', 's'],
  '--sweep-h': ['sweep', 'h', 'px'], '--sweep-t': ['sweep', 'secs', 's'], '--sweep-a': ['sweep', 'a', ''],
  '--flick-t': ['flicker', 'secs', 's'], '--flick-s': ['flicker', 'strength', ''],
  '--hud-sz': ['hud', 'sz', 'px'], '--hud-w': ['hud', 'w', 'px'],
  '--hud-a': ['hud', 'a', ''], '--hud-t': ['hud', 'secs', 's'],
  '--glitch-t': ['glitch', 'secs', 's'], '--glitch-s': ['glitch', 'strength', ''],
};

// --- pull the FX_SPECS defaults straight out of app.js ------------------------------
const specsSrc = appjs.slice(appjs.indexOf('const FX_SPECS'), appjs.indexOf('function fxp'));
const defOf = (effect, key) => {
  const block = new RegExp(`\\n  ${effect}: \\[([\\s\\S]*?)\\n  \\],`).exec(specsSrc);
  assert.ok(block, `FX_SPECS has no '${effect}' block`);
  const row = new RegExp(`\\{ k: '${key}',[^}]*?def: (-?[\\d.]+)`).exec(block[1]);
  assert.ok(row, `FX_SPECS.${effect} has no param '${key}'`);
  return row[1];
};

// --- and the fallbacks out of index.html --------------------------------------------
const fallbacks = new Map();
for (const m of html.matchAll(/var\((--[a-z0-9-]+)\s*,\s*([^)]+)\)/gi)) {
  if (!fallbacks.has(m[1])) fallbacks.set(m[1], m[2].trim());
}

let checked = 0;
for (const [cssVar, [effect, key, unit]] of Object.entries(BOUND)) {
  const fb = fallbacks.get(cssVar);
  assert.ok(fb !== undefined, `${cssVar} is bound to ${effect}.${key} but never appears in index.html`);
  const want = defOf(effect, key) + unit;
  // .5s and 0.5s are the same number; compare numerically, keeping the unit
  const num = s => parseFloat(s);
  const suffix = s => s.replace(/^-?[\d.]+/, '');
  assert.strictEqual(suffix(fb), suffix(want), `${cssVar}: unit mismatch — css '${fb}' vs spec '${want}'`);
  assert.strictEqual(num(fb), num(want),
    `${cssVar}: index.html falls back to '${fb}' but FX_SPECS.${effect}.${key} defaults to '${want}'`);
  checked++;
}
assert.strictEqual(checked, Object.keys(BOUND).length);

// --hor-gap2 is derived, not a slider: applyFXVars holds the original 72:46 ratio
{
  const gap = parseFloat(defOf('horizon', 'gap'));
  assert.strictEqual(Math.max(4, Math.round(gap * 46 / 72)) + 'px', fallbacks.get('--hor-gap2'),
    '--hor-gap2 no longer lands on its original 46px when the gap is at its default');
}

// every var the stylesheet reads for an effect must be one we actually drive
for (const cssVar of fallbacks.keys()) {
  if (/^--(neb|hor|scan|vig|grain|sweep|flick|hud|glitch)-/.test(cssVar) && !(cssVar in BOUND)) {
    assert.strictEqual(cssVar, '--hor-gap2', `${cssVar} is in the CSS but nothing ever sets it`);
  }
}

// --- every effect OFFERED a cursor toggle must actually respond to the cursor ---------
// MOUSE_AWARE decides which panels show "Cursor interaction". An effect listed there but
// never wired presents a switch that silently does nothing, which is worse than not
// offering it — and the two live hundreds of lines apart, so they drift quietly.
{
  const declared = new Set(/MOUSE_AWARE = new Set\(\[([\s\S]*?)\]\)/.exec(appjs)[1]
    .split(',').map(x => x.trim().replace(/^'|'$/g, '')).filter(Boolean));
  const wired = new Set([...appjs.matchAll(/fxMouse\('([a-zA-Z]+)'\)/g)].map(m => m[1]));
  const unwired = [...declared].filter(k => !wired.has(k));
  const unoffered = [...wired].filter(k => !declared.has(k));
  assert.deepStrictEqual(unwired, [],
    `these effects offer a cursor toggle that does nothing: ${unwired.join(', ')}`);
  assert.deepStrictEqual(unoffered, [],
    `these effects respond to the cursor but never offer the toggle: ${unoffered.join(', ')}`);
  assert.ok(declared.size >= 5, 'MOUSE_AWARE looks empty — the regex probably stopped matching');
}

// --- every effect OFFERED a Colour control must actually resolve colour through it ------
// A per-effect hue reaches the canvas only via _fxRGB(). Effects that take var(--acc) from
// the stylesheet, or carry their own colour params, cannot honour it — and a Colour knob
// that silently does nothing is the same defect as a dead cursor toggle.
{
  const declared = new Set(/COLOUR_AWARE = new Set\(\[([\s\S]*?)\]\)/.exec(appjs)[1]
    .split(',').map(x => x.trim().replace(/^'|'$/g, '')).filter(Boolean));

  // effects whose registered draw function resolves colour through _fxRGB
  const usesFxRGB = new Set();
  const re = /FX_(?:BACK|FORE)\.(\w+) = \(/g;
  for (let m = re.exec(appjs); m; m = re.exec(appjs)) {
    const rest = appjs.slice(m.index + m[0].length);
    const end = rest.search(/\nFX_(?:BACK|FORE)\.\w+ = \(|\n\/\* ---/);
    if (rest.slice(0, end === -1 ? 4000 : end).includes('_fxRGB(')) usesFxRGB.add(m[1]);
  }
  // the starfield draws in its own loop, so it resolves its hue directly instead
  assert.ok(/fxHueOf\('stars'\)/.test(appjs),
    'the starfield is offered Colour, so it must resolve a custom hue of its own');
  usesFxRGB.add('stars');

  const dead = [...declared].filter(k => !usesFxRGB.has(k));
  assert.deepStrictEqual(dead, [],
    `these effects offer a Colour control that cannot do anything: ${dead.join(', ')}`);
  assert.ok(declared.size >= 5, 'COLOUR_AWARE looks empty — the regex probably stopped matching');
}

console.log(`fx-vars: all assertions passed (${checked} knobs pinned to their CSS fallbacks)`);
