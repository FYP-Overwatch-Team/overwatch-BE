'use strict';

const {
  declarations,
  lineDiff,
  loadTypeScript,
  normalise,
  parseRenames,
  readSpec,
  repoRoot,
  takeOption,
} = require('./lib/source.cjs');

const usage = () => {
  console.error([
    'Check that declarations removed from a file reappear unchanged in the files they moved to.',
    '',
    '  node verify-move.cjs <from-ref> <source-path> <target-path>[,<target-path>...] [--to-ref <ref>] [--rename from=to ...]',
    '',
    'Compares <source-path> at <from-ref> with the same file at --to-ref (default: the working tree).',
    'Each declaration that disappeared must exist in a target file (read at --to-ref) with the same AST text.',
    'Use --rename for call sites that changed with the move, e.g. --rename "fetchRepos(=reposApi.fetchRepos(".',
    'Exit code 0 when every moved declaration matches, 1 otherwise.',
  ].join('\n'));
  process.exit(2);
};

let args = process.argv.slice(2);
if (args.length === 0 || args.includes('--help')) { usage(); }
const toRef = takeOption(args, '--to-ref');
args = toRef.rest;
const { renames, rest } = parseRenames(args);
if (rest.length !== 3) { usage(); }
const [fromRef, sourceRel, targetsArg] = rest;
const to = toRef.value || 'WORKTREE';

const root = repoRoot();
const ts = loadTypeScript(root);
const load = (ref, rel) => {
  const text = readSpec(root, `${ref}:${rel}`);
  if (text === null) { return null; }
  return new Map(declarations(ts, text, rel).map((d) => [d.qualified, d]));
};

const before = load(fromRef, sourceRel);
if (before === null) {
  console.error(`${fromRef}:${sourceRel} not found`);
  process.exit(2);
}
const after = load(to, sourceRel) || new Map();
const removed = [...before.keys()].filter((k) => !after.has(k));
const changedInPlace = [...before.keys()].filter(
  (k) =>
    after.has(k)
    && normalise(before.get(k).text, renames)
      !== normalise(after.get(k).text, renames),
);
const added = [...after.keys()].filter((k) => !before.has(k));

const targets = new Map();
for (const rel of targetsArg.split(',').filter(Boolean)) {
  const decls = load(to, rel);
  if (decls === null) {
    console.log(`target not found: ${to}:${rel}`);
    continue;
  }
  for (const d of decls.values()) {
    if (!targets.has(d.name)) { targets.set(d.name, []); }
    targets.get(d.name).push({ rel, d });
  }
}

let problems = 0;
const matchedTargets = new Set();
console.log(`removed from ${sourceRel}: ${removed.length}`);
for (const key of removed) {
  const old = before.get(key);
  const candidates = targets.get(old.name) || [];
  if (candidates.length === 0) {
    problems += 1;
    console.log(`  GONE    ${key} (not found in any target)`);
    continue;
  }
  const oldText = normalise(old.text, renames);
  const exact = candidates.find((c) =>
    normalise(c.d.text, renames) === oldText
  );
  if (exact) {
    matchedTargets.add(`${exact.rel}#${exact.d.qualified}`);
    console.log(`  MATCH   ${key} -> ${exact.rel} (${exact.d.qualified})`);
    continue;
  }
  problems += 1;
  const nearest = candidates[0];
  matchedTargets.add(`${nearest.rel}#${nearest.d.qualified}`);
  console.log(`  DIFFER  ${key} -> ${nearest.rel} (${nearest.d.qualified})`);
  for (
    const line of lineDiff(oldText, normalise(nearest.d.text, renames))
  ) { console.log(`      ${line}`); }
}

const extra = [];
for (const list of targets.values()) {
  for (const c of list) {
    if (c.d.name === 'constructor') { continue; }
    if (!matchedTargets.has(`${c.rel}#${c.d.qualified}`)) {
      extra.push(`${c.d.qualified} (${c.rel.split('/').pop()})`);
    }
  }
}
console.log(
  `target declarations not accounted for by the move: ${
    extra.join(', ') || 'none'
  }`,
);
console.log(
  `changed in place in ${sourceRel}: ${changedInPlace.join(', ') || 'none'}`,
);
console.log(`added to ${sourceRel}: ${added.join(', ') || 'none'}`);
console.log(
  problems === 0
    ? 'ALL MOVED DECLARATIONS IDENTICAL'
    : `${problems} DIFFERENCE(S) TO INSPECT`,
);
process.exit(problems === 0 ? 0 : 1);
