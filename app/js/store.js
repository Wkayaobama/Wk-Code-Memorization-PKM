/* Review history: one progress object, three places it can live.
   - this browser (always)
   - the localhost server started by `python -m recurser serve` (when the page is served by it)
   - the viewer's Claude account (when the page runs as a hosted artifact), private to that person
   Every write folds in what is already stored, so a second tab or device never overwrites newer reviews.
   The merge rules are the ones in recurser/server.py; both are checked against tests/srs_vectors.json. */
(function (root) {
  'use strict';
  var R = root.Recurser = root.Recurser || {};
  var srs = R.srs, model = R.model;
  /* On a team server the history is kept per person, in this browser too: two people (or one person trying a
     teammate's link) on the same browser profile never mix their reviews.  The server says who is signed in
     in the same script that carries the decks, so the right key is known before anything is read. */
  var viewer = root.RECURSER_VIEWER || {};
  var BASE_KEY = 'recurser.progress.v1';
  var KEY = BASE_KEY + (viewer.team && viewer.user ? '.' + viewer.user : '');
  var LOG_CAP = 3000, DAY = 86400000, KEEPALIVE_MAX = 60000;

  var progress = blank();
  var listeners = [];
  var status = { local: 'on', server: 'off', claude: 'off', note: '', user: viewer.user || '', team: !!viewer.team };
  var caps = { db: null, uid: null, sample: null, downloads: null, assistant: false };

  function blank() {
    return { version: 1, updated: 0, resetAt: 0, restoredAt: 0, cards: {}, log: [], days: {}, touch: {}, flags: {}, gaps: {}, prefs: {} };
  }

  var PREF_DEFAULTS = { newPerDay: 10, sessionSize: 20, order: 'risk', typeFirst: false };
  function pref(name) { var v = (progress.prefs || {})[name]; return v == null ? PREF_DEFAULTS[name] : v; }

  function emit() { listeners.forEach(function (fn) { try { fn(progress, status); } catch (e) { /* a view failed to redraw */ } }); }
  function subscribe(fn) { listeners.push(fn); }

  /* ---------- shape ---------- */
  function num(x) { return typeof x === 'number' && isFinite(x) ? x : 0; }
  function table(x) { return x && typeof x === 'object' && !Array.isArray(x) ? x : {}; }
  /* {v: 'suspended' | null, t: ms}: a cleared flag stays as a tombstone so the clearing travels. */
  function flagOf(value) {
    if (value && typeof value === 'object') return { v: typeof value.v === 'string' && value.v ? value.v : null, t: num(value.t) };
    return { v: typeof value === 'string' && value ? value : null, t: 0 };
  }
  /* {text, t}: an empty text is the tombstone of a cleared note. */
  function gapOf(value) {
    if (value && typeof value === 'object') return { text: String(value.text || '').slice(0, 400), t: num(value.t) };
    return { text: String(value || '').slice(0, 400), t: 0 };
  }
  function clean(data) {
    data = table(data);
    var out = blank(), k, src;
    out.updated = num(data.updated); out.resetAt = num(data.resetAt); out.restoredAt = num(data.restoredAt);
    src = table(data.cards);
    for (k in src) {
      if (!src[k] || typeof src[k] !== 'object') continue;
      var st = {}, f;
      for (f in src[k]) if (typeof src[k][f] === 'number' && isFinite(src[k][f])) st[f] = src[k][f];
      out.cards[k] = st;
    }
    (Array.isArray(data.log) ? data.log : []).forEach(function (e) {
      if (!e || typeof e !== 'object' || typeof e.id !== 'string' || !e.id) return;
      var item = { id: e.id, t: num(e.t), r: num(e.r) };
      if (e.h) item.h = 1;
      if (e.c) item.c = 1;
      out.log.push(item);
    });
    src = table(data.days);
    for (k in src) {
      if (!src[k] || typeof src[k] !== 'object') continue;
      out.days[k] = { n: Math.trunc(num(src[k].n)), ok: Math.trunc(num(src[k].ok)), fresh: Math.trunc(num(src[k].fresh)), t: num(src[k].t) };
    }
    src = table(data.touch); for (k in src) out.touch[k] = num(src[k]);
    src = table(data.flags); for (k in src) out.flags[k] = flagOf(src[k]);
    src = table(data.gaps); for (k in src) out.gaps[k] = gapOf(src[k]);
    src = table(data.prefs);
    for (k in src) {
      var v = src[k];
      if (typeof v === 'string' || typeof v === 'boolean' || (typeof v === 'number' && isFinite(v))) out.prefs[k] = v;
    }
    return out;
  }

  /* ---------- merge (same rules as recurser/server.py) ----------
     cards: the most recent review wins · log: union · flags and gap notes: the most recent change wins ·
     day counters: the larger figure · resetAt: everything dated at or before it is dropped, unless a
     saved history was restored after it (restoredAt). */
  function merge(a, b) {
    a = clean(a); b = clean(b);
    var out = blank(), id, k;
    out.updated = Math.max(a.updated, b.updated);
    out.resetAt = Math.max(a.resetAt, b.resetAt);
    out.restoredAt = Math.max(a.restoredAt, b.restoredAt);
    var wall = out.resetAt > out.restoredAt ? out.resetAt : 0;
    function alive(t) { return !(wall > 0 && t <= wall); }
    var newer = a.updated >= b.updated ? a : b, older = newer === a ? b : a;
    var cards = {}, flags = {}, gaps = {}, touch = {}, days = {};
    [older, newer].forEach(function (src) {
      for (id in src.cards) { if (!cards[id] || num(src.cards[id].last) >= num(cards[id].last)) cards[id] = src.cards[id]; }
      for (id in src.flags) { if (!flags[id] || src.flags[id].t >= flags[id].t) flags[id] = src.flags[id]; }
      for (id in src.gaps) { if (!gaps[id] || src.gaps[id].t >= gaps[id].t) gaps[id] = src.gaps[id]; }
      for (k in src.touch) touch[k] = Math.max(touch[k] || 0, src.touch[k]);
      for (k in src.days) {
        var d = days[k] || { n: 0, ok: 0, fresh: 0, t: 0 }, s = src.days[k];
        days[k] = { n: Math.max(d.n, s.n), ok: Math.max(d.ok, s.ok), fresh: Math.max(d.fresh, s.fresh), t: Math.max(d.t, s.t) };
      }
    });
    for (id in cards) if (alive(num(cards[id].last))) out.cards[id] = cards[id];
    for (id in flags) if (alive(flags[id].t)) out.flags[id] = flags[id];
    for (id in gaps) if (alive(gaps[id].t)) out.gaps[id] = gaps[id];
    for (k in touch) if (alive(touch[k])) out.touch[k] = touch[k];
    for (k in days) if (alive(days[k].t)) out.days[k] = days[k];
    var seen = {}, log = [];
    older.log.concat(newer.log).forEach(function (e) {
      var key = e.id + '|' + e.t;
      if (!seen[key] && alive(e.t)) { seen[key] = 1; log.push(e); }
    });
    log.sort(function (x, y) { return x.t - y.t || (x.id < y.id ? -1 : x.id > y.id ? 1 : 0); });
    out.log = log.slice(-LOG_CAP);
    for (k in older.prefs) out.prefs[k] = older.prefs[k];
    for (k in newer.prefs) out.prefs[k] = newer.prefs[k];
    return out;
  }

  /* ---------- this browser ---------- */
  function readStored() {
    try {
      var raw = root.localStorage && root.localStorage.getItem(KEY);
      return raw ? clean(JSON.parse(raw)) : null;
    } catch (e) { status.local = 'unavailable'; return null; }
  }
  function loadLocal() { return readStored() || blank(); }
  function saveLocal() {
    try {
      if (!root.localStorage) return;
      var stored = readStored();
      if (stored) progress = merge(stored, progress);          /* another tab may have written since this one loaded */
      root.localStorage.setItem(KEY, JSON.stringify(progress));
    } catch (e) { status.local = 'unavailable'; }
  }
  if (root.addEventListener) {
    root.addEventListener('storage', function (ev) {          /* a review made in another tab shows up here at once */
      if (ev.key !== KEY || !ev.newValue) return;
      try { progress = merge(progress, JSON.parse(ev.newValue)); emit(); } catch (e) { /* not ours */ }
    });
  }

  /* ---------- recording a review ---------- */
  function review(card, rating, opts) {
    opts = opts || {};
    var now = opts.now || Date.now(), key = model.dayKey(now);
    var day = progress.days[key] || (progress.days[key] = { n: 0, ok: 0, fresh: 0, t: 0 });
    day.n++; if (rating >= 2) day.ok++;
    day.t = now;
    var entry = { id: card.id, t: now, r: rating };
    if (opts.hint) entry.h = 1;
    if (opts.cram) entry.c = 1;
    progress.log.push(entry);
    if (progress.log.length > LOG_CAP) progress.log = progress.log.slice(-LOG_CAP);
    var prev = progress.cards[card.id];
    if (!opts.cram) {
      var next = srs.schedule(prev, rating, now);
      next.first = (prev && prev.first) || now;
      if (!prev || !prev.last) day.fresh++;
      /* Learned is earned, not felt: an unaided correct recall at least a day after the card was last on
         screen, whether that was a review or a practice run.  A lapse clears it. */
      var lastSeen = prev ? Math.max(prev.last || 0, prev.seen || 0) : 0;
      if (rating === 0) { /* cleared */ }
      else if (prev && prev.learned) next.learned = 1;
      else if (rating >= 2 && !opts.hint && lastSeen && now - lastSeen >= DAY) next.learned = 1;
      progress.cards[card.id] = next;
      dirty[card.slug] = 1;
    } else if (prev) {
      prev.seen = now;                                         /* practice is an exposure: the 24 h clock restarts */
      dirty[card.slug] = 1;
    }
    progress.touch[card.slug] = now;
    touch(now);
    return progress.cards[card.id];
  }

  function touch(now) { progress.updated = now || Date.now(); dirty.__meta = 1; saveLocal(); queueRemote(); emit(); }
  function setPref(name, value) { progress.prefs[name] = value; touch(); }
  function flag(id) { var f = progress.flags[id]; return f && typeof f === 'object' ? f.v : (f || null); }
  function setFlag(id, value) { progress.flags[id] = { v: value || null, t: Date.now() }; touch(); }
  /* "What did I miss?" — one line per card, written after a failed attempt.  An empty text clears it. */
  function setGap(id, text) {
    progress.gaps[id] = { t: Date.now(), text: String(text || '').trim().slice(0, 400) };
    touch();
  }
  function exportData() { return JSON.parse(JSON.stringify(progress)); }
  function importData(data) {
    if (!data || typeof data !== 'object' || typeof data.cards !== 'object') throw new Error('That file is not a Recurser history export.');
    var now = Date.now();
    progress.restoredAt = now;                                 /* a restored file outranks an earlier "Erase history" */
    progress = merge(progress, data);
    markAllDirty(); touch(Math.max(progress.updated, now));
  }
  /* Erase every review everywhere: the erase time travels with the history and drops older entries on every device. */
  function reset() {
    var now = Date.now(), prefs = progress.prefs;
    progress = blank();
    progress.resetAt = now; progress.prefs = prefs;
    try { if (root.localStorage) root.localStorage.setItem(KEY, JSON.stringify(progress)); } catch (e) { status.local = 'unavailable'; }
    markAllDirty(); touch(now);
  }

  /* ---------- remote sync ---------- */
  var dirty = {}, timer = null, knownSlugs = [], retry = false;
  function markAllDirty() { knownSlugs.forEach(function (s) { dirty[s] = 1; }); dirty.__meta = 1; }
  function queueRemote() {
    if (status.server !== 'on' && status.claude !== 'on') return;
    clearTimeout(timer);
    timer = setTimeout(function () { flushRemote(false); }, 900);
  }
  function flushRemote(leaving) {
    clearTimeout(timer); timer = null;
    if (status.server === 'on') pushServer(leaving === true);
    if (status.claude === 'on') pushClaude();
  }
  /* A review made a second before the tab closes must still be saved; a failed save is retried on return. */
  function flushIfPending() { if (timer) flushRemote(true); }
  if (root.addEventListener) {
    root.addEventListener('pagehide', flushIfPending);
    if (root.document) root.document.addEventListener('visibilitychange', function () {
      if (root.document.hidden) flushIfPending();
      else if (retry) { retry = false; markAllDirty(); queueRemote(); }
    });
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
        caps.assistant = !!info.assistant; status.user = info.user || ''; status.team = !!info.team;
        if (info.team && info.user && KEY !== BASE_KEY + '.' + info.user) {   /* an older bundle did not name the person */
          KEY = BASE_KEY + '.' + info.user; progress = loadLocal();
        }
        return root.fetch('api/progress').then(function (r) { return r.json(); }).then(function (remote) {
          status.server = 'on';
          var before = JSON.stringify(clean(remote));
          progress = merge(progress, remote);
          saveLocal();
          if (JSON.stringify(progress) !== before) pushServer();   /* this browser holds something the server has not seen */
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
    var body = JSON.stringify(progress);
    /* browsers refuse a keepalive request above 64 KiB: a long history goes out as a normal request instead */
    root.fetch('api/progress', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: body, keepalive: !!leaving && body.length <= KEEPALIVE_MAX })
      .then(function (r) { if (!r.ok) throw new Error('save refused'); return r.json(); })
      .then(function (merged) {
        var before = JSON.stringify(progress);
        progress = merge(progress, merged);                     /* what other browsers saved meanwhile */
        status.note = '';
        if (JSON.stringify(progress) !== before) { saveLocal(); emit(); }
      })
      .catch(function () { retry = true; status.note = 'The localhost server did not accept the last save; this browser still has it.'; emit(); })
      .then(function () { serverBusy = false; if (serverAgain) { serverAgain = false; pushServer(); } });
  }

  /* hosted artifact: the viewer's private subtree, one document per project */
  function docId(slug) { return 'deck.' + String(slug).replace(/[^A-Za-z0-9_.\-]/g, '_'); }
  function cardsFor(slug) {
    var out = {}, prefix = slug + ':';
    for (var id in progress.cards) if (id.indexOf(prefix) === 0) out[id.slice(prefix.length)] = progress.cards[id];
    return out;
  }
  function fromSnapshot(snap) {
    var remote = blank();
    snap.docs.forEach(function (doc) {
      var data = doc.data() || {};
      if (doc.id === 'meta') {
        remote.updated = data.updated || 0; remote.resetAt = data.resetAt || 0; remote.restoredAt = data.restoredAt || 0;
        remote.prefs = data.prefs || {}; remote.touch = data.touch || {};
        remote.flags = data.flags || {}; remote.days = data.days || {}; remote.gaps = data.gaps || {};
      } else if (doc.id.indexOf('deck.') === 0 && data.slug) {
        for (var key in (data.cards || {})) remote.cards[data.slug + ':' + key] = data.cards[key];
      }
    });
    return clean(remote);
  }
  function metaDoc() {
    var days = {}, keys = Object.keys(progress.days).sort().slice(-400);
    keys.forEach(function (k) { days[k] = progress.days[k]; });
    return { updated: progress.updated, resetAt: progress.resetAt, restoredAt: progress.restoredAt, prefs: progress.prefs,
             touch: progress.touch, flags: progress.flags, gaps: progress.gaps, days: days };
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
          var remote = fromSnapshot(snap);
          progress = merge(progress, remote);
          saveLocal();
          status.claude = 'on'; status.note = '';
          var ahead = false;
          for (var id in progress.cards) {                      /* only what survived the merge is worth uploading */
            var theirs = remote.cards[id];
            if (!theirs || num(progress.cards[id].last) > num(theirs.last)) { dirty[id.split(':')[0]] = 1; ahead = true; }
          }
          if (ahead || progress.updated > remote.updated || progress.resetAt > remote.resetAt || progress.restoredAt > remote.restoredAt) {
            dirty.__meta = 1; queueRemote();
          }
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
    var col = caps.db.collection('data/users/' + caps.uid), jobs = Object.keys(dirty);
    dirty = {};
    /* Read what the account holds first: a tab left open for days folds newer reviews in instead of replacing them. */
    col.get().then(function (snap) {
      var before = JSON.stringify(progress);
      progress = merge(progress, fromSnapshot(snap));
      if (JSON.stringify(progress) !== before) { saveLocal(); emit(); }
      var chain = Promise.resolve();
      jobs.forEach(function (job) {
        chain = chain.then(function () {
          if (job === '__meta') return col.doc('meta').set(metaDoc());
          var cards = cardsFor(job);
          if (!Object.keys(cards).length) return col.doc(docId(job)).delete().catch(function () { /* nothing was stored for it */ });
          return col.doc(docId(job)).set({ slug: job, cards: cards, updated: progress.updated });
        });
      });
      return chain;
    }).then(function () { status.note = ''; })
      .catch(function (e) {
        jobs.forEach(function (j) { dirty[j] = 1; });
        retry = true;
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
    caps: function () { return caps; }, review: review, pref: pref, setPref: setPref, flag: flag, setFlag: setFlag, setGap: setGap,
    exportData: exportData, importData: importData, reset: reset, merge: merge, clean: clean, blank: blank,
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = R.store;
})(typeof window !== 'undefined' ? window : globalThis);
