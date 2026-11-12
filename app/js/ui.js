/* Small DOM helpers. Everything that comes from a deck is set as text, never as markup. */
(function (root) {
  'use strict';
  var R = root.Recurser = root.Recurser || {};
  var SVG = 'http://www.w3.org/2000/svg';

  function h(tag, attrs) {
    var el = document.createElement(tag), i, k;
    attrs = attrs || {};
    for (k in attrs) {
      var v = attrs[k];
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k === 'rich') rich(el, v);
      else if (k === 'on') { for (var ev in v) el.addEventListener(ev, v[ev]); }
      else if (k === 'style') { for (var s in v) el.style.setProperty(s, v[s]); }
      else if (k === 'value' || k === 'checked' || k === 'disabled' || k === 'hidden' || k === 'open') el[k] = v;
      else el.setAttribute(k, v === true ? '' : v);
    }
    for (i = 2; i < arguments.length; i++) append(el, arguments[i]);
    return el;
  }
  function append(el, child) {
    if (child == null || child === false) return;
    if (Array.isArray(child)) { child.forEach(function (c) { append(el, c); }); return; }
    el.appendChild(child.nodeType ? child : document.createTextNode(String(child)));
  }
  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
  // Inline code: a pair of backticks around a name becomes a <code> element.  Built from DOM nodes
  // only, never from markup, so card text can never inject anything.  Unbalanced ticks stay as typed.
  function rich(el, text) {
    var str = String(text == null ? '' : text), parts = str.split('`'), i, c;
    if (parts.length < 3 || parts.length % 2 === 0) { el.textContent = str; return el; }
    for (i = 0; i < parts.length; i++) {
      if (!parts[i]) continue;
      if (i % 2) { c = document.createElement('code'); c.textContent = parts[i]; el.appendChild(c); }
      else el.appendChild(document.createTextNode(parts[i]));
    }
    return el;
  }

  var ICONS = {
    today: 'M12 7v5l3 2M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z',
    projects: 'M4 6.5 12 3l8 3.5-8 3.5L4 6.5Zm0 5.5 8 3.5 8-3.5M4 17.5 12 21l8-3.5',
    matrix: 'M4 4h6v6H4V4Zm10 0h6v6h-6V4ZM4 14h6v6H4v-6Zm10 0h6v6h-6v-6Z',
    inventory: 'M4 6h16M4 12h16M4 18h10',
    sync: 'M4 12a8 8 0 0 1 13.7-5.7L20 8.5M20 4v4.5h-4.5M20 12a8 8 0 0 1-13.7 5.7L4 15.5M4 20v-4.5h4.5',
  };
  function icon(name) {
    var svg = document.createElementNS(SVG, 'svg'), path = document.createElementNS(SVG, 'path');
    svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('fill', 'none'); svg.setAttribute('aria-hidden', 'true');
    path.setAttribute('d', ICONS[name] || ''); path.setAttribute('stroke', 'currentColor');
    path.setAttribute('stroke-width', '1.7'); path.setAttribute('stroke-linecap', 'round'); path.setAttribute('stroke-linejoin', 'round');
    svg.appendChild(path);
    return svg;
  }

  function code(c) { c = String(c || ''); return c.length === 4 ? c.slice(0, 2) + '·' + c.slice(2) : c; }
  function pct(x) { return Math.round((x || 0) * 100) + '%'; }
  function ago(days) {
    if (days == null) return 'unknown';
    if (days < 1) return 'today';
    if (days < 14) return Math.round(days) + ' d ago';
    if (days < 70) return Math.round(days / 7) + ' wk ago';
    if (days < 365) return Math.round(days / 30.4) + ' mo ago';
    return (Math.round(days / 36.5) / 10) + ' y ago';
  }
  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : (many || one + 's')); }
  function shortDate(ms) { return new Date(ms).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }); }

  /* Retention meter: the fill carries severity, the track is a lighter step of the same ramp. */
  function meter(value, label) {
    var cls = value >= 0.6 ? '' : value >= 0.3 ? ' warn' : ' crit';
    return h('div', { class: 'meter' + cls, role: 'img', 'aria-label': label + ' ' + pct(value) },
      h('span', { style: { width: Math.max(2, Math.round(value * 100)) + '%' } }));
  }
  function tierPill(tier) {
    var map = { hot: 'good', warm: 'accent', cooling: 'warn', cold: 'serious', frozen: '' };
    return h('span', { class: 'pill' }, h('span', { class: 'dot ' + (map[tier.key] || '') }), tier.label);
  }
  function codeBox(text, wrap) { return h('div', { class: 'codebox' + (wrap ? ' wrapping' : '') }, h('pre', { text: text })); }

  /* Offer a generated file. Hosted pages must go through the downloads capability; plain pages use a blob link. */
  function saveFile(filename, text, caps) {
    if (caps && caps.downloads) return caps.downloads.save({ filename: filename, data: text }).then(function () { return 'saved'; });
    return new Promise(function (resolve, reject) {
      try {
        var blob = new Blob([text], { type: 'application/octet-stream' }), url = URL.createObjectURL(blob);
        var a = h('a', { href: url, download: filename });
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
        resolve('saved');
      } catch (e) { reject(e); }
    });
  }

  function notice(text) {
    var el = document.getElementById('notice');
    if (!el) return;
    el.textContent = text || '';
    el.hidden = !text;
  }

  R.ui = { h: h, clear: clear, rich: rich, icon: icon, code: code, pct: pct, ago: ago, plural: plural, shortDate: shortDate,
           meter: meter, tierPill: tierPill, codeBox: codeBox, saveFile: saveFile, notice: notice };
})(window);
