/* Spaced-repetition scheduling. Mirror of recurser/srs.py, checked against tests/srs_vectors.json. */
(function (root) {
  'use strict';
  var DAY_MS = 86400000;
  var T = { againDelayMs: 600000, minEase: 1.3, matureDays: 21 };

  function configure(tuning) {
    if (!tuning) return;
    if (tuning.again_delay_ms) T.againDelayMs = tuning.again_delay_ms;
    if (tuning.min_ease) T.minEase = tuning.min_ease;
    if (tuning.mature_days) T.matureDays = tuning.mature_days;
  }

  function round2(x) { return Math.floor(x * 100 + 0.5) / 100; }

  function fresh() { return { ease: 2.5, interval: 0, reps: 0, lapses: 0, due: 0, last: 0 }; }

  /* Apply one review (rating 0 again, 1 hard, 2 good, 3 easy). Returns a new state. */
  function schedule(state, rating, nowMs) {
    if ([0, 1, 2, 3].indexOf(rating) < 0) throw new Error('rating must be 0, 1, 2 or 3');
    var s = Object.assign(fresh(), state || {});
    var ease = Number(s.ease), interval = Number(s.interval), reps = s.reps | 0, lapses = s.lapses | 0;
    if (rating === 0) {
      return { ease: round2(Math.max(T.minEase, ease - 0.2)), interval: 0, reps: 0,
               lapses: lapses + (reps > 0 ? 1 : 0), due: nowMs + T.againDelayMs, last: nowMs };
    }
    reps += 1;
    if (reps === 1) interval = 1;
    else if (reps === 2) interval = 3;
    else interval = round2(interval * ease);
    if (rating === 1) { interval = round2(Math.max(0.5, interval * 0.7)); ease = round2(Math.max(T.minEase, ease - 0.15)); }
    else if (rating === 3) { interval = round2(interval * 1.3); ease = round2(ease + 0.1); }
    return { ease: ease, interval: interval, reps: reps, lapses: lapses,
             due: nowMs + Math.floor(interval * DAY_MS + 0.5), last: nowMs };
  }

  /* new -> learning -> young -> mature */
  function stage(state) {
    if (!state || !state.last) return 'new';
    if (!state.reps) return 'learning';
    return state.interval >= T.matureDays ? 'mature' : 'young';
  }

  /* 0..1: how settled one card is; the mean over a project is its mastery. */
  function strength(state) {
    if (!state || !state.last || !state.reps) return 0;
    return Math.min(1, Number(state.interval) / T.matureDays);
  }

  /* Human label for the gap until a rating's next review. */
  function previewLabel(state, rating, nowMs) {
    var next = schedule(state, rating, nowMs);
    return spanLabel(next.due - nowMs);
  }

  function spanLabel(ms) {
    var min = ms / 60000;
    if (min < 60) return Math.max(1, Math.round(min)) + ' min';
    var h = min / 60;
    if (h < 22) return Math.round(h) + ' h';
    var d = h / 24;
    if (d < 30) return (d < 10 ? Math.round(d * 10) / 10 : Math.round(d)) + ' d';
    if (d < 365) return Math.round(d / 30.4 * 10) / 10 + ' mo';
    return Math.round(d / 365 * 10) / 10 + ' y';
  }

  var api = { DAY_MS: DAY_MS, configure: configure, round2: round2, fresh: fresh, schedule: schedule,
              stage: stage, strength: strength, previewLabel: previewLabel, spanLabel: spanLabel, tuning: T };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.Recurser = root.Recurser || {};
  root.Recurser.srs = api;
})(typeof window !== 'undefined' ? window : globalThis);
