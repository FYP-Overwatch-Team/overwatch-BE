'use strict';

const fs = require('node:fs');
const {
  declarations,
  lineDiff,
  loadTypeScript,
  normalise,
  parseRenames,
  readSpec,
  repoRoot,
  resolveName,
  takeOption,
} = require('./lib/source.cjs');

const usage = () => {
  console.error([
    'Compare named declarations between two versions, ignoring comments, formatting and visibility modifiers.',
    '',
    '  node ast-compare.cjs <from-spec> <to-spec> <name>[,<name>...] [--rename from=to ...]',
    '  node ast-compare.cjs --config <file.json>    [{ "name", "from", "to", "renames": [["a","b"]] }]',
    '  node ast-compare.cjs --print <spec> <name>',
    '  node ast-compare.cjs --list <spec>',
    '',
    'spec: <git-ref>:<repo-relative-path>, or WORKTREE:<path>, or a plain path (working tree).',
    'name: member, Class.member, a function name, or a module-level const. Run from inside the repo.',
    'Exit code 0 when every name is SAME, 1 otherwise.',
  ].join('\n'));
  process.exit(2);
};

const root = repoRoot();
const ts = loadTypeScript(root);
const cache = new Map();
const declsFor = (spec) => {
  if (!cache.has(spec)) {
    const text = readSpec(root, spec);
    cache.set(
      spec,
      text === null ? null : declarations(ts, text, spec.split(':').pop()),
    );
  }
  return cache.get(spec);
};

const lookup = (spec, name) => {
  const decls = declsFor(spec);
  if (decls === null) { return { error: `FILE NOT FOUND (${spec})` }; }
  return resolveName(decls, name);
};

const compare = ({ name, from, to, renames = [] }) => {
  const a = lookup(from, name);
  const b = lookup(to, name);
  if (a.error || b.error) {
    const side = a.error ? `from: ${a.error}` : `to: ${b.error}`;
    console.log(`MISSING ${name} (${side})`);
    return false;
  }
  const na = normalise(a.match.text, renames);
  const nb = normalise(b.match.text, renames);
  if (na === nb) {
    console.log(`SAME    ${name}`);
    return true;
  }
  console.log(`DIFFER  ${name}`);
  for (const line of lineDiff(na, nb)) { console.log(`    ${line}`); }
  return false;
};

let args = process.argv.slice(2);
if (args.length === 0 || args.includes('--help')) { usage(); }

const list = takeOption(args, '--list');
if (list.value !== undefined) {
  const decls = declsFor(list.value);
  if (decls === null) {
    console.error(`${list.value}: FILE NOT FOUND`);
    process.exit(1);
  }
  for (const d of decls) { console.log(d.qualified); }
  process.exit(0);
}

const print = takeOption(args, '--print');
if (print.value !== undefined) {
  const [name] = print.rest;
  if (!name) { usage(); }
  const found = lookup(print.value, name);
  if (found.error) {
    console.error(`${name}: ${found.error}`);
    process.exit(1);
  }
  console.log(found.match.text);
  process.exit(0);
}

const config = takeOption(args, '--config');
let jobs;
if (config.value !== undefined) {
  jobs = JSON.parse(fs.readFileSync(config.value, 'utf8'));
} else {
  const parsed = parseRenames(args);
  args = parsed.rest;
  if (args.length !== 3) { usage(); }
  const [from, to, names] = args;
  jobs = names.split(',').filter(Boolean).map((name) => ({
    name,
    from,
    to,
    renames: parsed.renames,
  }));
}

let ok = true;
for (const job of jobs) { ok = compare(job) && ok; }
process.exit(ok ? 0 : 1);
