import assert from 'node:assert';
import { createRequire } from 'node:module';
const { logicalLineAt, indexOfCell, scanLine, hitAt } =
  createRequire(import.meta.url)('./term-links.js');

const URL_RE = /https?:\/\/[^\s)>\]"'`]+/gi;
const PATS = [{ re: URL_RE, kind: 'url' }];

// Build a row of `cols` cells from a string. A char listed in `wide` occupies TWO cells,
// the second being null — exactly what xterm reports for emoji/CJK.
const row = (s, cols, isWrapped = false, wide = '') => {
  const cells = [];
  for (const ch of s) {
    cells.push(ch);
    if (wide.includes(ch)) cells.push(null);
  }
  while (cells.length < cols) cells.push(' ');
  return { cells: cells.slice(0, cols), isWrapped };
};
const reader = rows => r => rows[r] || null;
const scanAt = (rows, at) => {
  const { text, map, first } = logicalLineAt(reader(rows), at, rows.length);
  return { text, map, first, hits: scanLine(text, map, PATS) };
};

const URL = 'https://example.com/a/very/long/path?q=1&r=2';

// --- auto-wrap: the whole URL is returned from ANY row it crosses --------------------
// Rows 0 and 1 are exactly `cols` wide — a real auto-wrapped row always is, which is
// precisely why reconstruction has to preserve cell counts rather than string lengths.
{
  const cols = 20;
  const rows = [
    row('see https://example.', cols),      // 20 cells
    row('com/a/very/long/path', cols, true),// 20 cells
    row('?q=1&r=2 ok', cols, true),
  ];
  for (const at of [0, 1, 2]) {
    const { hits } = scanAt(rows, at, cols);
    assert.strictEqual(hits.length, 1, `expected one link clicking row ${at}`);
    assert.strictEqual(hits[0].raw, URL, `clicking row ${at} gave a truncated link: ${hits[0].raw}`);
  }
}

// --- the reported bug: clicking a middle row must not yield only that row's fragment --
{
  const cols = 20;
  const rows = [row('https://example.com/', cols), row('a/b/c.html', cols, true)];
  const { hits } = scanAt(rows, 1, cols);
  assert.strictEqual(hits[0].raw, 'https://example.com/a/b/c.html');
  assert.notStrictEqual(hits[0].raw, 'a/b/c.html');
}

// --- wide characters: padding a row to `cols` by STRING length breaks the seam --------
// The row below holds an emoji, so it has fewer characters than cells. Padding to `cols`
// would insert a space right where the URL continues and split it in two.
{
  const cols = 20;
  const rows = [
    row('✅ https://example.c', cols, false, '✅'),   // emoji = 2 cells, so 19 chars fill 20
    row('om/x.md done', cols, true),
  ];
  const { hits } = scanAt(rows, 0, cols);
  assert.strictEqual(hits.length, 1);
  assert.strictEqual(hits[0].raw, 'https://example.com/x.md',
    `wide char corrupted the join: ${hits[0].raw}`);
}

// --- hard wrap (TUI redraw): no isWrapped flag, joined only when flush at the seam ----
{
  const cols = 20;
  const joined = [row('https://example.com/', cols), row('a/b/c.html', cols)];   // both isWrapped=false
  assert.strictEqual(scanAt(joined, 0, cols).hits[0].raw, 'https://example.com/a/b/c.html');

  // a row that stops short of its right margin cannot have been hard-wrapped
  const spaced = [row('https://example.com ', cols), row('a/b/c.html', cols)];
  assert.strictEqual(scanAt(spaced, 0).hits[0].raw, 'https://example.com',
    'a row that does not reach its last cell must not be joined to the next');

  // ...but an INDENTED continuation must still join. A TUI wrapping inside its own layout
  // lines the next row up under the first, so it never starts at column zero.
  const indented = [row('https://example.com/', cols), row('    a/b/c.html', cols)];
  assert.strictEqual(scanAt(indented, 0).hits[0].raw, 'https://example.com/a/b/c.html',
    'an indented hard-wrapped continuation must be joined, indent dropped');

  // Prose is left alone. This row DOES reach its right margin — the case that would glue
  // if reaching the margin were the only test — but the token against it has no '/'.
  const head = 'detached this time (PPID 1, 10-hour';
  const prose = [row(head, head.length), row('  timeout, so it will not idle', head.length)];
  assert.strictEqual(head.length, 35, 'fixture must fill the row exactly or it tests nothing');
  const p = logicalLineAt(reader(prose), 0, prose.length);
  assert.ok(!p.text.includes('10-hourtimeout'),
    `prose was glued across the wrap: ${JSON.stringify(p.text.slice(20, 50))}`);

  // and it can be switched off entirely
  const { text } = logicalLineAt(reader(joined), 0, joined.length, { hardWrap: false });
  assert.ok(!text.includes('c.html'), 'hardWrap:false must stop at the row boundary');
}

// --- cell mapping stays exact across a wide char, so the hover box lands right --------
{
  const cols = 20;
  const rows = [row('✅ https://ex.com/a', cols, false, '✅')];
  const { text, map, hits } = scanAt(rows, 0, cols);
  assert.strictEqual(hits.length, 1);
  // the emoji ate two cells, so the URL starts at cell 3, not at string index 2
  assert.strictEqual(hits[0].from.col, 3, `link starts at the wrong cell: ${hits[0].from.col}`);
  assert.strictEqual(text[hits[0].start], 'h');
  // every mapped character round-trips back to its own index
  for (let i = 0; i < map.length; i++) {
    assert.strictEqual(indexOfCell(map, map[i].row, map[i].col), i);
  }
  assert.strictEqual(indexOfCell(map, 0, 1), -1, 'a wide char\'s second cell holds no character');
}

// --- clicking any cell of a wrapped link resolves to the same whole link --------------
{
  const cols = 20;
  const rows = [row('go https://example.c', cols), row('om/deep/link.md now', cols, true)];
  const { map, hits } = scanAt(rows, 0, cols);
  const seen = new Set();
  for (const h of hits) {
    for (let i = h.start; i <= h.end; i++) {
      const cell = map[i];
      const idx = indexOfCell(map, cell.row, cell.col);
      const got = hitAt(hits, idx);
      assert.ok(got, `cell (${cell.row},${cell.col}) inside the link resolved to nothing`);
      seen.add(got.raw);
    }
  }
  assert.deepStrictEqual([...seen], ['https://example.com/deep/link.md']);
  // a cell outside the link resolves to nothing
  assert.strictEqual(hitAt(hits, indexOfCell(map, 0, 0)), null);
}

// --- the reported case, taken from a real screenshot ---------------------------------
// Claude's TUI hard-wrapped a tailnet URL and indented the continuation under the first
// row. The first row reaches the right margin; the second starts with the block's indent.
{
  const cols = 66;
  const URL2 = 'http://mini.taile3fe07.ts.net:52071/?key=e21abde58d6787f8dc43fbafd3c83c032040e6b5a856a125f771651958ead579';
  const head = '  ' + URL2.slice(0, cols - 2);          // fills the row exactly
  const tail = '  ' + URL2.slice(cols - 2);             // indented continuation
  const rows = [row(head, cols), row(tail, cols)];
  for (const at of [0, 1]) {
    const { hits } = scanAt(rows, at);
    assert.strictEqual(hits.length, 1, `no link found clicking row ${at}`);
    assert.strictEqual(hits[0].raw, URL2,
      `row ${at} gave a truncated link:\n  got  ${hits[0].raw}\n  want ${URL2}`);
  }
  // and the key that was being cut off is actually present
  assert.ok(scanAt(rows, 0).hits[0].raw.endsWith('ead579'));
}

// --- a URL ending in a file extension must open as a URL, not as a local file ---------
// PATH_RE also matches the tail of such a URL, so whichever pattern is listed first wins.
{
  const PATH_RE = /((?:\/|~\/|\.{1,2}\/|[\w.\-]*\/)[\w.\-/]*[\w\-]\.(?:md|html?|json|py|txt))(?::\d+(?::\d+)?)?/gi;
  const cols = 44;
  const rows = [row('see https://example.com/docs/readme.md here', cols)];
  const { text, map } = logicalLineAt(reader(rows), 0, rows.length);

  const urlFirst = scanLine(text, map, [{ re: URL_RE, kind: 'url' }, { re: PATH_RE, kind: 'path' }]);
  assert.strictEqual(urlFirst.length, 1, 'the path match must be dropped as overlapping');
  assert.strictEqual(urlFirst[0].kind, 'url');
  assert.strictEqual(urlFirst[0].raw, 'https://example.com/docs/readme.md');

  // a genuine local path on the same line is still found
  const both = [row('open ./docs/spec.md and https://x.io/a.md now', cols)];
  const r2 = logicalLineAt(reader(both), 0, both.length);
  const hits = scanLine(r2.text, r2.map, [{ re: URL_RE, kind: 'url' }, { re: PATH_RE, kind: 'path' }]);
  assert.deepStrictEqual(hits.map(h => h.kind).sort(), ['path', 'url']);
  assert.ok(hits.find(h => h.kind === 'path').raw.includes('spec.md'));
  assert.strictEqual(hits.find(h => h.kind === 'url').raw, 'https://x.io/a.md');
}

// --- degenerate input is a no-op, never a throw ---------------------------------------
{
  assert.deepStrictEqual(logicalLineAt(() => null, 0, 0), { text: '', map: [], first: 0 });
  assert.deepStrictEqual(logicalLineAt(() => null, 5, 10).text, '');
  const empty = [row('', 10)];
  assert.deepStrictEqual(scanAt(empty, 0, 10).hits, []);
}

// --- walking back stops at the buffer start even if row 0 claims to be wrapped --------
{
  const cols = 10;
  const rows = [row('https://a.', cols, true), row('com/b.md', cols, true)];
  const { first, hits } = scanAt(rows, 1, cols);
  assert.strictEqual(first, 0);
  assert.strictEqual(hits[0].raw, 'https://a.com/b.md');
}

console.log('term-links: all assertions passed');
