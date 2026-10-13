'use strict';

const fs = require('node:fs');
const { git, repoRoot, takeOption } = require('./lib/source.cjs');
const { norm, patchFiles, readAt, trivial } = require('./lib/lines.cjs');

const usage = () => {
  console.error([
    'Check that every line a change added still exists after a merge (catches work silently lost in conflict resolution).',
    '',
    '  node preserved-lines.cjs --diff <from> <to> [--in <rev>] [--label <name>]',
    '  node preserved-lines.cjs --patch <file.patch> [--in <rev>] [--label <name>]',
    '',
    "--diff takes the patch from `git diff <from> <to>`, e.g. a PR's own delta (merge-base..head) or a squash commit (<sha>^ <sha>).",
    '--in checks a commit instead of the working tree. Lines are compared whitespace-normalised, as a multiset, per file.',
    'Every reported line is either an intended supersession or a loss; classify each one.',
  ].join('\n'));
  process.exit(2);
};

let args = process.argv.slice(2);
if (args.length === 0 || args.includes('--help')) { usage(); }
const inOpt = takeOption(args, '--in');
args = inOpt.rest;
const labelOpt = takeOption(args, '--label');
args = labelOpt.rest;
const label = labelOpt.value || 'check';

const root = repoRoot();
let patchText;
if (args[0] === '--diff' && args.length === 3) {
  patchText = git(root, ['diff', args[1], args[2]]);
} else if (args[0] === '--patch' && args.length === 2) {
  patchText = fs.readFileSync(args[1], 'utf8');
} else {
  usage();
}

const { added } = patchFiles(patchText);
let totalMissing = 0;
for (const [file, lines] of added) {
  const text = readAt(root, inOpt.value, file);
  if (text === null) {
    console.log(
      `[${label}] ${file}: FILE NOT PRESENT (${lines.length} added line(s))`,
    );
    totalMissing += lines.length;
    continue;
  }
  const have = new Map();
  for (const raw of text.split(/\r?\n/)) {
    const key = norm(raw);
    have.set(key, (have.get(key) || 0) + 1);
  }
  const missing = [];
  for (const raw of lines) {
    const key = norm(raw);
    if (trivial(key)) { continue; }
    if ((have.get(key) || 0) > 0) { have.set(key, have.get(key) - 1); }
    else { missing.push(key); }
  }
  if (missing.length) {
    totalMissing += missing.length;
    console.log(
      `[${label}] ${file}: ${missing.length} added line(s) not found`,
    );
    for (const m of missing) { console.log(`      - ${m.slice(0, 160)}`); }
  }
}
console.log(
  `[${label}] files checked: ${added.size}; non-trivial added lines missing: ${totalMissing}`,
);
process.exit(totalMissing ? 1 : 0);
