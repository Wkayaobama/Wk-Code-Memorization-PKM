/* A rehearsal session: which cards, in which order, and what happens on each rating. */
(function (root) {
  'use strict';
  var R = root.Recurser = root.Recurser || {};
  var model = R.model || (typeof require !== 'undefined' ? require('./model.js') : null);
  var BLOCK = 10, MAX_REQUEUE = 2;
  var current = null;

  function shuffle(list, seed) {
    var a = list.slice(), s = seed || 1;
    for (var i = a.length - 1; i > 0; i--) {
      s = (s * 1664525 + 1013904223) % 4294967296;
      var j = Math.floor(s / 4294967296 * (i + 1));
      var t = a[i]; a[i] = a[j]; a[j] = t;
    }
    return a;
  }

  /* Decide the cards for one kind of session. Pure: (kind, index, progress, now, options) -> plan. */
  function plan(kind, ix, progress, now, opts) {
    opts = opts || {};
    var size = opts.sessionSize || 20, order = opts.order || 'risk';
    var room = Math.max(0, (opts.newPerDay == null ? 10 : opts.newPerDay) - model.introducedToday(progress, now));
    var p = opts.slug ? ix.projectBySlug[opts.slug] : null, name = p ? (p.title || p.slug) : '';
    var cards = [], cram = false, title = '', note = '';

    if (kind === 'pulse') {
      cards = model.dueQueue(ix, progress, now).slice(0, size);
      title = 'Review what is due';
    } else if (kind === 'learn') {
      var fresh = model.newQueue(ix, progress, now, opts.slug ? { slug: opts.slug } : null, order);
      var slug = fresh.length ? fresh[0].slug : null;
      cards = fresh.filter(function (c) { return c.slug === slug; }).slice(0, Math.min(BLOCK, room));
      title = slug ? 'New block · ' + (ix.projectBySlug[slug].title || slug) : 'New block';
      if (!room) note = 'You reached today’s limit for new cards. Raise it under Sync if you want more.';
    } else if (kind === 'quick') {
      var q = model.dueQueue(ix, progress, now, { maxLevel: 1 });
      var extra = model.newQueue(ix, progress, now, { maxLevel: 1 }, order).slice(0, Math.min(room, Math.max(0, size - q.length)));
      cards = q.concat(extra).slice(0, size);
      title = 'On the go · one-line cards';
      if (!cards.length) {                       /* nothing scheduled: still give the commute something to chew on */
        cards = shuffle(ix.bundle.cards.filter(function (c) { return c.level === 1; }), opts.seed || now).slice(0, BLOCK);
        cram = true; title += ' · practice';
      }
    } else if (kind === 'outputs') {
      var f = { types: ['output'] };
      if (opts.slug) f.slug = opts.slug;
      var due = model.dueQueue(ix, progress, now, f);
      cards = due.concat(model.newQueue(ix, progress, now, f, order).slice(0, Math.min(room, Math.max(0, size - due.length)))).slice(0, size);
      title = (name ? name + ' · ' : '') + 'predict the output';
      if (!cards.length) {                       /* nothing due or new: practise the outputs anyway */
        var pool0 = (opts.slug ? (ix.cardsBySlug[opts.slug] || []) : ix.bundle.cards).filter(function (c) { return c.type === 'output'; });
        cards = (opts.slug ? pool0.slice().sort(model.ladder) : shuffle(pool0, opts.seed || now)).slice(0, size);
        cram = true; title += ' · practice';
      }
    } else if (kind === 'project') {
      var due2 = model.dueQueue(ix, progress, now, { slug: opts.slug });
      var new2 = model.newQueue(ix, progress, now, { slug: opts.slug }, order).slice(0, Math.min(BLOCK, room));
      cards = due2.concat(new2).slice(0, size);
      title = name;
      if (!cards.length) note = 'Nothing is due in this project and today’s new-card limit is used up. Use a random draw to rehearse anyway.';
    } else if (kind === 'facet') {
      cards = (ix.cardsBySlug[opts.slug] || []).filter(function (c) { return c.category === opts.category; })
        .slice().sort(function (a, b) { return a.rank - b.rank; });
      cram = true;
      title = name + ' · shelf ' + opts.category;
    } else if (kind === 'random') {
      var pool = opts.slug ? (ix.cardsBySlug[opts.slug] || []) : ix.bundle.cards;
      cards = shuffle(pool, opts.seed || now).slice(0, opts.count || BLOCK);
      cram = true;
      title = (name ? name + ' · ' : '') + 'random draw';
    } else if (kind === 'recap') {
      var key = model.dayKey(now), ids = {};
      (progress.log || []).forEach(function (e) {
        if (model.dayKey(e.t) !== key || e.c) return;
        var st = progress.cards[e.id];
        if (e.r === 0 || (st && st.first && model.dayKey(st.first) === key)) ids[e.id] = 1;
      });
      cards = Object.keys(ids).map(function (id) { return ix.cardsById[id]; }).filter(Boolean).slice(0, size);
      cram = true;
      title = 'Evening recap';
      if (!cards.length) note = 'Nothing to recap: no lapses and no new cards today.';
    } else if (kind === 'sit') {
      /* An exam sitting: one project, address order, attempt before every reveal, pass or fail. */
      cards = (ix.cardsBySlug[opts.slug] || []).slice().sort(function (a, b) { return a.code < b.code ? -1 : 1; });
      if (opts.maxLevel) cards = cards.filter(function (c) { return c.level <= opts.maxLevel; });
      title = name + ' \u00b7 sitting';
    } else if (kind === 'walk') {
      cards = (ix.cardsBySlug[opts.slug] || []).slice().sort(function (a, b) { return a.code < b.code ? -1 : 1; });
      cram = true;
      title = name + ' \u00b7 walkthrough in address order';
    } else if (kind === 'reentry') {
      /* The re-entry kit: every shelf's keystone (rank 0), with the anchor shown as a cue. */
      cards = (ix.cardsBySlug[opts.slug] || []).filter(function (c) { return c.rank === 0; })
        .slice().sort(function (a, b) { return a.category - b.category; });
      opts.cues = true;
      title = name + ' \u00b7 re-entry (the rank-0 row)';
    } else if (kind === 'single') {
      cards = [ix.cardsById[opts.id]].filter(Boolean);
      cram = true;
      title = 'One card';
    }
    return { kind: kind, title: title, cards: cards, cram: cram, note: note, opts: opts };
  }

  function start(p) {
    current = { kind: p.kind, title: p.title, queue: p.cards.slice(), pos: 0, cram: p.cram, opts: p.opts,
                startedAt: Date.now(), results: [], again: {}, total: p.cards.length,
                revealed: false, hint: 0, typed: "", shown: 0, writing: false, ticks: {}, failing: false };
    return current;
  }
  function get() { return current; }
  function end() { current = null; }
  function card() { return current && current.queue[current.pos]; }
  function resetCardState() { current.revealed = false; current.hint = 0; current.typed = ""; current.shown = 0; current.writing = false; current.ticks = {}; current.failing = false; }

  /* Record a rating, requeue lapses once or twice, advance. Returns true while cards remain. */
  function rate(rating, store) {
    var c = card();
    if (!c) return false;
    if (current.hint && rating > 1) rating = 1;                 /* an aided recall is scheduled as Hard at best */
    var state = store.review(c, rating, { hint: !!current.hint, cram: current.cram });
    current.results.push({ id: c.id, r: rating, hint: !!current.hint, learned: !!(state && state.learned) });
    if (rating === 0) {
      current.again[c.id] = (current.again[c.id] || 0) + 1;
      if (current.again[c.id] <= MAX_REQUEUE) current.queue.push(c);
    }
    current.pos++;
    resetCardState();
    return current.pos < current.queue.length;
  }

  function summary() {
    var s = { reviewed: current.results.length, unique: {}, again: 0, good: 0, hinted: 0, learned: 0, ms: Date.now() - current.startedAt, kind: current.kind };
    current.results.forEach(function (r) {
      s.unique[r.id] = 1;
      if (r.r === 0) s.again++; else if (r.r >= 2) s.good++;
      if (r.hint) s.hinted++;
    });
    var seen = {};
    current.results.forEach(function (r) { if (r.learned) seen[r.id] = 1; });
    s.learned = Object.keys(seen).length;
    s.unique = Object.keys(s.unique).length;
    return s;
  }

  var api = { plan: plan, start: start, get: get, end: end, card: card, rate: rate, summary: summary, shuffle: shuffle, BLOCK: BLOCK };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  R.session = api;
})(typeof window !== 'undefined' ? window : globalThis);
