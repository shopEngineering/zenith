import assert from 'node:assert';
import { createRequire } from 'node:module';
const { eachLinkedPair } = createRequire(import.meta.url)('./constellation-links.js');

// deterministic PRNG so a failure is always reproducible
let _s = 12345;
const rnd = () => (_s = (_s * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
const make = (n, w, h) => Array.from({ length: n }, (_, id) => ({ id, x: rnd() * w, y: rnd() * h }));

// the reference the grid has to match: every pair, tested directly
const brute = (nodes, link) => {
  const out = [];
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const d = Math.hypot(nodes[i].x - nodes[j].x, nodes[i].y - nodes[j].y);
      if (d <= link) out.push([nodes[i].id, nodes[j].id].sort((a, b) => a - b).join('-'));
    }
  }
  return out.sort();
};
const viaGrid = (nodes, link, w, h) => {
  const out = [];
  eachLinkedPair(nodes, link, w, h, (p, q) => out.push([p.id, q.id].sort((a, b) => a - b).join('-')));
  return out.sort();
};

// --- the grid must find exactly the pairs brute force finds, and each one once -----
for (const [n, link, w, h] of [[200, 90, 800, 600], [200, 37, 800, 600], [60, 250, 800, 600]]) {
  const nodes = make(n, w, h);
  const got = viaGrid(nodes, link, w, h);
  assert.deepStrictEqual(got, brute(nodes, link), `pairs differ at n=${n} link=${link}`);
  assert.strictEqual(new Set(got).size, got.length, `duplicate pair at n=${n} link=${link}`);
  assert.ok(got.length > 0, `no links at all at n=${n} link=${link} — test is not exercising anything`);
}

// link range larger than the canvas collapses to a single cell: still exact, still once
{
  const nodes = make(40, 300, 200);
  const got = viaGrid(nodes, 5000, 300, 200);
  assert.deepStrictEqual(got, brute(nodes, 5000));
  assert.strictEqual(got.length, 40 * 39 / 2);   // everything links to everything
}

// nodes drifted off-canvas are clamped into edge cells but still measured exactly
{
  const nodes = [{ id: 0, x: -500, y: -500 }, { id: 1, x: -495, y: -500 }, { id: 2, x: 400, y: 300 }];
  assert.deepStrictEqual(viaGrid(nodes, 50, 800, 600), ['0-1']);
}

// reported distance is the real distance, not the squared one
{
  const nodes = [{ id: 0, x: 0, y: 0 }, { id: 1, x: 30, y: 40 }];
  let seen = null;
  eachLinkedPair(nodes, 60, 800, 600, (p, q, d) => { seen = d; });
  assert.strictEqual(seen, 50);
}

// degenerate inputs are silent no-ops rather than throwing on a background canvas
for (const bad of [[[], 100], [[{ id: 0, x: 1, y: 1 }], 100], [make(10, 800, 600), 0], [make(10, 800, 600), -5]]) {
  assert.doesNotThrow(() => eachLinkedPair(bad[0], bad[1], 800, 600, () => {
    assert.fail('no pair should be reported for a degenerate input');
  }));
}

console.log('constellation-links: all assertions passed');
