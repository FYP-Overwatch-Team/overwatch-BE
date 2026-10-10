'use strict';

const fs = require('node:fs');

const usage = () => {
  console.error([
    'Resolve every conflict hunk in a file to one side, keeping everything git merged cleanly around them.',
    'Unlike `git checkout --ours/--theirs`, auto-merged changes outside the hunks survive. CRLF-safe; handles diff3 markers.',
    '',
    '  node take-side.cjs ours|theirs <file>...',
    '',
    "Only use it once you have decided a whole file's hunks belong to one side; then `git add` the file.",
  ].join('\n'));
  process.exit(2);
};

const [side, ...files] = process.argv.slice(2);
if (!['ours', 'theirs'].includes(side) || files.length === 0) { usage(); }

for (const file of files) {
  const lines = fs.readFileSync(file, 'utf8').split('\n');
  const out = [];
  let state = 'normal';
  let conflicts = 0;
  for (const raw of lines) {
    const line = raw.replace(/\r$/, '');
    if (state === 'normal' && line.startsWith('<<<<<<< ')) {
      state = 'ours';
      conflicts += 1;
      continue;
    }
    if (state === 'ours' && line.startsWith('||||||| ')) {
      state = 'base';
      continue;
    }
    if ((state === 'ours' || state === 'base') && line === '=======') {
      state = 'theirs';
      continue;
    }
    if (state === 'theirs' && line.startsWith('>>>>>>> ')) {
      state = 'normal';
      continue;
    }
    if (state === 'normal' || state === side) { out.push(raw); }
  }
  if (state !== 'normal') {
    throw new Error(`unterminated conflict in ${file}`);
  }
  fs.writeFileSync(file, out.join('\n'));
  console.log(`${file}: took ${side} in ${conflicts} conflict(s)`);
}
