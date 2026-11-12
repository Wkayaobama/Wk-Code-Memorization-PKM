// End-to-end browser check (optional; needs the `playwright` package and a Chromium).
//   node tests/browser_check.js http://127.0.0.1:8765/ out_dir [chromium executable]
const base = process.argv[2] || 'http://127.0.0.1:8765/';
const outDir = process.argv[3] || 'tests/out';
const exe = process.argv[4] || process.env.CHROMIUM_PATH;
const fs = require('fs');
const { chromium } = require('playwright');

(async () => {
  fs.mkdirSync(outDir, { recursive: true });
  const browser = await chromium.launch(exe ? { executablePath: exe } : {});
  const problems = [];
  const check = (cond, msg) => { if (!cond) problems.push(msg); console.log((cond ? 'ok   ' : 'FAIL ') + msg); };

  for (const vp of [{ name: 'desktop', width: 1280, height: 860 }, { name: 'phone', width: 390, height: 844 }]) {
    for (const scheme of ['light', 'dark']) {
      const ctx = await browser.newContext({ viewport: { width: vp.width, height: vp.height }, colorScheme: scheme, deviceScaleFactor: 1 });
      const page = await ctx.newPage();
      const errors = [];
      page.on('pageerror', e => errors.push('pageerror: ' + e.message));
      page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|fonts\.g/.test(m.text())) errors.push('console: ' + m.text()); });
      const tag = `${vp.name}-${scheme}`;
      const shot = async name => page.screenshot({ path: `${outDir}/${tag}-${name}.png`, fullPage: false });
      const noOverflow = async name => {
        const wide = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
        check(!wide, `${tag} ${name}: no horizontal page scroll`);
      };

      await page.goto(base, { waitUntil: 'load' });
      await page.waitForSelector('.view-title');
      check((await page.textContent('.view-title')).includes('pulse'), `${tag} today renders`);
      await noOverflow('today'); await shot('today');

      // run the session the page proposes first (a new block on a fresh history, otherwise reviews or a draw)
      const primary = await page.textContent('.hero ~ .cluster .btn-primary, section .cluster .btn-primary');
      await page.click('section .cluster .btn-primary');
      await page.waitForSelector('.card .question');
      await shot('review-front');
      let guard = 0, sawOutput = false, sawTrace = false, sawCloze = false;
      while (await page.$('.card .question') && guard++ < 60) {
        if (await page.$('.codebox')) sawOutput = true;
        if (await page.$('.steps')) sawTrace = true;
        if (await page.$('.blank')) sawCloze = true;
        while (await page.$('#reveal')) await page.click('#reveal');
        await page.waitForSelector('.ratings');
        if (guard === 1) { await noOverflow('review'); await shot('review-back'); }
        await page.click(guard === 2 ? '.rate.again' : '.rate.good');
      }
      await page.waitForSelector('text=Session done');
      check(true, `${tag} session completes: ${(primary || '').trim()} (${guard} answers; output=${sawOutput} trace=${sawTrace} cloze=${sawCloze})`);
      await shot('summary');
      await page.click('text=Back to today');
      await page.waitForSelector('.hero-figure');

      for (const [hash, sel, name] of [['#projects', '.rows .row', 'projects'], ['#matrix', 'table.grid tbody tr', 'matrix'], ['#inventory', 'table.grid tbody tr', 'inventory'], ['#sync', '#pref-new', 'sync']]) {
        await page.goto(base + hash); await page.waitForSelector(sel);
        await noOverflow(name); await shot(name);
      }
      await page.goto(base + '#projects');
      await page.fill('#project-search', 'gauge pipeline');
      const first = await page.textContent('.rows .row .row-title');
      check(/Tide Loader/.test(first || ''), `${tag} fuzzy search finds the nickname (${first})`);
      await page.click('.rows .row');
      await page.waitForSelector('.matrix');
      await page.click('button.cell');
      await page.waitForSelector('.card .answer');
      await noOverflow('project'); await shot('project');

      // predict the output: commit a prediction first, exact match is recognised
      await page.goto(base + '#p-tide-loader'); await page.waitForSelector('.matrix');
      await page.click('text=Predict the output'); await page.waitForSelector('#typed-answer');
      check(await page.isDisabled('#reveal'), `${tag} predict: reveal is locked until a prediction is typed`);
      const snippet = await page.textContent('.codebox pre');
      await page.fill('#typed-answer', /ft/.test(snippet) ? '91.4' : 'something else');
      await page.click('#reveal'); await page.waitForSelector('.ratings');
      if (/ft/.test(snippet)) check(!!(await page.$('text=exact match')), `${tag} predict: exact match badge`);
      await shot('predict'); await page.click('text=End');

      // a sitting: attempt, fail with a gap note, then pass
      await page.goto(base + '#p-tide-loader'); await page.waitForSelector('.matrix');
      await page.click('button:has-text("Sit")'); await page.waitForSelector('#typed-answer');
      check(!(await page.$('#hint')), `${tag} sitting: no hints offered`);
      await page.fill('#typed-answer', 'my attempt'); await page.click('#reveal');
      await page.click('.rate.again'); await page.waitForSelector('#gap-note');
      await page.fill('#gap-note', 'forgot the idempotent part'); await page.click('text=Save and continue');
      await page.waitForSelector('#typed-answer'); await page.fill('#typed-answer', 'second attempt'); await page.click('#reveal');
      await page.click('.rate.good'); await page.waitForSelector('.card .question');
      const gaps = await page.evaluate(() => Object.keys(Recurser.store.get().gaps).length);
      check(gaps === 1, `${tag} sitting: gap note stored (${gaps})`);
      await page.click('text=End');

      // re-entry shows the anchor as a cue; a trace card offers ticks after the reveal
      await page.goto(base + '#p-tide-loader'); await page.waitForSelector('.matrix');
      await page.click('text=Re-entry'); await page.waitForSelector('.session-bar');
      check(!!(await page.$('.card .label:has-text("Cue")')), `${tag} re-entry: anchor shown as a cue`);
      // the first card links to two cards of the other deck; a link opens in place
      while (await page.$('#reveal')) await page.click('#reveal');
      const also = await page.$$('details.also');
      check(also.length === 2, `${tag} cross-links: two linked cards are offered (${also.length})`);
      if (also.length) {
        await page.click('details.also summary');
        check(/one Markdown file per note/.test(await page.textContent('details.also[open]')), `${tag} cross-links: the linked answer opens in place`);
        await noOverflow('cross-link'); await shot('cross-link');
      }
      let hops = 0;
      while (!(await page.$('.steps')) && hops++ < 12) {
        while (await page.$('#reveal')) await page.click('#reveal');
        await page.click('.rate.good');
        if (!(await page.$('.session-bar'))) break;
      }
      if (await page.$('.steps')) {
        while (await page.$('#reveal')) await page.click('#reveal');
        await page.waitForSelector('.tick input');
        await page.click('.tick input');
        await page.waitForSelector('.rate.suggested');
        check(!!(await page.$('.rate.suggested')), `${tag} trace: ticking parts suggests a rating`);
        await noOverflow('trace-ticks'); await shot('trace');
      }
      if (await page.$('text=End')) await page.click('text=End');

      await page.goto(base + '#matrix'); await page.waitForSelector('table.grid tbody tr.clickable');
      await page.click('table.grid tbody tr.clickable'); await page.waitForSelector('td.detail .card');
      await noOverflow('matrix-open');

      // history survives a reload (browser storage) and reached the server
      await page.goto(base + '#today'); await page.reload(); await page.waitForSelector('.hero-figure');
      const seen = await page.evaluate(() => Object.keys(JSON.parse(localStorage.getItem('recurser.progress.v1')).cards).length);
      check(seen > 0, `${tag} history kept across reload (${seen} cards)`);
      check(errors.length === 0, `${tag} no script errors` + (errors.length ? ': ' + errors.slice(0, 3).join(' | ') : ''));
      await ctx.close();
    }
  }
  await browser.close();
  console.log(problems.length ? `\n${problems.length} problem(s)` : '\nall browser checks passed');
  process.exit(problems.length ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
