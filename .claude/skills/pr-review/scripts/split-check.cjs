'use strict';

const { declarations, loadTypeScript, readSpec, repoRoot, resolveName } =
  require('./lib/source.cjs');

const usage = () => {
  console.error([
    'Check that a method split into several methods kept its statements verbatim.',
    '',
    '  node split-check.cjs <original-spec> <original-name> <split-spec> <name>[,<name>...]',
    '',
    'spec: <git-ref>:<path>, WORKTREE:<path> or a plain path. Names may be Class.member.',
    'Prints how many original top-level statements appear verbatim in the split methods,',
    'the ones that do not, and the new "glue" statements to read by hand.',
  ].join('\n'));
  process.exit(2);
};

const args = process.argv.slice(2);
if (args.length !== 4 || args.includes('--help')) { usage(); }
const [origSpec, origName, splitSpec, splitNames] = args;

const root = repoRoot();
const ts = loadTypeScript(root);
const printer = ts.createPrinter({ removeComments: true });
const squash = (text) => text.replace(/\s+/g, '').replace(/,([)\]}])/g, '$1');

const statementsOf = (spec, name) => {
  const text = readSpec(root, spec);
  if (text === null) { throw new Error(`${spec} not found`); }
  const found = resolveName(
    declarations(ts, text, spec.split(':').pop()),
    name,
  );
  if (found.error) { throw new Error(`${name} in ${spec}: ${found.error}`); }
  const node = found.match.node;
  const body = node.body || (node.initializer && node.initializer.body);
  if (!body || !body.statements) {
    throw new Error(`${name} in ${spec} has no block body`);
  }
  const source = node.getSourceFile();
  return body.statements.map((s) => ({
    method: name,
    text: squash(printer.printNode(ts.EmitHint.Unspecified, s, source)),
  }));
};

const original = statementsOf(origSpec, origName);
const pool = splitNames
  .split(',')
  .filter(Boolean)
  .flatMap((name) => statementsOf(splitSpec, name))
  .map((s) => ({ ...s, used: false }));

let found = 0;
const missing = [];
for (const statement of original) {
  const match = pool.find((p) => !p.used && p.text === statement.text);
  if (match) {
    match.used = true;
    found += 1;
  } else {
    missing.push(statement.text);
  }
}
console.log(
  `${origName}: ${found}/${original.length} top-level statements appear verbatim in the split`,
);
for (const m of missing) { console.log(`  NOT VERBATIM: ${m.slice(0, 220)}`); }
const glue = pool.filter((p) => !p.used);
console.log(
  `statements in the split that are not from the original (read these): ${glue.length}`,
);
for (const g of glue) {
  console.log(
    `  [${g.method}] ${g.text.slice(0, 180)}`,
  );
}
