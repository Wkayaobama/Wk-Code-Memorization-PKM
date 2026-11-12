/* Boot, routing and redraws. Routes are plain #names so they survive every host. */
(function (root) {
  'use strict';
  var R = root.Recurser, ui = R.ui, h = ui.h, store = R.store, model = R.model, views = R.views;
  var bundle = root.RECURSER_BUNDLE;
  var view = document.getElementById('view'), app = document.getElementById('app'), main = document.getElementById('main');
  if (!bundle || !bundle.cards || !bundle.projects) {
    ui.clear(view).appendChild(h('p', { class: 'empty', text: 'No deck data was found. Build it with: python -m recurser build' }));
    return;
  }
  var ix = model.index(bundle);
  var TABS = [['today', 'Today'], ['projects', 'Projects'], ['matrix', 'Matrix'], ['inventory', 'Inventory'], ['sync', 'Sync']];
  var ctx = { ix: ix, bundle: bundle, render: render };

  function routeName() { return (root.location.hash || '#today').replace(/^#/, '') || 'today'; }

  function drawNav(name) {
    var active = name.indexOf('p-') === 0 ? 'projects' : name;
    ['nav', 'tabbar'].forEach(function (id) {
      var el = ui.clear(document.getElementById(id));
      TABS.forEach(function (t) {
        el.appendChild(h('a', { href: '#' + t[0], 'aria-current': active === t[0] ? 'page' : null }, id === 'tabbar' ? ui.icon(t[0]) : null, t[1]));
      });
    });
  }
  function drawSync() {
    var s = store.status(), parts = [];
    if (s.local === 'on') parts.push('browser');
    if (s.server === 'on') parts.push('localhost');
    if (s.claude === 'on') parts.push('Claude');
    if (s.claude === 'connecting') parts.push('connecting\u2026');
    document.getElementById('sync-chip').textContent = parts.length ? 'saved \u00b7 ' + parts.join(' + ') : 'history not saved';
  }

  function render() {
    var name = routeName();
    if (name === 'review' && !R.session.get()) { root.location.hash = '#today'; return; }
    ui.clear(view);
    app.classList.toggle('in-session', name === 'review');
    if (name === 'review') views.review(view, ctx);
    else if (name.indexOf('p-') === 0 && ix.projectByHash[name]) views.project(view, ctx, ix.projectByHash[name]);
    else if (TABS.some(function (t) { return t[0] === name; })) views[name](view, ctx);
    else { root.location.hash = '#today'; return; }
    drawNav(name);
    drawSync();
  }

  root.addEventListener('hashchange', function () { ui.notice(''); render(); main.scrollTop = 0; });
  document.addEventListener('keydown', function (ev) { if (routeName() === 'review') views.keys(ctx, ev); });
  store.subscribe(function () {
    drawSync();
    if (routeName() === 'review') return;                      /* the session redraws itself */
    var a = document.activeElement;
    if (a && view.contains(a) && /^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName)) return;   /* do not steal focus while typing */
    render();
  });
  store.init(bundle.projects.map(function (p) { return p.slug; }));
  render();
  setInterval(function () { if (routeName() === 'today' && !document.hidden) render(); }, 60000);
})(window);
