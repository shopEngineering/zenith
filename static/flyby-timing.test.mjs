import assert from 'node:assert';
import { createRequire } from 'node:module';
const { flybyDue, flybyPick } = createRequire(import.meta.url)('./flyby-timing.js');

// --- flybyDue: the wait is scaled by the CURRENT rate, not the one at spawn ------
// 30s into a 60s gap at 1x: not yet
assert.strictEqual(flybyDue(30, 0, 60, 1, false, 0, 3), false);
// the SAME pending wait at 4x is already overdue — this is the bug that made
// 'How often' look dead: it used to divide once, at scheduling time
assert.strictEqual(flybyDue(30, 0, 60, 4, false, 0, 3), true);
// and dropping the rate pushes it back out again
assert.strictEqual(flybyDue(30, 0, 60, 0.5, false, 0, 3), false);
// rate is floored at 0.25 so a 0 or negative rate can never divide by zero
assert.strictEqual(flybyDue(239, 0, 60, 0, false, 0, 3), false);
assert.strictEqual(flybyDue(241, 0, 60, 0, false, 0, 3), true);

// force jumps the queue regardless of how much of the wait is left
assert.strictEqual(flybyDue(0, 0, 60, 1, true, 0, 3), true);
assert.strictEqual(flybyDue(0, 0, 60, 1, 'whale', 0, 3), true);
// ...but never past the on-screen cap
assert.strictEqual(flybyDue(0, 0, 60, 1, true, 3, 3), false);
assert.strictEqual(flybyDue(999, 0, 60, 1, false, 3, 3), false);

// --- flybyPick: a forced request wins, and degrades to random when stale --------
const pool = [{ kind: 'whale' }, { kind: 'comet' }, { kind: 'img', img: { id: 'c7' } }];
assert.strictEqual(flybyPick(pool, 'whale', 0.9).kind, 'whale');   // by cast key
assert.strictEqual(flybyPick(pool, 'c7', 0.0).img.id, 'c7');       // by custom sprite id
// nothing asked for -> roll decides
assert.strictEqual(flybyPick(pool, true, 0).kind, 'whale');
assert.strictEqual(flybyPick(pool, false, 0.99).kind, 'img');
// asked for one that has since been switched off -> fall back, never return undefined
assert.strictEqual(flybyPick(pool, 'bunny', 0.5).kind, 'comet');

console.log('flyby-timing: all assertions passed');
