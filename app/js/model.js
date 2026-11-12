/* Derived data: what is due, what to learn next, how much of each project is still in memory. Pure functions. */
(function (root) {
  'use strict';
  var R = root.Recurser = root.Recurser || {};
  var srs = R.srs || (typeof require !== 'undefined' ? require('./srs.js') : null);
  var DAY = 86400000;

  function index(bundle) {
    var ix = { bundle: bundle, cardsById: {}, cardsBySlug: {}, projectBySlug: {}, projectByHash: {} };
    bundle.projects.forEach(function (p) {
      ix.projectBySlug[p.slug] = p;
      ix.projectByHash[hashName(p.slug)] = p;
      ix.cardsBySlug[p.slug] = [];
    });
    bundle.cards.forEach(function (c) {
      ix.cardsById[c.id] = c;
      (ix.cardsBySlug[c.slug] = ix.cardsBySlug[c.slug] || []).push(c);
    });
    ix.tuning = bundle.tuning || { base_half_life_days: 21, mastery_gain: 9, mature_days: 21, tiers: [] };
    if (srs) srs.configure(ix.tuning);
    return ix;
  }

  /* '#p-…' route names may only hold letters, digits and hyphens. */
  function hashName(slug) { return 'p-' + String(slug).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, ''); }

  function dayKey(ms) {
    var d = new Date(ms);
    return d.getFullYear() + '-' + ('0' + (d.getMonth() + 1)).slice(-2) + '-' + ('0' + d.getDate()).slice(-2);
  }

  function daysSince(iso, now) {
    if (!iso) return null;
    var t = Date.parse(iso);
    return isNaN(t) ? null : Math.max(0, (now - t) / DAY);
  }

  function tierFor(ix, days) {
    var tiers = ix.tuning.tiers || [];
    for (var i = 0; i < tiers.length; i++) if (days <= tiers[i].max_days) return tiers[i];
    return tiers[tiers.length - 1] || { key: 'frozen', label: 'archaeology' };
  }

  /* Share of one card still retrievable. Unseen cards fade from the last push; seen ones from their last review. */
  function cardRetention(ix, state, pushDays, now) {
    var base = ix.tuning.base_half_life_days, gain = ix.tuning.mastery_gain;
    if (!state || !state.last) return pushDays == null ? 0 : Math.pow(0.5, pushDays / base);
    var half = base * (1 + gain * srs.strength(state));
    return Math.pow(0.5, Math.max(0, (now - state.last) / DAY) / half);
  }

  function isSuspended(progress, id) { return progress.flags && progress.flags[id] === 'suspended'; }

  function projectStats(ix, progress, slug, now) {
    var p = ix.projectBySlug[slug], cards = ix.cardsBySlug[slug] || [];
    var pushDays = daysSince(p.pushed_at, now);
    var s = { slug: slug, total: cards.length, fresh: 0, learning: 0, young: 0, mature: 0, due: 0, learned: 0,
              mastery: 0, retention: 0, pushDays: pushDays, lastReview: 0 };
    var strengthSum = 0, retSum = 0;
    cards.forEach(function (c) {
      var st = progress.cards[c.id], stage = srs.stage(st);
      if (stage === 'new') s.fresh++; else s[stage]++;
      if (st && st.last) {
        if (st.due <= now && !isSuspended(progress, c.id)) s.due++;
        if (st.learned) s.learned++;
        if (st.last > s.lastReview) s.lastReview = st.last;
      }
      strengthSum += srs.strength(st);
      retSum += cardRetention(ix, st, pushDays, now);
    });
    if (cards.length) { s.mastery = strengthSum / cards.length; s.retention = retSum / cards.length; }
    s.tier = tierFor(ix, pushDays == null ? 1e9 : pushDays);
    s.seen = s.total - s.fresh;
    return s;
  }

  function allStats(ix, progress, now) {
    return ix.bundle.projects.map(function (p) { return projectStats(ix, progress, p.slug, now); });
  }

  /* Ladder order inside a project: every shelf's most fundamental card first (rank, then category). */
  function ladder(a, b) { return a.rank - b.rank || a.category - b.category; }

  function matches(card, f) {
    if (!f) return true;
    if (f.slug && card.slug !== f.slug) return false;
    if (f.category != null && card.category !== f.category) return false;
    if (f.types && f.types.indexOf(card.type) < 0) return false;
    if (f.maxLevel && card.level > f.maxLevel) return false;
    return true;
  }

  /* Cards whose review time has come, most overdue first. */
  function dueQueue(ix, progress, now, filter) {
    var out = [];
    ix.bundle.cards.forEach(function (c) {
      var st = progress.cards[c.id];
      if (st && st.last && st.due <= now && !isSuspended(progress, c.id) && matches(c, filter)) out.push(c);
    });
    out.sort(function (a, b) { return progress.cards[a.id].due - progress.cards[b.id].due; });
    return out;
  }

  /* Unseen cards in the order they should be introduced: one project at a time, most forgotten project first. */
  function newQueue(ix, progress, now, filter, order) {
    var onDemand = filter && filter.slug;
    var stats = allStats(ix, progress, now).filter(function (s) { return s.fresh > 0 && (onDemand || s.tier.key !== 'frozen'); });
    stats.sort(order === 'fresh'
      ? function (a, b) { return (a.pushDays == null ? 1e9 : a.pushDays) - (b.pushDays == null ? 1e9 : b.pushDays); }
      : function (a, b) { return a.retention - b.retention || b.fresh - a.fresh; });
    var out = [];
    stats.forEach(function (s) {
      (ix.cardsBySlug[s.slug] || []).slice().sort(ladder).forEach(function (c) {
        var st = progress.cards[c.id];
        if ((!st || !st.last) && !c.stale && !isSuspended(progress, c.id) && matches(c, filter)) out.push(c);
      });
    });
    return out;
  }

  function introducedToday(progress, now) {
    var key = dayKey(now), n = 0;
    for (var id in progress.cards) { var st = progress.cards[id]; if (st && st.first && dayKey(st.first) === key) n++; }
    return n;
  }

  /* Cards due per day for the coming days (index 0 = today, overdue included). */
  function forecast(ix, progress, now, days) {
    var start = new Date(now); start.setHours(0, 0, 0, 0);
    var t0 = start.getTime(), out = [];
    for (var i = 0; i < days; i++) out.push({ day: t0 + i * DAY, count: 0 });
    ix.bundle.cards.forEach(function (c) {
      var st = progress.cards[c.id];
      if (!st || !st.last || isSuspended(progress, c.id)) return;
      var slot = Math.max(0, Math.floor((st.due - t0) / DAY));
      if (slot < days) out[slot].count++;
    });
    return out;
  }

  function streak(progress, now) {
    var n = 0, t = now;
    if (!(progress.days || {})[dayKey(t)]) t -= DAY;          // today not started yet: count up to yesterday
    while ((progress.days || {})[dayKey(t)]) { n++; t -= DAY; }
    return n;
  }

  function totals(ix, progress, now) {
    var t = { cards: ix.bundle.cards.length, seen: 0, learned: 0, due: 0, mature: 0 };
    ix.bundle.cards.forEach(function (c) {
      var st = progress.cards[c.id];
      if (!st || !st.last) return;
      t.seen++;
      if (st.learned) t.learned++;
      if (st.interval >= ix.tuning.mature_days) t.mature++;
      if (st.due <= now && !isSuspended(progress, c.id)) t.due++;
    });
    return t;
  }

  /* 'h____ b__ c_____' — the answer reduced to first letters (aided recall). */
  function firstLetters(text) {
    return String(text || '').replace(/[A-Za-z0-9_]+/g, function (w) { return w.length > 1 ? w[0] + new Array(w.length).join('·') : w; });
  }

  /* Split an answer into separately checkable parts: trace steps, or "(1) ... (2) ..." points. */
  function answerParts(card) {
    if (card.type === 'trace' && card.steps && card.steps.length) return card.steps.slice();
    var text = String(card.answer || ''), marks = text.match(/\(\d\)/g);
    if (!marks || marks.length < 2) return [];
    return text.split(/\(\d\)/).slice(1).map(function (t) { return t.trim(); }).filter(Boolean);
  }
  /* all recalled -> good (2), one miss -> hard (1), more -> again (0) */
  function ratingFromTicks(total, ticked) { var missed = total - ticked; return missed <= 0 ? 2 : missed === 1 ? 1 : 0; }
  function sameOutput(a, b) {
    function norm(x) { return String(x || '').replace(/\s+/g, ' ').trim().replace(/^["'`]|["'`]$/g, ''); }
    return !!norm(a) && norm(a) === norm(b);
  }

  function clozeParts(text) {
    var parts = [], re = /\{\{c\d+::([^{}]+?)\}\}/g, last = 0, m;
    while ((m = re.exec(text))) {
      if (m.index > last) parts.push({ text: text.slice(last, m.index) });
      parts.push({ text: m[1], hidden: true });
      last = m.index + m[0].length;
    }
    if (last < text.length) parts.push({ text: text.slice(last) });
    return parts;
  }

  var api = { index: index, hashName: hashName, dayKey: dayKey, daysSince: daysSince, tierFor: tierFor,
              cardRetention: cardRetention, projectStats: projectStats, allStats: allStats, dueQueue: dueQueue,
              newQueue: newQueue, introducedToday: introducedToday, forecast: forecast, streak: streak, totals: totals,
              firstLetters: firstLetters, clozeParts: clozeParts, answerParts: answerParts, ratingFromTicks: ratingFromTicks, sameOutput: sameOutput, ladder: ladder, isSuspended: isSuspended, DAY: DAY };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  R.model = api;
})(typeof window !== 'undefined' ? window : globalThis);
