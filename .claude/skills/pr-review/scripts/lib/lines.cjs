'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { git } = require('./source.cjs');

const norm = (line) => line.trim().replace(/\s+/g, ' ');

const trivial = (line) =>
  line === ''
  || /^[\])}{([;,>:</\s]*$/.test(line)
  || /^\)?\}?\)?;?,?$/.test(line);

const readAt = (root, rev, file) => {
  if (!rev) {
    const target = path.join(root, file);
    return fs.existsSync(target) ? fs.readFileSync(target, 'utf8') : null;
  }
  try {
    return git(root, ['show', `${rev}:${file}`]);
  } catch {
    return null;
  }
};

const lineCounts = (text, skip = trivial) => {
  const counts = new Map();
  if (text === null) { return counts; }
  for (const raw of text.split(/\r?\n/)) {
    const key = norm(raw);
    if (skip(key)) { continue; }
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  return counts;
};

const patchFiles = (patchText) => {
  const added = new Map();
  const removed = new Map();
  let oldFile = null;
  let newFile = null;
  for (const line of patchText.split(/\r?\n/)) {
    if (line.startsWith('diff ')) {
      oldFile = null;
      newFile = null;
      continue;
    }
    if (line.startsWith('--- ')) {
      oldFile = line.slice(4).replace(/^a\//, '');
      if (oldFile === '/dev/null') { oldFile = null; }
      continue;
    }
    if (line.startsWith('+++ ')) {
      newFile = line.slice(4).replace(/^b\//, '');
      if (newFile === '/dev/null') { newFile = null; }
      continue;
    }
    if (line.startsWith('index ') || line.startsWith('@@')) { continue; }
    if (line.startsWith('+') && newFile) {
      if (!added.has(newFile)) { added.set(newFile, []); }
      added.get(newFile).push(line.slice(1));
    } else if (line.startsWith('-') && oldFile) {
      if (!removed.has(oldFile)) { removed.set(oldFile, []); }
      removed.get(oldFile).push(line.slice(1));
    }
  }
  return { added, removed };
};

module.exports = { lineCounts, norm, patchFiles, readAt, trivial };
