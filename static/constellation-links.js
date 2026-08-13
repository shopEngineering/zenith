// Spatial-hash pair finder for the constellations effect. No canvas deps so the
// neighbourhood walk stays unit-testable: a dropped neighbour or a double-counted cell
// shows up only as slightly wrong line density on screen, which is easy to ship and
// very hard to spot by eye.
(function (root) {
  const FORWARD = [[1, 0], [-1, 1], [0, 1], [1, 1]];   // E, SW, S, SE

  // Calls fn(p, q, distance) once for every pair of nodes closer together than `link`.
  // Nodes go into link-sized cells, so a pair can only exist within the 3x3
  // neighbourhood; each cell tests itself plus the four FORWARD neighbours, which
  // visits every adjacent pair of cells exactly once and so never yields a pair twice.
  // Brute force is the same work when `link` approaches the canvas size (one big cell),
  // and far less as soon as it does not — which is the case that makes a high node
  // count affordable at all.
  function eachLinkedPair(nodes, link, w, h, fn) {
    if (!(link > 0) || !nodes || nodes.length < 2) return;
    const cols = Math.max(1, Math.ceil(w / link)), rows = Math.max(1, Math.ceil(h / link));
    const grid = new Array(cols * rows);
    for (const p of nodes) {
      // clamp: a node that has drifted off-canvas still belongs to an edge cell, and
      // the exact distance test below keeps the result correct either way
      const cx = Math.min(cols - 1, Math.max(0, Math.floor(p.x / link)));
      const cy = Math.min(rows - 1, Math.max(0, Math.floor(p.y / link)));
      const i = cy * cols + cx;
      (grid[i] || (grid[i] = [])).push(p);
    }
    const lim = link * link;
    const test = (p, q) => {
      const dx = p.x - q.x, dy = p.y - q.y, d2 = dx * dx + dy * dy;
      if (d2 <= lim) fn(p, q, Math.sqrt(d2));   // sqrt only for pairs that actually link
    };
    for (let cy = 0; cy < rows; cy++) {
      for (let cx = 0; cx < cols; cx++) {
        const cell = grid[cy * cols + cx];
        if (!cell) continue;
        for (let i = 0; i < cell.length; i++) {
          for (let j = i + 1; j < cell.length; j++) test(cell[i], cell[j]);
        }
        for (let n = 0; n < FORWARD.length; n++) {
          const nx = cx + FORWARD[n][0], ny = cy + FORWARD[n][1];
          if (nx < 0 || nx >= cols || ny < 0 || ny >= rows) continue;
          const other = grid[ny * cols + nx];
          if (!other) continue;
          for (const p of cell) for (const q of other) test(p, q);
        }
      }
    }
  }
  const api = { eachLinkedPair };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else Object.assign(root, api);
})(typeof window !== 'undefined' ? window : globalThis);
