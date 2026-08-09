import assert from 'node:assert';
import { createRequire } from 'node:module';
const { filterForArrange, shouldReflowGap } = createRequire(import.meta.url)('./arrange-filter.js');

const wins = [{ id: 'a', arrange: true }, { id: 'b', arrange: false }, { id: 'c' }];

// includeOff = false -> drop arrange:false, keep undefined (defaults to included)
assert.deepStrictEqual(filterForArrange(wins, false).map(w => w.id), ['a', 'c']);
// includeOff = true -> keep everything
assert.deepStrictEqual(filterForArrange(wins, true).map(w => w.id), ['a', 'b', 'c']);
// returns a copy, never the original array
assert.notStrictEqual(filterForArrange(wins, true), wins);

// --- shouldReflowGap: closing/minimizing a tiled window frees a slot ---------
// reflow mode reclaims the space in every slotted arrangement
assert.strictEqual(shouldReflowGap('columns', 'reflow'), true);
assert.strictEqual(shouldReflowGap('tiled', 'reflow'), true);
// FREE drag mode leaves the void on purpose
assert.strictEqual(shouldReflowGap('columns', 'free'), false);
// cascade has no slots to reclaim
assert.strictEqual(shouldReflowGap('cascade', 'reflow'), false);
assert.strictEqual(shouldReflowGap('cascade', 'free'), false);
// unset settings fall back to the defaults (tiled + reflow) -> reclaim
assert.strictEqual(shouldReflowGap(undefined, undefined), true);
assert.strictEqual(shouldReflowGap('columns', undefined), true);
assert.strictEqual(shouldReflowGap(undefined, 'free'), false);

console.log('arrange-filter: all assertions passed');
