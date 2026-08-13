import assert from 'node:assert';
import { createRequire } from 'node:module';
const { stepGravity } = createRequire(import.meta.url)('./constellation-gravity.js');

const at = (x, y, g) => ({ x, y, vx: 0, vy: 0, g });
const NO_DAMP = 1, NO_CAP = Infinity;

// --- direction: who pulls, who pushes ----------------------------------------------
// two gravity nodes fall toward each other
{
  const [a, b] = [at(0, 0, 1), at(100, 0, 1)];
  stepGravity([a, b], 1000, NO_DAMP, NO_CAP);
  assert.ok(a.vx > 0, 'left gravity node should accelerate right, toward the other');
  assert.ok(b.vx < 0, 'right gravity node should accelerate left');
}
// a gravity node and an anti-gravity node push apart
{
  const [a, b] = [at(0, 0, 1), at(100, 0, -1)];
  stepGravity([a, b], 1000, NO_DAMP, NO_CAP);
  assert.ok(a.vx < 0 && b.vx > 0, 'a mixed pair must repel');
}
// two anti-gravity nodes also push apart — this is the rule that keeps the mix slider
// monotonic, and is the one place the model departs from signed-mass gravity
{
  const [a, b] = [at(0, 0, -1), at(100, 0, -1)];
  stepGravity([a, b], 1000, NO_DAMP, NO_CAP);
  assert.ok(a.vx < 0 && b.vx > 0, 'two anti-gravity nodes must repel, not clump');
}

// --- momentum is conserved: the sim can never drift the whole field off screen -------
// Momentum is sum(gm*v), NOT sum(v): forces are equal and opposite but accelerations are
// not, because a heavier node resists the same force more. Weighting by mass is the whole
// point — an unweighted sum only looks conserved while every mass happens to be equal.
{
  let seed = 7;
  const rnd = () => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
  const nodes = Array.from({ length: 120 }, () => {
    const p = at(rnd() * 900, rnd() * 600, rnd() < 0.35 ? -1 : 1);
    p.gm = 0.3 + rnd() * 3;                     // deliberately varied, not all 1
    return p;
  });
  stepGravity(nodes, 900, NO_DAMP, NO_CAP);
  const px = nodes.reduce((s, p) => s + p.gm * p.vx, 0);
  const py = nodes.reduce((s, p) => s + p.gm * p.vy, 0);
  assert.ok(Math.abs(px) < 1e-9 && Math.abs(py) < 1e-9,
    `net momentum must stay zero, got (${px}, ${py})`);
}

// --- mass: a heavy node barely moves, a light one is flung -------------------------
{
  const heavy = at(0, 0, 1), light = at(120, 0, 1);
  heavy.gm = 50; light.gm = 0.5;
  stepGravity([heavy, light], 4000, NO_DAMP, NO_CAP);
  assert.ok(Math.abs(light.vx) > Math.abs(heavy.vx) * 50,
    'the lighter node must accelerate far more than the heavy one it is falling toward');
  assert.ok(heavy.vx > 0 && light.vx < 0, 'they should still be falling toward each other');
}

// --- polarity magnitude: |g| scales the force, so an oscillator fades through zero --
{
  const full = [at(0, 0, 1), at(100, 0, 1)];
  const half = [at(0, 0, 0.5), at(100, 0, 1)];
  const zero = [at(0, 0, 0), at(100, 0, 1)];
  [full, half, zero].forEach(pair => stepGravity(pair, 1000, NO_DAMP, NO_CAP));
  assert.ok(full[0].vx > half[0].vx && half[0].vx > 0, '|g| must scale the pull');
  // at g == 0 only the always-on core repulsion remains, so it pushes rather than pulls
  assert.ok(zero[0].vx < 0, 'a node at zero polarity should feel no attraction at all');
}

// --- inverse square: quadrupling the distance quarters the pull ----------------------
{
  const near = [at(0, 0, 1), at(100, 0, 1)];
  const far = [at(0, 0, 1), at(200, 0, 1)];
  stepGravity(near, 1000, NO_DAMP, NO_CAP);
  stepGravity(far, 1000, NO_DAMP, NO_CAP);
  const ratio = near[0].vx / far[0].vx;
  assert.ok(ratio > 3.5 && ratio < 4.5, `expected ~4x falloff over 2x distance, got ${ratio.toFixed(2)}`);
}

// --- softening: coincident nodes must not produce NaN or a slingshot -----------------
{
  const nodes = [at(50, 50, 1), at(50, 50, 1)];
  stepGravity(nodes, 5000, NO_DAMP, NO_CAP);
  for (const p of nodes) {
    assert.ok(Number.isFinite(p.vx) && Number.isFinite(p.vy), 'coincident nodes produced a non-finite velocity');
  }
  assert.strictEqual(nodes[0].vx, 0, 'a zero separation has no direction, so no acceleration');
}

// --- the speed cap is a hard ceiling, and damping actually bleeds energy --------------
{
  const nodes = [at(0, 0, 1), at(1, 0, 1)];
  stepGravity(nodes, 1e9, NO_DAMP, 6);
  for (const p of nodes) {
    assert.ok(Math.hypot(p.vx, p.vy) <= 6 + 1e-9, 'speed cap breached');
  }
}
{
  const p = { x: 0, y: 0, vx: 10, vy: 0, g: 1 };
  const q = { x: 1e7, y: 0, vx: 0, vy: 0, g: 1 };   // far enough that the force is nil
  stepGravity([p, q], 1, 0.5, NO_CAP);
  assert.ok(Math.abs(p.vx - 5) < 1e-3, `damping should halve 10 to ~5, got ${p.vx}`);
}

// --- disabled or degenerate input is a no-op, never a throw --------------------------
for (const [nodes, g] of [[[], 500], [[at(0, 0, 1)], 500], [[at(0, 0, 1), at(9, 9, 1)], 0]]) {
  const before = nodes.map(p => [p.vx, p.vy]);
  assert.strictEqual(stepGravity(nodes, g, NO_DAMP, NO_CAP), 0);
  assert.deepStrictEqual(nodes.map(p => [p.vx, p.vy]), before, 'a disabled step must not touch velocities');
}
assert.doesNotThrow(() => stepGravity(null, 500, 1, 1));

// --- every pair is visited exactly once ----------------------------------------------
assert.strictEqual(stepGravity(Array.from({ length: 50 }, (_, i) => at(i * 3, i * 2, 1)), 100, 1, NO_CAP),
  50 * 49 / 2);

// --- emergent behaviour, the part that is not obvious from the force law -------------
// SOFT, CORE and the caller's strength scale are tuned against each other. Get the
// balance wrong and the sim still passes every test above while doing the opposite of
// what the control promises: the cluster overshoots, virialises into a hot gas, and
// MORE gravity yields a LOOSER field. This runs the real loop and pins the promise.
{
  const W = 1920, H = 1080;
  const spread = ns => {
    const cx = ns.reduce((s, p) => s + p.x, 0) / ns.length;
    const cy = ns.reduce((s, p) => s + p.y, 0) / ns.length;
    return Math.sqrt(ns.reduce((s, p) => s + (p.x - cx) ** 2 + (p.y - cy) ** 2, 0) / ns.length);
  };
  // same scale the constellations effect uses; keep these in step with app.js
  const MUL = 40, DAMP = 0.99, CAP = 6;
  const run = (gstr, anti) => {
    let seed = 4242;
    const rnd = () => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
    const ns = Array.from({ length: 150 }, () => ({
      x: rnd() * W, y: rnd() * H, vx: (rnd() - 0.5) * 0.16, vy: (rnd() - 0.5) * 0.16, r: rnd() }));
    for (let f = 0; f < 600; f++) {
      for (const p of ns) p.g = p.r < anti ? -1 : 1;
      stepGravity(ns, gstr * MUL, DAMP, CAP);
      for (const p of ns) {
        p.x += p.vx; p.y += p.vy;
        if (p.x < 0) { p.x = 0; p.vx = Math.abs(p.vx); } else if (p.x > W) { p.x = W; p.vx = -Math.abs(p.vx); }
        if (p.y < 0) { p.y = 0; p.vy = Math.abs(p.vy); } else if (p.y > H) { p.y = H; p.vy = -Math.abs(p.vy); }
      }
    }
    assert.ok(ns.every(p => Number.isFinite(p.x) && Number.isFinite(p.y)), 'simulation produced a non-finite position');
    return spread(ns);
  };

  // more gravity must mean a tighter field, at every step up to the slider's ceiling
  const byStrength = [0.1, 0.5, 1, 2, 4, 6].map(g => run(g, 0));
  for (let i = 1; i < byStrength.length; i++) {
    assert.ok(byStrength[i] <= byStrength[i - 1] + 8,
      `raising gravity loosened the field: ${byStrength.map(v => v.toFixed(0)).join(' -> ')}`);
  }
  // ...and it must actually cluster, not merely fail to expand
  assert.ok(byStrength[byStrength.length - 1] < byStrength[0] / 4,
    `full strength barely clustered: ${byStrength[0].toFixed(0)} -> ${byStrength.at(-1).toFixed(0)}`);
  // The core repulsion has to hold the blob OPEN. Without it the tightest setting lands
  // near 1px RMS — every node on one pixel — where with it the field rests around 12px.
  // Checking the minimum across the whole range, not just the end, because the collapse
  // shows up first in the middle of the slider.
  const tightest = Math.min(...byStrength);
  assert.ok(tightest > 6,
    `field collapsed to a dot (tightest ${tightest.toFixed(1)}px RMS) — core repulsion is not holding it open`);

  // more anti-gravity must mean a looser field, monotonically
  const byMix = [0, 0.25, 0.5, 0.75, 1].map(a => run(2, a));
  for (let i = 1; i < byMix.length; i++) {
    assert.ok(byMix[i] >= byMix[i - 1] - 8,
      `raising the anti-gravity share tightened the field: ${byMix.map(v => v.toFixed(0)).join(' -> ')}`);
  }
  assert.ok(byMix.at(-1) > byMix[0] * 4, 'an all-anti-gravity field should end far more spread out than an all-gravity one');
}

console.log('constellation-gravity: all assertions passed');
