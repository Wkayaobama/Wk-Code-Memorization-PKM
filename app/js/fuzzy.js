/* Fuzzy project matching. Port of recurser/fuzzy.py: nicknames -> projects. */
(function (root) {
  'use strict';
  var WEIGHTS = { name: 1.0, alias: 1.0, title: 0.9, keyword: 0.75, one_liner: 0.6 };
  var STOP = { the: 1, a: 1, an: 1, of: 1, for: 1, and: 1, to: 1, in: 1, on: 1, my: 1, project: 1, repo: 1, thing: 1, app: 1 };
  var CAMEL = /[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+/g;

  function tokens(text) {
    var out = [];
    String(text || '').split(/[^A-Za-z0-9]+/).forEach(function (chunk) {
      if (!chunk) return;
      var parts = chunk.match(CAMEL) || [chunk];
      parts.forEach(function (p) { out.push(p.toLowerCase()); });
    });
    var kept = out.filter(function (t) { return !STOP[t]; });
    return kept.length ? kept : out;
  }

  function squash(text) { return String(text || '').toLowerCase().replace(/[^a-z0-9]+/g, ''); }

  function trigrams(s) {
    s = '  ' + s + ' ';
    var set = {};
    for (var i = 0; i < s.length - 2; i++) set[s.substr(i, 3)] = 1;
    return set;
  }

  function trigramDice(a, b) {
    a = squash(a); b = squash(b);
    if (!a || !b) return 0;
    if (a === b) return 1;
    var ta = trigrams(a), tb = trigrams(b), na = 0, nb = 0, both = 0, k;
    for (k in ta) { na++; if (tb[k]) both++; }
    for (k in tb) nb++;
    return 2 * both / (na + nb);
  }

  /* Longest common substring length and a Ratcliff/Obershelp ratio (as difflib computes it). */
  function longestMatch(a, alo, ahi, b, blo, bhi) {
    var best = { i: alo, j: blo, size: 0 }, prev = {}, i, j;
    for (i = alo; i < ahi; i++) {
      var cur = {};
      for (j = blo; j < bhi; j++) {
        if (a[i] === b[j]) {
          var k = (prev[j - 1] || 0) + 1;
          cur[j] = k;
          if (k > best.size) best = { i: i - k + 1, j: j - k + 1, size: k };
        }
      }
      prev = cur;
    }
    return best;
  }
  function matchingChars(a, alo, ahi, b, blo, bhi) {
    var m = longestMatch(a, alo, ahi, b, blo, bhi);
    if (!m.size) return 0;
    return m.size + matchingChars(a, alo, m.i, b, blo, m.j) + matchingChars(a, m.i + m.size, ahi, b, m.j + m.size, bhi);
  }
  function ratio(a, b) {
    if (!a.length && !b.length) return 1;
    return 2 * matchingChars(a, 0, a.length, b, 0, b.length) / (a.length + b.length);
  }

  function tokenSimilarity(q, c) {
    if (q === c) return 1;
    if (Math.min(q.length, c.length) >= 3 && (c.indexOf(q) === 0 || q.indexOf(c) === 0)) return 0.85;
    var r = ratio(q, c);
    if (r >= 0.75) return r * 0.9;
    if (longestMatch(q, 0, q.length, c, 0, c.length).size >= 4) return 0.6;
    return 0;
  }

  function acronymHits(q, c) {
    var hits = {}, cset = {}, n, i, j, initials;
    c.forEach(function (t) { cset[t] = 1; });
    for (n = 4; n >= 2; n--) {
      for (i = 0; i + n <= q.length; i++) {
        initials = q.slice(i, i + n).map(function (t) { return t[0]; }).join('');
        if (cset[initials]) for (j = i; j < i + n; j++) hits[j] = Math.max(hits[j] || 0, 0.9);
      }
      for (i = 0; i + n <= c.length; i++) {
        initials = c.slice(i, i + n).map(function (t) { return t[0]; }).join('');
        for (j = 0; j < q.length; j++) if (q[j] === initials) hits[j] = Math.max(hits[j] || 0, 0.9);
      }
    }
    return hits;
  }

  function coverage(q, c, withAcronyms) {
    var acr = withAcronyms ? acronymHits(q, c) : {}, sum = 0;
    q.forEach(function (qt, i) {
      var best = 0;
      c.forEach(function (ct) { best = Math.max(best, tokenSimilarity(qt, ct)); });
      sum += Math.max(best, acr[i] || 0);
    });
    return sum / q.length;
  }

  function fieldScore(query, text) {
    var q = tokens(query), c = tokens(text);
    if (!q.length || !c.length) return 0;
    if (squash(query) === squash(text)) return 1;
    return 0.75 * coverage(q, c, true) + 0.25 * trigramDice(query, text);
  }

  function fields(item) {
    var out = [];
    ['name', 'repo', 'slug'].forEach(function (k) { if (item[k]) out.push(['name', String(item[k]).split('/').pop()]); });
    (item.aliases || []).forEach(function (a) { out.push(['alias', a]); });
    if (item.title) out.push(['title', item.title]);
    (item.keywords || []).forEach(function (k) { out.push(['keyword', k]); });
    if (item.one_liner) out.push(['one_liner', item.one_liner]);
    return out;
  }

  function scoreItem(query, item) {
    var best = 0, why = '', strong = 0;
    fields(item).forEach(function (f) {
      var raw = fieldScore(query, f[1]);
      if (f[0] === 'one_liner') {
        var q = tokens(query), c = tokens(f[1]);
        if (q.length && c.length) raw = coverage(q, c, false);
      }
      var s = raw * WEIGHTS[f[0]];
      if (s >= 0.55) strong++;
      if (s > best) { best = s; why = f[0] + ': ' + f[1]; }
    });
    if (strong > 1) best = Math.min(1, best + 0.03 * (strong - 1));
    return { score: Math.round(best * 10000) / 10000, why: why };
  }

  function rank(query, items, opts) {
    opts = opts || {};
    var limit = opts.limit || 5, threshold = opts.threshold == null ? 0.35 : opts.threshold, out = [];
    items.forEach(function (item) {
      var s = scoreItem(query, item);
      if (s.score >= threshold) out.push({ item: item, score: s.score, why: s.why });
    });
    out.sort(function (a, b) {
      if (b.score !== a.score) return b.score - a.score;
      var an = String(a.item.name || a.item.slug || ''), bn = String(b.item.name || b.item.slug || '');
      return an < bn ? -1 : an > bn ? 1 : 0;
    });
    return out.slice(0, limit);
  }

  var api = { tokens: tokens, squash: squash, trigramDice: trigramDice, ratio: ratio, tokenSimilarity: tokenSimilarity,
              fieldScore: fieldScore, scoreItem: scoreItem, rank: rank };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.Recurser = root.Recurser || {};
  root.Recurser.fuzzy = api;
})(typeof window !== 'undefined' ? window : globalThis);
