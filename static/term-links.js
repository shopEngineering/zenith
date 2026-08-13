// Reconstructing the logical line under the cursor in a terminal buffer, so a URL or path
// split across rows is matched and opened WHOLE. Pure (no xterm, no DOM) so the index
// arithmetic — the part that silently truncates a link rather than failing loudly — is
// testable under node.
//
// A row is passed in as `{ cells, isWrapped }`, where `cells` has ONE ENTRY PER CELL:
// a string for a cell that holds a character, or null for the second cell of a
// double-width character. That shape matters. The obvious approach — take each row's
// string and pad it to `cols` — assumes one character per cell, which is false the moment
// output contains an emoji, CJK, or box drawing. The padding then lands in the middle of a
// reconstructed line and cuts the URL in half exactly where the rows join.
//
// Two ways a line continues onto the next row, and they need different handling:
//
//   AUTO-WRAP  the terminal itself ran out of columns. xterm marks the next row
//              `isWrapped`, so this is unambiguous.
//   HARD-WRAP  a full-screen app (Claude's TUI, vim) positioned the cursor and drew each
//              row itself. Nothing marks the rows as related, so continuation has to be
//              inferred: the previous row reaches its last cell and the next row starts in
//              its first, with no space at the seam. Prose that happens to fill the width
//              can trip this, which is why the join is only ever used to FIND a match —
//              text that does not look like a URL or path is unaffected either way.
(function (root) {
  const isBlank = c => c === null || c === undefined || c === '' || c === ' ';

  // The run of non-blank cells ending at the row's LAST cell — empty when the row does not
  // reach its right margin. A hard wrap can only have happened if it does.
  const tailToken = row => {
    const cells = row.cells;
    let i = cells.length - 1;
    while (i >= 0 && cells[i] === null) i--;
    if (i < 0 || isBlank(cells[i])) return '';
    let out = '';
    for (; i >= 0; i--) {
      const c = cells[i];
      if (c === null) continue;
      if (isBlank(c)) break;
      out = c + out;
    }
    return out;
  };
  // index of the first cell holding a non-blank character, or -1
  const firstContent = row => {
    for (let i = 0; i < row.cells.length; i++) {
      const c = row.cells[i];
      if (c === null) continue;
      if (!isBlank(c)) return i;
    }
    return -1;
  };
  // Does a hard-wrapped row continue onto the next one? Two conditions, and the second is
  // what keeps this from gluing ordinary prose together. The row must reach its right
  // margin, AND the token sitting against that margin must contain a '/' — the thing a URL
  // or path has and a word at the end of a sentence does not. The continuation may be
  // INDENTED: a TUI that wraps inside its own layout lines the next row up under the first,
  // so requiring column zero (a real terminal wrap) misses exactly the case this is for.
  const continuesInto = (prev, next) => {
    const tail = tailToken(prev);
    return tail.includes('/') && firstContent(next) >= 0;
  };

  // Reconstruct the logical line containing row `row0`.
  //   readRow(r) -> { cells, isWrapped } | null
  // Returns { text, map, first } where map[i] = {row, col} for text[i] — an exact
  // character->cell mapping, so a match can be pointed back at real screen coordinates
  // whatever widths the row contains.
  function logicalLineAt(readRow, row0, maxRow, opts) {
    const hard = !opts || opts.hardWrap !== false;
    // Walk back to the row the line STARTS on. This has to follow hard wraps as well as
    // auto-wraps, or clicking the tail of a link finds only the fragment under the cursor
    // — which is the whole complaint this exists to answer.
    let first = row0;
    while (first > 0) {
      const cur = readRow(first);
      if (!cur) break;
      if (cur.isWrapped) { first--; continue; }
      const above = readRow(first - 1);
      if (hard && above && continuesInto(above, cur)) { first--; continue; }
      break;
    }
    const chars = [], map = [];
    let prev = null;
    for (let r = first; r < maxRow; r++) {
      const row = readRow(r);
      if (!row) break;
      let from = 0;
      if (r > first && !row.isWrapped) {
        if (!hard || !continuesInto(prev, row)) break;
        from = firstContent(row);   // drop the continuation's indent, or it splits the link
      }
      for (let c = from; c < row.cells.length; c++) {
        const ch = row.cells[c];
        if (ch === null) continue;            // second cell of a wide char: not its own character
        chars.push(ch === '' ? ' ' : ch);
        map.push({ row: r, col: c });
      }
      prev = row;
    }
    return { text: chars.join(''), map, first };
  }

  // Character index of a given cell, or -1 when that cell holds no character (blank tail
  // of a row, or a wide char's second cell).
  function indexOfCell(map, row, col) {
    for (let i = 0; i < map.length; i++) if (map[i].row === row && map[i].col === col) return i;
    return -1;
  }

  // All matches for `patterns` ([{re, kind}, ...]) in the reconstructed text, each carrying
  // the cells its first and last characters occupy. `re` must be global.
  //
  // Patterns are tried IN ORDER and an overlapping match from a later pattern is dropped,
  // so the caller resolves ambiguity by listing the more specific pattern first. This is
  // load-bearing: a file-path pattern also matches the tail of a web URL that ends in an
  // extension (`https://example.com/readme.md` contains `//example.com/readme.md`), and
  // whichever wins decides between opening a browser tab and hunting for a local file.
  function scanLine(text, map, patterns) {
    const out = [];
    for (const { re, kind } of patterns) {
      re.lastIndex = 0;
      for (const m of text.matchAll(re)) {
        const start = m.index, end = start + m[0].length - 1;
        if (!map[start] || !map[end]) continue;
        if (out.some(h => start <= h.end && end >= h.start)) continue;   // claimed already
        out.push({ raw: m[0], kind, start, end, from: map[start], to: map[end] });
      }
    }
    return out;
  }

  const hitAt = (hits, idx) => hits.find(h => idx >= h.start && idx <= h.end) || null;

  const api = { logicalLineAt, indexOfCell, scanLine, hitAt, tailToken, firstContent, continuesInto };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else Object.assign(root, api);
})(typeof window !== 'undefined' ? window : globalThis);
