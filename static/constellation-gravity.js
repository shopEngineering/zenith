// N-body step for the constellation nodes. Pure (no canvas, no settings) so the physics
// can be unit-tested and benchmarked outside the browser.
//
// MODEL. Every node carries a signed polarity g in [-1, 1] and a mass gm > 0.
//
// POLARITY decides direction: a pair ATTRACTS only when both g are positive, otherwise it
// REPELS. That is a rule rather than signed-mass electrostatics, and deliberately so: with
// signed mass, two anti-gravity nodes attract each other, so sliding the mix to 100% anti
// would collapse into a blob indistinguishable from 0% and the control would be useless in
// the middle. This way the mix runs monotonically from one blob to an even spread.
// |g| scales the magnitude, so an oscillating node crossing zero fades its influence out
// and back in with the opposite sign rather than flipping discontinuously.
//
// MASS enters twice, as real mass does: a heavier node pulls harder AND resists being
// moved. So the forces on a pair stay equal and opposite while the accelerations do not,
// and a few heavy nodes behave like anchors with lighter ones swarming around them.
// Momentum -- sum of gm*v, not sum of v -- is therefore still exactly conserved.
//
// The force itself is honest: softened inverse-square, every pair accumulated once.
(function (root) {
  // Softening length. Without it two nodes that drift close see a force approaching
  // infinity and get flung off screen in a single frame; this caps the force at a
  // finite value inside `soft` pixels without changing the far field.
  const SOFT = 18;
  const SOFT2 = SOFT * SOFT;

  // Short-range repulsion, falling off as 1/r^4 so it is negligible far out and dominant
  // close in. Without it there is no equilibrium: attraction alone has nothing to push
  // back, so any strength above a whisper ends with every node stacked on one pixel —
  // the difference between settings being only how fast you get to that dot. With it,
  // a pair rests where g/r^2 == CORE/r^4, i.e. r = sqrt(CORE/g): raising the strength
  // tightens the spacing instead of destroying it, which is what a blob should do.
  const CORE = 150000;

  // Applies one step of pairwise acceleration to nodes[].{vx,vy}, then damps and caps.
  //   g     force constant (already scaled for screen pixels)
  //   damp  per-frame velocity retention; < 1 lets a cluster settle instead of boiling
  //   maxv  hard speed ceiling in px/frame, so nothing ever teleports across the screen
  // Returns the number of pairs evaluated (handy for benchmarking).
  function stepGravity(nodes, g, damp, maxv) {
    const n = nodes ? nodes.length : 0;
    if (!(g > 0) || n < 2) return 0;
    let pairs = 0;
    for (let i = 0; i < n; i++) {
      const p = nodes[i];
      const pm = p.gm > 0 ? p.gm : 1, pg = p.g === undefined ? 1 : p.g;
      for (let j = i + 1; j < n; j++) {
        const q = nodes[j];
        const qm = q.gm > 0 ? q.gm : 1, qg = q.g === undefined ? 1 : q.g;
        const dx = q.x - p.x, dy = q.y - p.y;
        const d2 = dx * dx + dy * dy + SOFT2;
        const inv = 1 / Math.sqrt(d2);
        // attract only if BOTH pull; one anti-gravity node in the pair makes it push
        const s = (pg > 0 && qg > 0) ? 1 : -1;
        // positive f pulls p toward q. The core term is always repulsive, for every pair,
        // because it stands in for volume rather than for charge.
        const f = (g * s * Math.abs(pg) * Math.abs(qg) / d2 - CORE / (d2 * d2)) * pm * qm;
        const ux = dx * inv, uy = dy * inv;
        // F is equal and opposite; a = F/m is not. Heavy nodes therefore act as anchors,
        // and sum(gm*v) -- the actual momentum -- still comes out unchanged.
        p.vx += ux * f / pm; p.vy += uy * f / pm;
        q.vx -= ux * f / qm; q.vy -= uy * f / qm;
        pairs++;
      }
    }
    for (let i = 0; i < n; i++) {
      const p = nodes[i];
      p.vx *= damp; p.vy *= damp;
      const sp = Math.sqrt(p.vx * p.vx + p.vy * p.vy);
      if (sp > maxv) { const k = maxv / sp; p.vx *= k; p.vy *= k; }
    }
    return pairs;
  }

  const api = { stepGravity, SOFT };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else Object.assign(root, api);
})(typeof window !== 'undefined' ? window : globalThis);
