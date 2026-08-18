// NexusMind terminal: command dispatch + the nexusmind_api gate.
//
// The dispatch table (NM_TYPED / NM_SHOW_TAGS / the prefix order) is a straight port of
// NexusMind's own runCmd, and it is exactly the kind of table that rots silently — a
// wrong namespace or a prefix that loses to a shorter one still "works", it just files
// things in the wrong place. Same for the gate: appVisible() answering `true` when the
// integration is off is invisible until someone toggles the switch.
//
// Both are slices of the real static/*.js run against stubs; the slice markers are
// named consts, and a moved marker fails the located-assert loudly rather than
// quietly testing nothing.
import assert from 'node:assert';
import { readFileSync } from 'node:fs';


const read = name => readFileSync(new URL('./' + name, import.meta.url), 'utf8');

const src = read('apps.js');
const start = src.indexOf('const NM_TYPED = {');
const end = src.indexOf('const NM_HINT_CHIPS');
assert.ok(start > 0 && end > start, 'terminal block located');
const block = src.slice(start, end);

const calls = [];
const preamble = `
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const performance = { now: () => 0 };
  const apiSafe = async (path, opts) => {
    __calls.push({ path, body: opts && opts.body ? JSON.parse(opts.body) : null });
    if (path.startsWith('/api/memory/ask'))     return { available: true, context: '[Memory: t]\\nhello', sources: [{type:'memory',title:'t'}] };
    if (path.startsWith('/api/memory/ingest'))  return { available: true, key: 'k1', classification: { type:'idea', namespace:'work', tags:['idea'] }, dedup_action:'new', relationships_created: 2 };
    if (path.startsWith('/api/memory/correct')) return { available: true, key: 'k2', title: 'T', correction_applied: 'x' };
    if (path.startsWith('/api/memory/nmstats')) return { available: true, stats: { total_memories: 5, embedded: 5, embedding_coverage: 100 } };
    if (path.startsWith('/api/memory/meta'))    return { available: true, namespaces: [{name:'work',count:3}] };
    if (path.startsWith('/api/memory'))         return { available: true, memories: [{key:'m1',title:'M',namespace:'work'}] };
    return null;
  };
`;
const fn = new Function('__calls', preamble + block + '\nreturn { nmRunCommand, NM_TYPED, NM_SHOW_TAGS, NM_UNPROXIED };');
const M = fn(calls);

const last = () => calls[calls.length - 1];
const run = async cmd => { calls.length = 0; return { html: await M.nmRunCommand(cmd), call: last() }; };

// --- typed captures carry the exact NexusMind hints -------------------------------
let r = await run('idea: a new dashboard');
assert.equal(r.call.path, '/api/memory/ingest');
assert.deepEqual(r.call.body, { text: 'a new dashboard', hints: { type: 'idea', namespace: 'work', tags: ['idea', 'backlog'] } });

r = await run('q: park this');
assert.deepEqual(r.call.body.hints, { type: 'todo', namespace: 'work', tags: ['queue', 'backlog'] });

r = await run('bug: it crashes');
assert.deepEqual(r.call.body.hints, { type: 'bug-fix', namespace: 'work', tags: ['bug-fix'] });

// longest prefix wins: instruct_g: must not be eaten by instruct:, save-link: by save:
r = await run('instruct_g: always do X');
assert.deepEqual(r.call.body.hints.tags, ['instruction', 'global']);
r = await run('instruct: just this session');
assert.deepEqual(r.call.body.hints.tags, ['instruction', 'session-scoped']);
r = await run('save-link: http://x');
assert.deepEqual(r.call.body.hints.tags, ['bookmark', 'backlog']);
r = await run('save: raw text');            // a bare ingest, not a typed capture
assert.deepEqual(r.call.body, { text: 'raw text' });

// req: is ZENITH's ⌘K grammar; it must mean the same as requirement:
const a = await run('req: must log in'), b = await run('requirement: must log in');
assert.deepEqual(a.call.body.hints, b.call.body.hints);

// --- ask / ingest / correct -------------------------------------------------------
r = await run('ask: what is EAP?');
assert.equal(r.call.path, '/api/memory/ask');
assert.deepEqual(r.call.body, { question: 'what is EAP?' });
assert.ok(r.html.includes('hello') && r.html.includes('1 sources'), 'ask renders answer + sources');

r = await run('correct: her name is Krissy');
assert.equal(r.call.path, '/api/memory/correct');
assert.deepEqual(r.call.body, { text: 'her name is Krissy' });
r = await run('fix: x');  assert.equal(r.call.path, '/api/memory/correct');
r = await run('update: x'); assert.deepEqual(r.call.body, { text: 'x' });

r = await run('ingest: some prose');
assert.deepEqual(r.call.body, { text: 'some prose' });

// unmatched input defaults to ask, exactly as NexusMind does
r = await run('who is luke');
assert.equal(r.call.path, '/api/memory/ask');
assert.deepEqual(r.call.body, { question: 'who is luke' });

// --- search / show / list / system ------------------------------------------------
r = await run('search vector db'); assert.ok(r.call.path.includes('q=vector%20db'));
r = await run('find vector db');   assert.ok(r.call.path.includes('q=vector%20db'));
r = await run('show todos');       assert.ok(r.call.path.includes('tag=todo'), r.call.path);
r = await run('show expenses');    assert.ok(r.call.path.includes('tag=financial'));
r = await run('show weirdtag');    assert.ok(r.call.path.includes('tag=weirdtag'));
r = await run('list backlog');     assert.ok(r.call.path.includes('tag=backlog'));
r = await run('recent');           assert.ok(r.call.path.includes('limit=10'));
r = await run('stats');            assert.equal(r.call.path, '/api/memory/nmstats');
r = await run('ns');               assert.equal(r.call.path, '/api/memory/meta');
r = await run('namespaces');       assert.equal(r.call.path, '/api/memory/meta');

// help is local — it must not hit the network at all
calls.length = 0;
const help = await M.nmRunCommand('help');
assert.equal(calls.length, 0, 'help makes no request');
assert.ok(help.includes('ask:') && help.includes('Not proxied'));

// --- unproxied commands refuse in words instead of silently becoming ask: ----------
for (const cmd of ['share: http://x', 'summarize: k', 'report: k', 'theme: warm',
                   'goal: ship it', 'ref: /tmp/x', 'prompt: hi', 'refs',
                   'sync_status', 'pair http://x "n"', 'https://example.com']) {
  calls.length = 0;
  const out = await M.nmRunCommand(cmd);
  assert.equal(calls.length, 0, cmd + ' makes no request');
  assert.ok(out.includes('not proxied by ZENITH'), cmd + ' names itself as unproxied');
}

// --- every /api/memory/* answer's failure shape renders as one error line ----------
const failPreamble = `
  const esc = s => String(s ?? '');
  const performance = { now: () => 0 };
  const apiSafe = async () => ({ available: false, error: 'integration disabled' });
`;
const fail = new Function('__calls', failPreamble + block + '\nreturn { nmRunCommand };')(calls);
for (const cmd of ['ask: x', 'idea: x', 'correct: x', 'search x', 'show todos', 'stats', 'recent', 'ns']) {
  const out = await fail.nmRunCommand(cmd);
  assert.ok(out.includes('integration disabled'), cmd + ' surfaces the gate reason');
}

console.log('nm terminal command dispatch: all checks passed');

/* ================= the nexusmind_api gate ================= */
const appjs = read('app.js');
const appsjs = src;

const slice = (src, from, to, label) => {
  const a = src.indexOf(from), b = src.indexOf(to, a);
  assert.ok(a > 0 && b > a, label + ' located');
  return src.slice(a, b);
};
const caps = slice(appjs, 'const Caps = {', '\n// Live toggling', 'Caps');
const requires = slice(appsjs, 'const REQUIRES = {', '\nconst MODULE_OF', 'REQUIRES');
const moduleOf = slice(appsjs, 'const MODULE_OF = {', '\nconst APPS', 'MODULE_OF');

const mk = new Function(requires + moduleOf + caps + '\nreturn { Caps, REQUIRES, MODULE_OF };');
const { Caps, REQUIRES, MODULE_OF } = mk();

// The dock apps as APPS declares them (shape only — enough for the composition rule).
const NexusMind = { id: 'memory', name: 'NexusMind' };                    // no tab bar
const Docs = { id: 'library', name: 'Docs' };                             // collapsed to plain
const Run = { id: 'run', tabs: [{ key: 'jobs' }, { key: 'loops' }, { key: 'swarm' },
                                { key: 'watchers' }, { key: 'builder' }] };
const Lab = { id: 'lab', tabs: [{ key: 'ab' }, { key: 'gpu' }] };

// --- integration ON (or unknown => fail open) -> NexusMind is visible ---------------
Caps.data = null;
assert.equal(Caps.appVisible(NexusMind), true, 'no caps payload => fail open, button shown');
Caps.data = { integrations: { nexusmind_api: { active: true } }, modules: {} };
assert.equal(Caps.appVisible(NexusMind), true, 'integration on => button shown');

// --- integration OFF -> the whole app disappears, terminal and all six tabs with it --
Caps.data = { integrations: { nexusmind_api: { active: false } }, modules: {} };
assert.equal(Caps.appVisible(NexusMind), false, 'integration off => dock button hidden');
assert.equal(Caps.tabVisible('run', 'watchers'), false, 'watchers shares the same gate');
assert.equal(Caps.appVisible(Docs), true, 'Docs is not gated on NexusMind');
assert.equal(Caps.appVisible(Lab), true, 'Lab keeps its GPU/AB tabs (fleet gate unchanged)');
assert.equal(Caps.appVisible(Run), true, 'Run survives on its other four tabs');

// --- the user "modules" switch still hides it independently of the integration -------
Caps.data = { integrations: { nexusmind_api: { active: true } }, modules: { memory: false } };
assert.equal(Caps.appVisible(NexusMind), false, 'user-hidden module stays hidden');

// --- registry invariants -------------------------------------------------------------
assert.equal(REQUIRES.memory, 'nexusmind_api', 'NexusMind is gated as a whole app');
assert.ok(!('library.memory' in REQUIRES), 'the retired Memory-tab gate is gone');
assert.ok(!('memory' in MODULE_OF), 'memory is a top-level app id, not a module hop');
assert.deepEqual(MODULE_OF.files, { m: 'library' }, 'files still routes to the Docs window');

// --- APPS/registration, read straight out of the source ------------------------------
const apps = slice(appsjs, 'const APPS = [', '];', 'APPS');
assert.ok(/\bNexusMindApp\b/.test(apps), 'NexusMindApp is registered in APPS');
assert.ok(/id: 'memory', name: 'NexusMind', icon: I\.memory/.test(appsjs),
  'the dock entry uses the NexusMind icon');
assert.ok(!/moduleApp\(\{ id: 'library'/.test(appsjs), 'Docs is no longer a tab module');
assert.ok(/MemoryApp\.render\(host, win\)/.test(appsjs), 'the six tabs still come from MemoryApp');
assert.ok(appsjs.indexOf("body.appendChild(nmTerminal())") < appsjs.indexOf("MemoryApp.render(host, win)"),
  'the terminal is appended above the tabs');

console.log('nm gating + registry: all checks passed');
