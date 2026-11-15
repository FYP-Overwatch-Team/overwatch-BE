'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { createRequire } = require('node:module');

const repoRoot = (cwd = process.cwd()) => {
  try {
    return execFileSync('git', ['rev-parse', '--show-toplevel'], {
      cwd,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim();
  } catch {
    return cwd;
  }
};

const git = (root, args) =>
  execFileSync('git', args, {
    cwd: root,
    encoding: 'utf8',
    maxBuffer: 1 << 28,
    stdio: ['ignore', 'pipe', 'pipe'],
  });

const loadTypeScript = (root) => {
  try {
    return createRequire(path.join(root, 'package.json'))('typescript');
  } catch {
    throw new Error(
      `Cannot load typescript from ${root}. Run this from a worktree that has node_modules installed.`,
    );
  }
};

const parseSpec = (spec) => {
  const i = spec.indexOf(':');
  if (i > 1) { return { ref: spec.slice(0, i), rel: spec.slice(i + 1) }; }
  return { ref: 'WORKTREE', rel: spec };
};

const readSpec = (root, spec) => {
  const { ref, rel } = parseSpec(spec);
  if (ref === 'WORKTREE') {
    const file = path.resolve(root, rel);
    return fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : null;
  }
  try {
    return git(root, ['show', `${ref}:${rel}`]);
  } catch {
    return null;
  }
};

const parseRenames = (args) => {
  const renames = [];
  const rest = [];
  for (let i = 0; i < args.length; i += 1) {
    if (args[i] === '--rename') {
      const pair = args[i + 1] || '';
      const eq = pair.indexOf('=');
      if (eq <= 0) {
        throw new Error(`--rename expects from=to, got "${pair}"`);
      }
      renames.push([pair.slice(0, eq), pair.slice(eq + 1)]);
      i += 1;
    } else {
      rest.push(args[i]);
    }
  }
  return { renames, rest };
};

const takeOption = (args, name) => {
  const i = args.indexOf(name);
  if (i < 0) { return { value: undefined, rest: args }; }
  return {
    value: args[i + 1],
    rest: [...args.slice(0, i), ...args.slice(i + 2)],
  };
};

const takeFlag = (args, name) => {
  const i = args.indexOf(name);
  if (i < 0) { return { value: false, rest: args }; }
  return { value: true, rest: [...args.slice(0, i), ...args.slice(i + 1)] };
};

const normalise = (text, renames = []) =>
  renames
    .reduce((acc, [from, to]) => acc.split(from).join(to), text)
    .replace(
      /^(\s*)((?:public|private|protected|readonly|override)\s+)+/gm,
      '$1',
    )
    .replace(/\s+/g, ' ')
    .replace(/,\s*([)\]}])/g, '$1')
    .replace(/([([{])\s+/g, '$1')
    .replace(/\s+([)\]}])/g, '$1')
    .replace(/ \./g, '.')
    .trim();

const splitLines = (text) =>
  text
    .replace(/([;{])/g, '$1\n')
    .replace(/\}/g, '\n}')
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);

const lineDiff = (aText, bText, max = 40) => {
  const a = splitLines(aText);
  const b = splitLines(bText);
  const n = a.length;
  const m = b.length;
  const dp = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      dp[i][j] = a[i] === b[j]
        ? dp[i + 1][j + 1] + 1
        : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const out = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      i += 1;
      j += 1;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      out.push(`- ${a[i]}`);
      i += 1;
    } else {
      out.push(`+ ${b[j]}`);
      j += 1;
    }
  }
  while (i < n) { out.push(`- ${a[i++]}`); }
  while (j < m) { out.push(`+ ${b[j++]}`); }
  if (out.length > max) {
    return [...out.slice(0, max), `... ${out.length - max} more line(s)`];
  }
  return out;
};

const declarations = (ts, text, file) => {
  const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true);
  const found = [];
  const add = (owner, name, node) =>
    found.push({
      owner,
      name,
      node,
      qualified: owner ? `${owner}.${name}` : name,
    });
  const memberName = (member) => {
    if (ts.isConstructorDeclaration(member)) { return 'constructor'; }
    if (!member.name) { return null; }
    const base = member.name.getText(source);
    if (ts.isGetAccessorDeclaration(member)) { return `get ${base}`; }
    if (ts.isSetAccessorDeclaration(member)) { return `set ${base}`; }
    return base;
  };
  const visitClass = (node) => {
    const owner = node.name ? node.name.text : '<anonymous>';
    for (const member of node.members) {
      const name = memberName(member);
      if (!name) { continue; }
      if (
        (ts.isMethodDeclaration(member) || ts.isConstructorDeclaration(member))
        && !member.body
      ) { continue; }
      add(owner, name, member);
    }
  };
  const visitTop = (node) => {
    if (ts.isClassDeclaration(node)) {
      visitClass(node);
      return;
    }
    if (ts.isFunctionDeclaration(node) && node.name && node.body) {
      add(null, node.name.text, node);
      return;
    }
    if (ts.isVariableStatement(node)) {
      for (const decl of node.declarationList.declarations) {
        if (ts.isIdentifier(decl.name) && decl.initializer) {
          add(null, decl.name.text, decl);
        }
      }
      return;
    }
    if (
      ts.isModuleDeclaration(node) && node.body && ts.isModuleBlock(node.body)
    ) {
      node.body.statements.forEach(visitTop);
    }
  };
  source.statements.forEach(visitTop);
  const printer = ts.createPrinter({ removeComments: true });
  return found.map((d) => ({
    ...d,
    text: printer.printNode(ts.EmitHint.Unspecified, d.node, source),
  }));
};

const resolveName = (decls, wanted) => {
  const matches = wanted.includes('.')
    ? decls.filter((d) => d.qualified === wanted)
    : decls.filter((d) => d.name === wanted);
  if (matches.length === 1) { return { match: matches[0] }; }
  if (matches.length === 0) { return { error: 'MISSING' }; }
  return {
    error: `AMBIGUOUS (${
      matches.map((m) => m.qualified).join(', ')
    }; qualify as Class.member)`,
  };
};

module.exports = {
  declarations,
  git,
  lineDiff,
  loadTypeScript,
  normalise,
  parseRenames,
  parseSpec,
  readSpec,
  repoRoot,
  resolveName,
  takeFlag,
  takeOption,
};
