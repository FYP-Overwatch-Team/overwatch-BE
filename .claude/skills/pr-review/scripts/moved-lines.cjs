'use strict';

const { git, repoRoot, takeOption } = require('./lib/source.cjs');
const { norm, patchFiles, readAt } = require('./lib/lines.cjs');

const usage = () => {
  console.error([
    'Find edits that need carrying over after code moved: lines that <ours> changed or removed in a file',
    'which <theirs> copied verbatim into a file it created. Each hit is old code living on in the new',
    'location, so the edit <ours> made at the old location probably has to be ported there.',
    '',
    '  node moved-lines.cjs <base> <ours> <theirs> [--merged <rev>] [--min <hits>]',
    '',
    'Hits are grouped per (changed file, new file) pair; pairs below --min hits (default 3) are summarised',
    'only, because one or two shared lines are usually boilerplate, while a real move produces a cluster.',
    'Example: the PR edited features/graph/components/X.tsx while main moved X into shared/components/...',
    '  node moved-lines.cjs $(git merge-base HEAD MERGE_HEAD) HEAD MERGE_HEAD     (during the merge)',
  ].join('\n'));
  process.exit(2);
};

let args = process.argv.slice(2);
if (args.includes('--help')) { usage(); }
const mergedOpt = takeOption(args, '--merged');
args = mergedOpt.rest;
const minOpt = takeOption(args, '--min');
args = minOpt.rest;
const minHits = Number(minOpt.value || 3);
if (args.length !== 3 || !Number.isFinite(minHits)) { usage(); }
const [base, ours, theirs] = args;

const root = repoRoot();
const significant = (line) =>
  line.length >= 12 && !/^[\])}{([;,>:</\s]*$/.test(line)
  && !/^<\/\w+>$/.test(line);
const { added, removed } = patchFiles(git(root, ['diff', base, ours]));

const created = git(root, [
  'diff',
  '--name-only',
  '--diff-filter=A',
  base,
  theirs,
])
  .split(/\r?\n/)
  .filter((f) => f && /\.(tsx?|jsx?|cjs|mjs)$/.test(f));
const createdLines = created
  .map((f) => [f, readAt(root, mergedOpt.value, f)])
  .filter(([, text]) => text !== null)
  .map(([f, text]) => [f, text.split(/\r?\n/).map(norm)]);

const pairs = new Map();
for (const [file, lines] of removed) {
  const readded = new Map();
  for (const l of added.get(file) || []) {
    readded.set(norm(l), (readded.get(norm(l)) || 0) + 1);
  }
  const seen = new Map();
  for (const raw of lines) {
    const key = norm(raw);
    if (!significant(key)) { continue; }
    seen.set(key, (seen.get(key) || 0) + 1);
    if ((readded.get(key) || 0) >= seen.get(key)) { continue; }
    for (const [target, targetLines] of createdLines) {
      const idx = targetLines.indexOf(key);
      if (idx < 0) { continue; }
      const pair = `${file} -> ${target}`;
      if (!pairs.has(pair)) { pairs.set(pair, []); }
      pairs.get(pair).push(
        `${target.split('/').pop()}:${idx + 1} | ${key.slice(0, 110)}`,
      );
    }
  }
}

const ranked = [...pairs].sort((a, b) => b[1].length - a[1].length);
const strong = ranked.filter(([, hits]) => hits.length >= minHits);
const weak = ranked.filter(([, hits]) => hits.length < minHits);
for (const [pair, hits] of strong) {
  console.log(`\n${pair}  (${hits.length} line(s))`);
  for (const h of hits.slice(0, 8)) { console.log(`    ${h}`); }
  if (hits.length > 8) { console.log(`    ... ${hits.length - 8} more`); }
}
if (weak.length) {
  console.log(
    `\n${weak.length} pair(s) below ${minHits} hit(s), likely boilerplate: ${
      weak.map(([p, h]) => `${p} (${h.length})`).join('; ')
    }`,
  );
}
console.log(
  `\nfiles created by theirs and scanned: ${createdLines.length}; pairs to inspect: ${strong.length}`,
);
process.exit(strong.length ? 1 : 0);
