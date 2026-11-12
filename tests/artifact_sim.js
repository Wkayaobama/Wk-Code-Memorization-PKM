// Simulated hosted page: the fragment build wrapped the way a self-wrapping host does it, with fake
// `claude.use()` capabilities (db, user, sample, downloads).  Proves the cross-device history sync,
// the "check it with Claude" flow and the file export without the real host.
//
//   python3 -m recurser --data-root tests/fixtures/demo build --fragment /tmp/frag.html
//   node tests/artifact_sim.js /tmp/frag.html [chromium executable]
const fs = require('fs');
const path = require('path');
const os = require('os');
const { chromium } = require('playwright');

const fragment = fs.readFileSync(process.argv[2], 'utf8');
const exe = process.argv[3] || process.env.CHROMIUM_PATH;
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'recurser-host-'));
const pagePath = path.join(dir, 'page.html');
fs.writeFileSync(pagePath, '<!doctype html><html><head><meta charset=utf8><meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">'
  + '<style>:root{color-scheme:light}body{margin:0;font:14px system-ui}img{max-width:100%}[hidden]{display:none!important}</style></head><body>'
  + fragment + '</body></html>');
const url = 'file://' + pagePath;

let remote = {};            // the fake per-user document store, shared by every "device"
const saved = [];           // files the page offered through the downloads capability
const problems = [];
const check = (cond, msg) => { if (!cond) problems.push(msg); console.log((cond ? 'ok   ' : 'FAIL ') + msg); };

function fakeHost(mode) {
  // Runs in the page before any of its scripts.
  return `
    (function () {
      var mode = ${JSON.stringify(mode)};
      var docs = JSON.parse(${JSON.stringify(JSON.stringify(remote))});
      function persist() { window.__persist(JSON.stringify(docs)); }
      function docRef(col, id) {
        var key = col + '/' + id;
        return {
          id: id, path: key,
          get: function () { return Promise.resolve({ id: id, exists: key in docs, data: function () { return docs[key]; } }); },
          set: function (data) { docs[key] = JSON.parse(JSON.stringify(data)); persist(); return Promise.resolve(); },
          delete: function () { delete docs[key]; persist(); return Promise.resolve(); },
        };
      }
      var db = {
        collection: function (col) {
          if (col.split('/').length % 2 === 0) throw new TypeError('collection path needs an odd number of segments');
          return {
            path: col,
            doc: function (id) { if (!/^[A-Za-z0-9_.~:@+-]+$/.test(id)) throw new TypeError('bad segment ' + id); return docRef(col, id); },
            get: function () {
              var out = Object.keys(docs).filter(function (k) { return k.indexOf(col + '/') === 0 && k.slice(col.length + 1).indexOf('/') < 0; })
                .map(function (k) { var id = k.slice(col.length + 1); return { id: id, exists: true, data: function () { return docs[k]; } }; });
              return Promise.resolve({ docs: out, size: out.length, empty: !out.length });
            },
          };
        },
        doc: function (p) { var i = p.lastIndexOf('/'); return docRef(p.slice(0, i), p.slice(i + 1)); },
      };
      var user = { id: function () { return Promise.resolve('u_test123'); } };
      function sample(input, opts) {
        window.__samplePrompt = String(input);
        return new Promise(function (resolve) {
          setTimeout(function () {
            var text = 'partly. You named the loader but missed the idempotent batch.';
            if (opts && opts.onText) opts.onText({ text: text, delta: text });
            resolve({ text: text, truncated: false, modelTierApplied: 'quick' });
          }, 30);
        });
      }
      var downloads = { save: function (req) { window.__saved(req.filename, String(req.data).length); return Promise.resolve({ status: 'saved' }); } };
      var caps = mode === 'signed-out' ? {} : { db: db, user: user, sample: sample, downloads: downloads };
      window.claude = { use: function (name) { return new Promise(function (r) { setTimeout(function () { r(caps[name] || null); }, 20); }); } };
    })();`;
}

async function open(browser, mode) {
  const ctx = await browser.newContext({ viewport: { width: 412, height: 880 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|fonts\.g|net::ERR/.test(m.text())) errors.push(m.text()); });
  await page.exposeFunction('__persist', json => { remote = JSON.parse(json); });
  await page.exposeFunction('__saved', (name, size) => { saved.push({ name, size }); });
  await page.addInitScript(fakeHost(mode));
  await page.goto(url);
  await page.waitForSelector('.hero-figure');
  return { ctx, page, errors };
}

(async () => {
  const browser = await chromium.launch(exe ? { executablePath: exe } : {});

  // Device A: learn three cards; they must land in the per-user documents.
  const a = await open(browser, 'signed-in');
  await a.page.waitForFunction(() => Recurser.store.status().claude === 'on');
  check((await a.page.textContent('#sync-chip')).includes('Claude'), 'device A: the page reports history saved to the Claude account');
  await a.page.click('text=Learn a new block');
  for (let i = 0; i < 3; i++) {
    await a.page.waitForSelector('.session-bar');
    while (await a.page.$('#reveal')) await a.page.click('#reveal');
    await a.page.click('.rate.good');
  }
  await a.page.click('text=End');
  await a.page.waitForTimeout(2600);
  const keys = Object.keys(remote).sort();
  check(keys.includes('data/users/u_test123/meta'), 'device A: a meta document was written under the private subtree (' + keys.join(', ') + ')');
  const deckKey = keys.find(k => k.indexOf('data/users/u_test123/deck.') === 0);
  check(!!deckKey && Object.keys(remote[deckKey].cards).length === 3, 'device A: one deck document holds the three reviewed cards');
  check(keys.every(k => k.indexOf('data/users/u_test123/') === 0), 'device A: nothing was written outside the private subtree');
  check(a.errors.length === 0, 'device A: no script errors' + (a.errors.length ? ': ' + a.errors[0] : ''));
  await a.ctx.close();

  // Device B: a fresh browser (empty local storage) must get the history back from the account.
  const b = await open(browser, 'signed-in');
  await b.page.waitForFunction(() => Recurser.store.status().claude === 'on');
  await b.page.waitForFunction(() => Object.keys(Recurser.store.get().cards).length === 3);
  check(true, 'device B: the three reviews arrived on a browser that had never seen them');
  await b.page.goto(url + '#today'); await b.page.waitForSelector('.tile-value');
  check((await b.page.textContent('.tiles')).includes('3 / 19'), 'device B: the dashboard counts them (Seen 3 / 19)');

  // Explain-back: write first, reveal, ask for a check.
  await b.page.goto(url + '#p-tide-loader'); await b.page.waitForSelector('.matrix');
  await b.page.click('text=Random draw'); await b.page.waitForSelector('.session-bar');
  await b.page.click('text=Write it first'); await b.page.fill('#typed-answer', 'it loads tide files <b>somehow</b>');
  while (await b.page.$('#reveal')) await b.page.click('#reveal');
  await b.page.click('text=Check it with Claude');
  await b.page.waitForFunction(() => { const el = document.querySelector('.critique'); return el && /idempotent/.test(el.textContent); });
  const prompt = await b.page.evaluate(() => window.__samplePrompt);
  check(/REFERENCE ANSWER/.test(prompt) && /it loads tide files/.test(prompt), 'explain-back: the prompt carries the question, the reference and the typed answer');
  check((await b.page.$$('.mine b')).length === 0, 'explain-back: typed text is shown as text, never as markup');
  await b.page.click('text=End');

  // File export goes through the downloads capability.
  await b.page.goto(url + '#sync'); await b.page.waitForSelector('#pref-new');
  await b.page.click('text=Save history (.json)');
  await b.page.waitForFunction(() => /Saved recurser-history\.json/.test(document.body.textContent));
  await b.page.click('text=Export for Anki (.txt)');
  await b.page.waitForFunction(() => /Saved recurser-anki\.txt/.test(document.body.textContent));
  check(saved.length === 2 && saved[0].name === 'recurser-history.json' && saved[1].size > 1000, 'export: both files were offered through the host (' + saved.map(s => s.name).join(', ') + ')');

  // Erase history: confirmed in the page, and the account copy is emptied too.
  await b.page.click('text=Erase history');
  await b.page.click('.confirm >> text=Erase history');
  await b.page.waitForTimeout(2600);
  const left = Object.keys(remote).filter(k => k.indexOf('/deck.') > 0);
  check(left.length === 0, 'erase: deck documents are removed from the account (' + left.length + ' left)');
  check(b.errors.length === 0, 'device B: no script errors' + (b.errors.length ? ': ' + b.errors[0] : ''));
  await b.ctx.close();

  // A visitor with no capabilities at all still gets a working page.
  const c = await open(browser, 'signed-out');
  await c.page.waitForTimeout(400);
  check((await c.page.evaluate(() => Recurser.store.status().claude)) === 'off', 'no capabilities: the page falls back to this browser only');
  await c.page.click('text=Learn a new block'); await c.page.waitForSelector('.session-bar');
  check(!(await c.page.$('text=Check it with Claude')), 'no capabilities: the Claude check is not offered');
  check(c.errors.length === 0, 'no capabilities: no script errors' + (c.errors.length ? ': ' + c.errors[0] : ''));
  await c.ctx.close();

  await browser.close();
  console.log(problems.length ? `\n${problems.length} problem(s)` : '\nall hosted-page checks passed');
  process.exit(problems.length ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
