// Measure what autofixLesson changes over a folder of lessons: issues by code before vs after.
//   node check_autofix.js eval_final3 [outdir]
const fs = require('fs'), path = require('path');
const { validateLesson, autofixLesson } = require('./validator.js');
const dir = process.argv[2], out = process.argv[3];
if (out) fs.mkdirSync(out, { recursive: true });
const tally = (acc, issues) => issues.forEach((i) => { acc[i.code] = (acc[i.code] || 0) + 1; });
const before = {}, after = {};
let n = 0, changed = 0, nChanges = 0, newIssues = [];
for (const f of fs.readdirSync(dir).filter((x) => x.endsWith('.json') && !x.includes('INVALID'))) {
  const lesson = JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8'));
  const b = validateLesson(lesson);
  const { lesson: fixed, changes } = autofixLesson(lesson);
  const a = validateLesson(fixed);
  n++; if (changes.length) changed++; nChanges += changes.length;
  tally(before, b.issues); tally(after, a.issues);
  const had = new Set(b.issues.map((i) => i.code + i.path));
  for (const i of a.issues) if (!had.has(i.code + i.path) && !['label_on_mark', 'orphan_label', 'dangling_line', 'label_overlap'].includes(i.code)) newIssues.push(`${f}: ${i.code} ${i.path}`);
  if (out) fs.writeFileSync(path.join(out, f), JSON.stringify(fixed));
}
console.log(`lessons=${n} changedLessons=${changed} totalChanges=${nChanges}`);
console.log('before', JSON.stringify(before));
console.log('after ', JSON.stringify(after));
console.log('new non-geometry issues introduced:', newIssues.length, newIssues.slice(0, 5));
