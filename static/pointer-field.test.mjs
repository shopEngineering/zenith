import assert from 'node:assert';
import { createRequire } from 'node:module';
const { pointerForce, springStep, agePointer } = createRequire(import.meta.url)('./pointer-field.js');

const R = 200, S = 1;

// --- direction per mode --------------------------------------------------------------
{
  // a point to the RIGHT of the cursor
  const push = pointerForce(0, 0, 50, 0, R, 'push', S);
  assert.ok(push.fx > 0 && Math.abs(push.fy) < 1e-9, 'push must drive it further right');
  const pull = pointerForce(0, 0, 50, 0, R, 'pull', S);
  assert.ok(pull.fx < 0, 'pull must draw it back toward the cursor');
  const swirl = pointerForce(0, 0, 50, 0, R, 'swirl', S);
  assert.ok(Math.abs(swirl.fx) < 1e-9 && swirl.fy > 0, 'swirl must act perpendicular, not radially');
  // swirl does no radial work, which is what keeps it an orbit rather than a vacuum
  assert.ok(Math.abs(swirl.fx * 1 + swirl.fy * 0) < 1e-9, 'swirl must have no radial component');
}

// --- the radius is a hard edge, and the falloff reaches it smoothly -------------------
{
  assert.deepStrictEqual(pointerForce(0, 0, R + 1, 0, R, 'push', S), { fx: 0, fy: 0 },
    'nothing outside the radius may feel anything');
  const edge = pointerForce(0, 0, R - 0.5, 0, R, 'push', S);
  assert.ok(Math.hypot(edge.fx, edge.fy) < 1e-4,
    'force must arrive at zero AT the radius, or elements jerk as they cross it');
  const near = pointerForce(0, 0, 20, 0, R, 'push', S);
  const far = pointerForce(0, 0, 150, 0, R, 'push', S);
  assert.ok(Math.hypot(near.fx, near.fy) > Math.hypot(far.fx, far.fy), 'closer must mean stronger');
}

// --- the centre is finite: passing the cursor over a node must not fling it -----------
{
  for (const d of [0, 0.001, 0.01, 0.5]) {
    const f = pointerForce(0, 0, d, 0, R, 'push', S);
    assert.ok(Number.isFinite(f.fx) && Number.isFinite(f.fy), `non-finite force at d=${d}`);
    assert.ok(Math.hypot(f.fx, f.fy) <= S * 4, `force blew up at d=${d}: ${f.fx}`);
  }
}

// --- strength scales linearly, and zero/degenerate input is a no-op -------------------
{
  const a = pointerForce(0, 0, 50, 0, R, 'push', 1);
  const b = pointerForce(0, 0, 50, 0, R, 'push', 3);
  assert.ok(Math.abs(b.fx / a.fx - 3) < 1e-9, 'strength must scale the force linearly');
  for (const bad of [[R, 'push', 0], [0, 'push', S], [-5, 'push', S]]) {
    assert.deepStrictEqual(pointerForce(0, 0, 10, 0, bad[0], bad[1], bad[2]), { fx: 0, fy: 0 });
  }
}

// --- the spring returns to rest, which is what lets an effect be restored exactly -----
{
  const el = {};
  springStep(el, 12, -8, 0.2, 0.8, 999);          // one shove
  assert.ok(el.ox !== 0 || el.oy !== 0, 'the shove should have displaced it');
  for (let i = 0; i < 400; i++) springStep(el, 0, 0, 0.2, 0.8, 999);   // then let go
  assert.ok(Math.hypot(el.ox, el.oy) < 1e-3,
    `displacement must decay to nothing, left at (${el.ox}, ${el.oy})`);
  assert.ok(Math.hypot(el.dvx, el.dvy) < 1e-3, 'and come to rest, not keep ringing');
}

// --- the leash caps displacement however hard or long it is pushed -------------------
{
  const el = {};
  for (let i = 0; i < 500; i++) springStep(el, 100, 100, 0.05, 0.95, 40);
  assert.ok(Math.hypot(el.ox, el.oy) <= 40 + 1e-6,
    `leash breached: ${Math.hypot(el.ox, el.oy)}`);
  assert.ok(Number.isFinite(el.ox) && Number.isFinite(el.oy));
}

// --- a stiffer spring settles nearer home under the same steady push -----------------
{
  const soft = {}, stiff = {};
  for (let i = 0; i < 300; i++) { springStep(soft, 2, 0, 0.05, 0.9, 999); springStep(stiff, 2, 0, 0.4, 0.9, 999); }
  assert.ok(Math.abs(stiff.ox) < Math.abs(soft.ox), 'a stiffer spring must hold closer to rest');
}

// --- pointer ageing: velocity decays, idle accumulates -------------------------------
{
  const p = { x: 0, y: 0, vx: 100, vy: -50, idle: 0, inside: true };
  agePointer(0.5, p);
  assert.ok(Math.abs(p.vx) < 100 && Math.abs(p.vy) < 50, 'velocity must decay while idle');
  assert.ok(p.idle > 0, 'idle must accumulate');
  for (let i = 0; i < 100; i++) agePointer(0.1, p);
  assert.ok(Math.hypot(p.vx, p.vy) < 1e-6, 'a stopped cursor must stop dragging things');
}

console.log('pointer-field: all assertions passed');
