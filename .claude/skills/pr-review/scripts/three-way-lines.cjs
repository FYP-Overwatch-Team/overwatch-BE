'use strict';

const { git, repoRoot, takeOption } = require('./lib/source.cjs');
const { lineCounts, readAt } = require('./lib/lines.cjs');

const usage = () => {
  console.error([
    'Line-count audit of a merge: for every file either side changed, each line should appear',
    'ours + theirs - base times in the result. Flags LOST lines (dropped by a resolution) and EXTRA',
    'lines (duplicated, e.g. a describe block auto-merged twice).',
    '',
    '  node three-way-lines.cjs <base> <ours> <theirs> [--merged <rev>]',
    '',
    'During a merge: base=$(git merge-base HEAD MERGE_HEAD) ours=HEAD theirs=MERGE_HEAD, result = working tree.',
    'After committing: ours=<merge>^1 theirs=<merge>^2 --merged <merge>.',
    'Expect some noise where both sides made the same edit or a resolution deliberately rewrote lines.',
  ].join('\n'));
  process.exit(2);
};

let args = process.argv.slice(2);
if (args.includes('--help')) { usage(); }
const mergedOpt = takeOption(args, '--merged');
args = mergedOpt.rest;
if (args.length !== 3) { usage(); }
const [base, ours, theirs] = args;

const root = repoRoot();
const names = (a, b) =>
  git(root, ['diff', '--name-only', a, b]).split(/\r?\n/).filter(Boolean);
const files = [...new Set([...names(base, ours), ...names(base, theirs)])]
  .sort();

let flagged = 0;
for (const file of files) {
  const b = lineCounts(readAt(root, base, file));
  const o = lineCounts(readAt(root, ours, file));
  const t = lineCounts(readAt(root, theirs, file));
  const m = lineCounts(readAt(root, mergedOpt.value, file));
  const rows = [];
  for (
    const key of new Set([...b.keys(), ...o.keys(), ...t.keys(), ...m.keys()])
  ) {
    const cb = b.get(key) || 0;
    const co = o.get(key) || 0;
    const ct = t.get(key) || 0;
    const cm = m.get(key) || 0;
    const expected = Math.max(0, co + ct - cb);
    if (cm !== expected) { rows.push({ key, cb, co, ct, cm, expected }); }
  }
  if (rows.length) {
    flagged += rows.length;
    console.log(`\n### ${file}  (${rows.length})`);
    for (const r of rows) {
      console.log(
        `  ${
          r.cm < r.expected ? 'LOST ' : 'EXTRA'
        } base${r.cb} ours${r.co} theirs${r.ct} merged${r.cm} expected${r.expected} | ${
          r.key.slice(0, 140)
        }`,
      );
    }
  }
}
console.log(`\nfiles: ${files.length}; divergent line keys: ${flagged}`);
process.exit(flagged ? 1 : 0);
