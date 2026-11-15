'use strict';

const {
  lineDiff,
  loadTypeScript,
  normalise,
  parseRenames,
  readSpec,
  repoRoot,
  takeFlag,
  takeOption,
} = require('./lib/source.cjs');

const usage = () => {
  console.error([
    'Compare the tests (it/test, including .each/.only/.skip, keyed by describe trail) in one or more spec files.',
    '',
    '  node test-diff.cjs --base <spec>[,<spec>...] --head <spec>[,<spec>...] [--rename from=to ...] [--normalise <regex>] [--show-changed]',
    '',
    'spec: <git-ref>:<path>, WORKTREE:<path> or a plain path. A spec missing at its ref counts as empty.',
    'Typical uses:',
    '  what a PR did to a spec:   --base origin/main:apps/x/a.spec.ts --head WORKTREE:apps/x/a.spec.ts',
    '  tests moved between files: list every involved file on both sides',
    '  duplicated blocks:         same file on both sides shows duplicates already present',
    '--normalise replaces regex matches in bodies with SVC (for renamed services); --rename does exact replacements.',
    'Exit code 1 when tests were removed or new duplicates appeared, otherwise 0.',
  ].join('\n'));
  process.exit(2);
};

let args = process.argv.slice(2);
if (args.length === 0 || args.includes('--help')) { usage(); }
const baseOpt = takeOption(args, '--base');
args = baseOpt.rest;
const headOpt = takeOption(args, '--head');
args = headOpt.rest;
const normOpt = takeOption(args, '--normalise');
args = normOpt.rest;
const showOpt = takeFlag(args, '--show-changed');
args = showOpt.rest;
const { renames, rest } = parseRenames(args);
if (!baseOpt.value || !headOpt.value || rest.length) { usage(); }

const root = repoRoot();
const ts = loadTypeScript(root);
const printer = ts.createPrinter({ removeComments: true });
const placeholder = normOpt.value ? new RegExp(normOpt.value, 'g') : null;
const bodyKey = (text) => {
  const replaced = placeholder ? text.replace(placeholder, 'SVC') : text;
  return normalise(replaced, renames);
};

const calleeKind = (expression) => {
  let e = expression;
  let each = false;
  if (ts.isCallExpression(e)) {
    e = e.expression;
    each = true;
  }
  let modifier = null;
  if (ts.isPropertyAccessExpression(e)) {
    modifier = e.name.text;
    e = e.expression;
  }
  if (
    !ts.isIdentifier(e) || !['it', 'test', 'describe'].includes(e.text)
  ) { return null; }
  return {
    kind: e.text === 'describe' ? 'describe' : 'test',
    each: each || modifier === 'each',
  };
};

const titleOf = (node, source) => {
  const arg = node.arguments[0];
  if (!arg) { return null; }
  if (ts.isStringLiteralLike(arg)) { return arg.text; }
  if (ts.isTemplateExpression(arg)) { return arg.getText(source); }
  return null;
};

const collect = (spec) => {
  const text = readSpec(root, spec);
  const file = spec.split(':').pop();
  if (text === null) { return { spec, missing: true, tests: [] }; }
  const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true);
  const tests = [];
  const visit = (node, trail) => {
    if (ts.isCallExpression(node)) {
      const kind = calleeKind(node.expression);
      const title = kind ? titleOf(node, source) : null;
      if (kind && title !== null) {
        const label = `${kind.each ? '[each] ' : ''}${title}`;
        if (kind.kind === 'describe') {
          ts.forEachChild(node, (child) => visit(child, [...trail, label]));
          return;
        }
        tests.push({
          file,
          title: label,
          key: [...trail, label].join(' > '),
          body: bodyKey(
            printer.printNode(ts.EmitHint.Unspecified, node, source),
          ),
        });
        return;
      }
    }
    ts.forEachChild(node, (child) => visit(child, trail));
  };
  visit(source, []);
  return { spec, missing: false, tests };
};

const sides = (value) => value.split(',').filter(Boolean).map(collect);
const base = sides(baseOpt.value);
const head = sides(headOpt.value);
for (const side of [...base, ...head]) {
  if (side.missing) {
    console.log(`note: ${side.spec} does not exist (treated as empty)`);
  }
}
const baseTests = base.flatMap((s) => s.tests);
const headTests = head.flatMap((s) => s.tests);

const pending = baseTests.map((t, i) => ({ t, i }));
const usedHead = new Set();
const pairs = [];
const matchPass = (keyOf) => {
  for (const entry of pending) {
    if (entry.done) { continue; }
    const idx = headTests.findIndex((h, j) =>
      !usedHead.has(j) && keyOf(h) === keyOf(entry.t)
    );
    if (idx >= 0) {
      usedHead.add(idx);
      entry.done = true;
      pairs.push({ before: entry.t, after: headTests[idx] });
    }
  }
};
matchPass((t) => `${t.file}\u0000${t.key}`);
matchPass((t) => t.key);
matchPass((t) => t.title);

const identical = pairs.filter((p) =>
  p.before.body === p.after.body && p.before.key === p.after.key
  && p.before.file === p.after.file
);
const moved = pairs.filter((p) =>
  p.before.key !== p.after.key || p.before.file !== p.after.file
);
const changed = pairs.filter((p) =>
  p.before.body !== p.after.body && p.before.key === p.after.key
  && p.before.file === p.after.file
);
const removed = pending.filter((e) => !e.done).map((e) => e.t);
const added = headTests.filter((_, j) => !usedHead.has(j));

const counts = (tests) => {
  const m = new Map();
  for (const t of tests) {
    const k = `${t.file} :: ${t.key}`;
    m.set(k, (m.get(k) || 0) + 1);
  }
  return m;
};
const baseCounts = counts(baseTests);
const dupes = [...counts(headTests)].filter(([, n]) => n > 1);
const newDupes = dupes.filter(([k, n]) => n > (baseCounts.get(k) || 0));

const where = (t) => `[${t.file.split('/').pop()}] ${t.key}`;
console.log(`base tests: ${baseTests.length}; head tests: ${headTests.length}`);
console.log(`identical: ${identical.length}`);
console.log(`CHANGED BODY (${changed.length}):`);
for (const p of changed) {
  console.log(`  ~ ${where(p.after)}`);
  if (showOpt.value) {
    for (const line of lineDiff(p.before.body, p.after.body, 20)) {
      console.log(`      ${line}`);
    }
  }
}
console.log(`MOVED OR RETITLED TRAIL (${moved.length}):`);
for (const p of moved) {
  console.log(
    `  > ${where(p.before)}  ->  ${where(p.after)}${
      p.before.body === p.after.body ? '' : '  (body changed)'
    }`,
  );
}
console.log(`REMOVED (${removed.length}):`);
for (const t of removed) { console.log(`  - ${where(t)}`); }
console.log(`ADDED (${added.length}):`);
for (const t of added) { console.log(`  + ${where(t)}`); }
console.log(
  `DUPLICATED IN HEAD (${dupes.length}, new vs base: ${newDupes.length}):`,
);
for (const [k, n] of dupes) {
  console.log(`  x${n} (base x${baseCounts.get(k) || 0}) ${k}`);
}
process.exit(removed.length || newDupes.length ? 1 : 0);
