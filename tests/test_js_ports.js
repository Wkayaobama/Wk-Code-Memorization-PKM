// node tests/test_js_ports.js — the JavaScript ports must agree with the Python reference.
const assert = require('assert');
const path = require('path');
const vec = require('./srs_vectors.json');
const srs = require(path.join('..', 'app', 'js', 'srs.js'));
const fuzzy = require(path.join('..', 'app', 'js', 'fuzzy.js'));
let n = 0;
for (const v of vec.srs) {
  const out = srs.schedule(v.state, v.rating, v.now);
  assert.deepStrictEqual(out, v.expect, 'schedule mismatch at vector ' + n);
  assert.strictEqual(srs.stage(out), v.stage);
  assert.ok(Math.abs(srs.strength(out) - v.strength) < 1e-12);
  n++;
}
for (const t of vec.fuzzy.tokens) assert.deepStrictEqual(fuzzy.tokens(t.text), t.tokens, 'tokens: ' + t.text);
for (const q of vec.fuzzy.queries) {
  const got = fuzzy.rank(q.query, vec.fuzzy.items, { limit: 3 }).map(h => ({ name: h.item.name, score: h.score }));
  assert.deepStrictEqual(got.map(g => g.name), q.top.map(t => t.name), 'ranking for ' + q.query);
  got.forEach((g, i) => assert.ok(Math.abs(g.score - q.top[i].score) < 1e-4, `score for ${q.query}/${g.name}: ${g.score} vs ${q.top[i].score}`));
}
console.log(`ok — ${n} scheduling vectors, ${vec.fuzzy.queries.length} fuzzy queries, ${vec.fuzzy.tokens.length} tokenisations agree with the Python reference`);
