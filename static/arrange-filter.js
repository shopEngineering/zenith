// Pure include-filter for the auto-arrange views. No DOM deps so it is unit-testable.
// A window with arrange === false is excluded UNLESS the view opts to include off windows.
(function (root) {
  function filterForArrange(wins, includeOff) {
    return includeOff ? wins.slice() : wins.filter(w => w.arrange !== false);
  }
  // When a tiled window leaves (popped out, closed, minimized), should the
  // survivors close the gap? FREE drag mode keeps the void on purpose, and
  // cascade has no slots to reclaim. Pure, so the guards stay testable.
  function shouldReflowGap(arrangeMode, dragMode) {
    return (dragMode || 'reflow') === 'reflow'
      && (arrangeMode || 'tiled') !== 'cascade';
  }
  const api = { filterForArrange, shouldReflowGap };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else Object.assign(root, api);
})(typeof window !== 'undefined' ? window : globalThis);
