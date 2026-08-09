// Pure flyby scheduling. No canvas or DOM deps so the timing stays unit-testable.
(function (root) {
  // Is the next flyby due? The wait is stored UNSCALED and divided by the CURRENT rate
  // here rather than at spawn, so moving 'How often' retimes a wait already in progress
  // instead of only the one after it. `force` is the settings panel asking for one right
  // away so a slider change proves itself — it still respects the on-screen cap.
  function flybyDue(now, since, gap, rate, force, flying, maxOn) {
    if (flying >= maxOn) return false;
    return !!force || now - since > gap / Math.max(0.25, rate);
  }
  // Which sprite to send across: the one the panel asked for by cast key or sprite id,
  // falling back to a random pick if it is gone or was switched off in the meantime.
  function flybyPick(pool, want, roll) {
    const forced = typeof want === 'string'
      ? pool.find(p => p.kind === want || (p.img && p.img.id === want)) : null;
    return forced || pool[(roll * pool.length) | 0];
  }
  const api = { flybyDue, flybyPick };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else Object.assign(root, api);
})(typeof window !== 'undefined' ? window : globalThis);
