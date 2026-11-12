/* Review history: one progress object, three places it can live.
   - this browser (always)
   - the localhost server started by `python -m recurser serve` (when the page is served by it)
   - the viewer's Claude account (when the page runs as a hosted artifact), private to that person
   Merging is per card: the most recent review wins. */
(function (root) {
  'use strict';
  var R = root.Recurser = root.Recurser || {};
  var srs = R.srs, model = R.model;
  var KEY = 'recurser.progress.v1';
  var LOG_CAP = 3000, DAY = 86400000;

  var progress = blank();
  var listeners = [];
  var status = { local: 'on', server: 'off', claude: 'off', note: '' };
  var caps = { db: null, uid: null, sample: null, downloads: null };

  function blank() { return { version: 1, updated: 0, cards: {}, log: [], days: {}, touch: {}, flags: {}, gaps: {}, prefs: {} }; }

  var PREF_DEFAULTS = { newPerDay: 10, sessionSize: 20, order: 'risk', typeFirst: false };
  function pref(name) { var v = (progress.prefs || {})[name]; return v == null ? PREF_DEFAULTS[name] : v; }

  function emit() { listeners.forEach(function (fn) { try { fn(progress, status); } catch (e) { /* a view failed to redraw */ } }); }
  function subscribe(fn) { listeners.push(fn); }

  /* ---------- merge (same rule as recurser/server.py) ---------- */
  function merge(a, b) {
    a = a || blank(); b = b || blank();
    var out = blank(), id, k;
    out.updated = Math.max(a.updated || 0, b.updated || 0);
    var newer = (a.updated || 0) >= (b.updated || 0) ? a : b, older = newer === a ? b : a;
    [older, newer].forEach(function (src) {
      for (id in (src.cards || {})) {
        var cur = out.cards[id], st = src.cards[id];
        if (!st) continue;
        if (!cur || (st.last || 0) >= (cur.last || 0)) out.cards[id] = st;
      }
      for (k in (src.touch || {})) out.touch[k] = Math.max(out.touch[k] || 0, src.touch[k]);
      for (k in (src.flags || {})) out.flags[k] = src.flags[k];
      for (k in (src.gaps || {})) { if (!out.gaps[k] || (src.gaps[k].t || 0) >= (out.gaps[k].t || 0)) out.gaps[k] = src.gaps[k]; }
      for (k in (src.days || {})) {
        var d = out.days[k] || { n: 0, ok: 0, fresh: 0 }, s = src.days[k] || {};
        out.days[k] = { n: Math.max(d.n, s.n || 0), ok: Math.max(d.ok, s.ok || 0), fresh: Math.max(d.fresh, s.fresh || 0) };
      }
    });
    var seen = {}, log = [];
    (older.log || []).concat(newer.log || []).forEach(function (e) {
      var key = e.id + '|' + e.t;
      if (e.id && !seen[key]) { seen[key] = 1; log.push(e); }
    });
    log.sort(function (x, y) { return x.t - y.t; });
    out.log = log.slice(-LOG_CAP);
    out.prefs = Object.assign({}, older.prefs || {}, newer.prefs || {});
    return out;
  }

  /* ---------- this browser ---------- */
  function loadLocal() {
    try {
      var raw = root.localStorage && root.localStorage.getItem(KEY);
      if (raw) { var data = JSON.parse(raw); if (data && typeof data === 'object') return Object.assign(blank(), data); }
    } catch (e) { status.local = 'unavailable'; }
    return blank();
  }
  function saveLocal() {
    try { if (root.localStorage) root.localStorage.setItem(KEY, JSON.stringify(progress)); }
    catch (e) { status.local = 'unavailable'; }
  }

  /* ---------- recording a review ---------- */
  function review(card, rating, opts) {
    opts = opts || {};
    var now = opts.now || Date.now(), key = model.dayKey(now);
    var day = progress.days[key] || (progress.days[key] = { n: 0, ok: 0, fresh: 0 });
    day.n++; if (rating >= 2) day.ok++;
    var entry = { id: card.id, t: now, r: rating };
    if (opts.hint) entry.h = 1;
    if (opts.cram) entry.c = 1;
    progress.log.push(entry);
    if (progress.log.length > LOG_CAP) progress.log = progress.log.slice(-LOG_CAP);
    if (!opts.cram) {
      var prev = progress.cards[card.id];
      var next = srs.schedule(prev, rating, now);
      next.first = (prev && prev.first) || now;
      if (!prev || !prev.last) day.fresh++;
      /* Learned is earned, not felt: an unaided correct recall at least a day after the previous review. */
      if (rating === 0) { /* a lapse clears it */ }
      else if (prev && prev.learned) next.learned = 1;
      else if (rating >= 2 && !opts.hint && prev && prev.reps >= 1 && now - prev.last >= DAY) next.learned = 1;
      progress.cards[card.id] = next;
      dirty[card.slug] = 1;
    }
    progress.touch[card.slug] = now;
    touch(now);
    return progress.cards[card.id];
  }

  function touch(now) { progress.updated = now || Date.now(); dirty.__meta = 1; saveLocal(); queueRemote(); emit(); }
  function setPref(name, value) { progress.prefs[name] = value; touch(); }
  function setFlag(id, flag) {
    if (flag) progress.flags[id] = flag; else delete progress.flags[id];
    touch();
  }
  /* "What did I miss?" — one line per card, written after a failed attempt. */
  function setGap(id, text) {
    text = String(text || '').trim().slice(0, 400);
    if (text) progress.gaps[id] = { t: Date.now(), text: text }; else delete progress.gaps[id];
    touch();
  }
  function exportData() { return JSON.parse(JSON.stringify(progress)); }
  function importData(data) {
    if (!data || typeof data !== 'object' || typeof data.cards !== 'object') throw new Error('That file is not a Recurser history export.');
    progress = merge(progress, Object.assign(blank(), data));
    markAllDirty(); touch(Math.max(progress.updated, Date.now()));
  }
  function reset() { progress = blank(); wipeRemote = true; markAllDirty(); touch(); }

  /* ---------- remote sync ---------- */
  var dirty = {}, timer = null, wipeRemote = false, knownSlugs = [];
  function markAllDirty() { knownSlugs.forEach(function (s) { dirty[s] = 1; }); dirty.__meta = 1; }
  function queueRemote() {
    if (status.server !== 'on' && status.claude !== 'on') return;
    clearTimeout(timer);
    timer = setTimeout(function () { flushRemote(false); }, 1500);
  }
  function flushRemote(leaving) {
    clearTimeout(timer); timer = null;
    if (status.server === 'on') pushServer(leaving === true);
    if (status.claude === 'on') pushClaude();
  }
  /* A review made a second before the tab closes must still be saved. */
  function flushIfPending() { if (timer) flushRemote(true); }
  if (root.addEventListener) {
    root.addEventListener('pagehide', flushIfPending);
    if (root.document) root.document.addEventListener('visibilitychange', function () { if (root.document.hidden) flushIfPending(); });
  }

  /* localhost server */
  function initServer() {
    if (!root.fetch || !/^https?:$/.test(root.location.protocol) || root.claude) return Promise.resolve(false);
    var ctl = root.AbortController ? new AbortController() : null;
    var t = setTimeout(function () { if (ctl) ctl.abort(); }, 2500);
    return root.fetch('api/health', ctl ? { signal: ctl.signal } : {})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (info) {
        clearTimeout(t);
        if (!info || !info.ok) return false;
        return root.fetch('api/progress').then(function (r) { return r.json(); }).then(function (remote) {
          status.server = 'on';
          var mine = progress.updated || 0, theirs = (remote && remote.updated) || 0;
          progress = merge(progress, Object.assign(blank(), remote || {}));
          saveLocal();
          if (mine > theirs) pushServer();          /* this browser holds reviews the server has not seen */
          emit();
          return true;
        });
      })
      .catch(function () { clearTimeout(t); return false; });
  }
  var serverBusy = false, serverAgain = false;
  function pushServer(leaving) {
    if (serverBusy && !leaving) { serverAgain = true; return; }
    serverBusy = true;
    root.fetch('api/progress', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(progress), keepalive: !!leaving })
      .then(function (r) { if (!r.ok) throw new Error('save refused'); status.note = ''; })
      .catch(function () { status.note = 'The localhost server did not accept the last save; this browser still has it.'; emit(); })
      .then(function () { serverBusy = false; if (serverAgain) { serverAgain = false; pushServer(); } });
  }

  /* hosted artifact: the viewer's private subtree, one document per project */
  function docId(slug) { return 'deck.' + String(slug).replace(/[^A-Za-z0-9_.\-]/g, '_'); }
  function cardsFor(slug) {
    var out = {}, prefix = slug + ':';
    for (var id in progress.cards) if (id.indexOf(prefix) === 0) out[id.slice(prefix.length)] = progress.cards[id];
    return out;
  }
  function initClaude(slugs) {
    knownSlugs = slugs || [];
    if (!root.claude || typeof root.claude.use !== 'function') return Promise.resolve(false);
    root.claude.use('sample').then(function (s) { caps.sample = s || null; emit(); }).catch(function () {});
    root.claude.use('downloads').then(function (d) { caps.downloads = d || null; emit(); }).catch(function () {});
    return Promise.all([root.claude.use('db'), root.claude.use('user')]).then(function (res) {
      var db = res[0], user = res[1];
      if (!db || !user) return false;
      return user.id().then(function (uid) {
        if (!uid) return false;
        caps.db = db; caps.uid = uid;
        status.claude = 'connecting'; emit();
        var slow = setTimeout(function () {
          if (status.claude === 'connecting') { status.note = 'Your Claude account storage has not answered yet; working from this browser’s copy.'; emit(); }
        }, 10000);
        return db.collection('data/users/' + uid).get().then(function (snap) {
          clearTimeout(slow);
          var remote = blank();
          snap.docs.forEach(function (doc) {
            var data = doc.data() || {};
            if (doc.id === 'meta') {
              remote.updated = data.updated || 0; remote.prefs = data.prefs || {}; remote.touch = data.touch || {};
              remote.flags = data.flags || {}; remote.days = data.days || {}; remote.gaps = data.gaps || {};
            } else if (doc.id.indexOf('deck.') === 0 && data.slug) {
              for (var key in (data.cards || {})) remote.cards[data.slug + ':' + key] = data.cards[key];
            }
          });
          var localOnly = {}, metaAhead = (progress.updated || 0) > (remote.updated || 0);
          for (var id in progress.cards) {
            var mine = progress.cards[id], theirs = remote.cards[id];
            if (!theirs || (mine.last || 0) > (theirs.last || 0)) localOnly[id.split(':')[0]] = 1;
          }
          progress = merge(progress, remote);
          saveLocal();
          status.claude = 'on'; status.note = '';
          for (var slug in localOnly) dirty[slug] = 1;
          if (Object.keys(localOnly).length || metaAhead) { dirty.__meta = 1; queueRemote(); }
          emit();
          return true;
        });
      });
    }).catch(function () { status.claude = 'off'; emit(); return false; });
  }
  var claudeBusy = false, claudeAgain = false;
  function pushClaude() {
    if (!caps.db || !caps.uid) return;
    if (claudeBusy) { claudeAgain = true; return; }
    claudeBusy = true;
    var col = caps.db.collection('data/users/' + caps.uid), jobs = Object.keys(dirty), chain = Promise.resolve();
    dirty = {};
    var wipe = wipeRemote; wipeRemote = false;
    jobs.forEach(function (job) {
      chain = chain.then(function () {
        if (job === '__meta') {
          var days = {}, keys = Object.keys(progress.days).sort().slice(-400);
          keys.forEach(function (k) { days[k] = progress.days[k]; });
          return col.doc('meta').set({ updated: progress.updated, prefs: progress.prefs, touch: progress.touch, flags: progress.flags, gaps: progress.gaps, days: days });
        }
        var cards = cardsFor(job);
        if (!Object.keys(cards).length) return wipe ? col.doc(docId(job)).delete() : null;
        return col.doc(docId(job)).set({ slug: job, cards: cards, updated: progress.updated });
      });
    });
    chain.then(function () { status.note = ''; })
      .catch(function (e) {
        jobs.forEach(function (j) { dirty[j] = 1; });
        status.note = e && e.code === 'quota_exceeded'
          ? 'Your Claude account storage for this page is full; history stays in this browser.'
          : 'Could not save to your Claude account just now; history stays in this browser and will be retried on the next review.';
        if (e && (e.code === 'revoked' || e.code === 'not_granted' || e.code === 'invalid_argument')) status.claude = 'off';
        emit();
      })
      .then(function () { claudeBusy = false; if (claudeAgain) { claudeAgain = false; pushClaude(); } });
  }

  function init(slugs) {
    progress = loadLocal();
    knownSlugs = slugs || [];
    emit();
    return Promise.all([initServer(), initClaude(slugs)]);
  }

  R.store = {
    init: init, subscribe: subscribe, get: function () { return progress; }, status: function () { return status; },
    caps: function () { return caps; }, review: review, pref: pref, setPref: setPref, setFlag: setFlag, setGap: setGap,
    exportData: exportData, importData: importData, reset: reset, merge: merge, blank: blank,
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = R.store;
})(typeof window !== 'undefined' ? window : globalThis);
