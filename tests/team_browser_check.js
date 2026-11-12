// Team mode in a real browser: personal links, per-person decks, the Ask view, the server-backed check.
//   python3 tests/team_demo_server.py 8770 &
//   node tests/team_browser_check.js http://127.0.0.1:8770 out_dir [chromium executable]
const base = (process.argv[2] || 'http://127.0.0.1:8770').replace(/\/$/, '');
const outDir = process.argv[3] || 'tests/out';
const exe = process.argv[4] || process.env.CHROMIUM_PATH;
const fs = require('fs');
const { chromium } = require('playwright');
const TOKENS = { owner: 'owner-token-0123456789abcdef', sam: 'sam-token-0123456789abcdefgh' };

(async () => {
  fs.mkdirSync(outDir, { recursive: true });
  const browser = await chromium.launch(exe ? { executablePath: exe } : {});
  const problems = [];
  const check = (cond, msg) => { if (!cond) problems.push(msg); console.log((cond ? 'ok   ' : 'FAIL ') + msg); };

  async function open(name, viewport) {
    const ctx = await browser.newContext({ viewport: viewport || { width: 1280, height: 900 } });
    const page = await ctx.newPage();
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|fonts\.g/.test(m.text())) errors.push(m.text()); });
    await page.goto(base + '/?token=' + TOKENS[name]);
    await page.waitForSelector('.hero-figure');
    await page.waitForFunction(() => Recurser.store.status().server === 'on');
    return { ctx, page, errors };
  }

  // nobody gets in without a personal link
  const anon = await browser.newContext();
  const anonPage = await anon.newPage();
  const resp = await anonPage.goto(base + '/');
  check(resp.status() === 401, 'without a personal link the page answers 401');
  await anon.close();

  // sam: one project, the Ask view, a cited answer
  const sam = await open('sam', { width: 390, height: 844 });
  check((await sam.page.textContent('#sync-chip')).startsWith('sam'), 'sam: the page shows who is signed in');
  check(!/token=/.test(sam.page.url()), 'sam: the token leaves the address bar once the cookie is set');
  check((await sam.page.evaluate(() => RECURSER_BUNDLE.projects.map(p => p.slug).join(','))) === 'tide-loader', 'sam: only the project listed for sam is in the page');
  check(!!(await sam.page.locator('a[href="#ask"]').count()), 'sam: the Ask button is offered when the server has the assistant');
  await sam.page.click('a[href="#ask"]');
  await sam.page.waitForSelector('#ask-question');
  await sam.page.fill('#ask-question', 'Why does tide-loader merge on a key instead of appending?');
  await sam.page.click('#ask-send');
  await sam.page.waitForSelector('.ask-answer');
  check(/Grounded sentence\./.test(await sam.page.textContent('.ask-answer')), 'ask: the answer is shown');
  check((await sam.page.locator('sup.cite').count()) === 1, 'ask: the cited sentence carries its marker');
  check((await sam.page.locator('details.also').count()) === 1, 'ask: the passage the answer rests on is listed');
  await sam.page.click('details.also summary');
  check(/Question:/.test(await sam.page.textContent('details.also[open]')), 'ask: the passage opens in place');
  const wide = await sam.page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  check(wide <= 1, 'ask: no sideways scroll on a phone (' + wide + 'px)');
  await sam.page.screenshot({ path: outDir + '/team-ask-phone.png' });

  // the server-backed check of a typed answer
  await sam.page.goto(base + '/#p-tide-loader'); await sam.page.waitForSelector('.matrix');
  await sam.page.click('text=Random draw'); await sam.page.waitForSelector('.session-bar');
  await sam.page.click('text=Write it first'); await sam.page.fill('#typed-answer', 'it loads tide files');
  while (await sam.page.locator('#reveal').count()) await sam.page.click('#reveal');
  await sam.page.click('text=Check it with Claude');
  await sam.page.waitForFunction(() => { const el = document.querySelector('.critique'); return el && /batch key/.test(el.textContent); });
  check(true, 'check: the server compared the typed answer with the reference');
  await sam.page.click('.rate.good');
  await sam.page.click('text=End');
  await sam.page.waitForTimeout(2200);
  const samCards = await sam.page.evaluate(() => fetch('api/progress').then(r => r.json()).then(p => Object.keys(p.cards).length));
  // the same browser profile then opens the owner's link: the two histories must not mix
  const samLog = await sam.page.evaluate(() => fetch('api/progress').then(r => r.json()).then(p => p.log.length));
  await sam.page.goto(base + '/?token=' + TOKENS.owner);
  await sam.page.waitForSelector('.hero-figure');
  await sam.page.waitForFunction(() => Recurser.store.status().server === 'on' && Recurser.store.status().user === 'owner');
  await sam.page.waitForTimeout(1500);
  const mixed = await sam.page.evaluate(() => fetch('api/progress').then(r => r.json()).then(p => p.log.length + Object.keys(p.cards).length));
  check(samLog >= 1 && mixed === 0, 'same browser, other link: the owner history received nothing from sam (' + samLog + ' entries stayed with sam)');
  const keys = await sam.page.evaluate(() => Object.keys(localStorage).filter(k => k.indexOf('recurser.progress') === 0).sort().join(','));
  check(keys === 'recurser.progress.v1.owner,recurser.progress.v1.sam', 'same browser: one stored history per person (' + keys + ')');
  check(sam.errors.length === 0, 'sam: no script errors' + (sam.errors.length ? ': ' + sam.errors[0] : ''));
  await sam.ctx.close();

  // owner: both projects, a history of their own
  const owner = await open('owner');
  check((await owner.page.evaluate(() => RECURSER_BUNDLE.projects.length)) === 2, 'owner: both projects are in the page');
  const ownerCards = await owner.page.evaluate(() => fetch('api/progress').then(r => r.json()).then(p => Object.keys(p.cards).length));
  check(ownerCards === 0, 'owner: the reviews sam made are not in the owner history (' + ownerCards + ' cards, sam has ' + samCards + ')');
  await owner.page.goto(base + '/#ask'); await owner.page.waitForSelector('#ask-question');
  await owner.page.screenshot({ path: outDir + '/team-ask-desktop.png' });
  check(owner.errors.length === 0, 'owner: no script errors' + (owner.errors.length ? ': ' + owner.errors[0] : ''));
  await owner.ctx.close();

  await browser.close();
  console.log(problems.length ? '\n' + problems.length + ' problem(s)' : '\nall team checks passed');
  process.exit(problems.length ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
