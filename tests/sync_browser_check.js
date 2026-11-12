// History sync in a real browser: two tabs, erase, bring-back, restore.  Needs a server on the demo decks:
//   python3 -m recurser --data-root tests/fixtures/demo serve --port 8765 --progress /tmp/recurser-sync-test.json
//   node tests/sync_browser_check.js http://127.0.0.1:8765/ [chromium executable]
// Start it on an empty history file.
const base = process.argv[2] || 'http://127.0.0.1:8765/';
const exe = process.argv[3] || process.env.CHROMIUM_PATH;
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch(exe ? { executablePath: exe } : {});
  const problems = [];
  const check = (cond, msg) => { if (!cond) problems.push(msg); console.log((cond ? 'ok   ' : 'FAIL ') + msg); };
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  const open = async () => {
    const page = await ctx.newPage();
    page.on('pageerror', e => errors.push(e.message));
    await page.goto(base);
    await page.waitForSelector('.hero-figure');
    await page.waitForFunction(() => Recurser.store.status().server === 'on');
    return page;
  };
  const reloadHome = async page => {
    await page.goto(base + '#today');
    await page.reload();
    await page.waitForSelector('.hero-figure');
    await page.waitForFunction(() => Recurser.store.status().server === 'on');
  };
  const cardsIn = page => page.evaluate(() => Object.keys(Recurser.store.get().cards).length);
  const stored = page => page.evaluate(() => Object.keys(JSON.parse(localStorage.getItem('recurser.progress.v1') || '{"cards":{}}').cards).length);
  const onServer = page => page.evaluate(() => fetch('api/progress').then(r => r.json()));
  const learn = async (page, n) => {
    await page.click('text=Learn a new block');
    for (let i = 0; i < n; i++) {
      await page.waitForSelector('.session-bar');
      while (await page.locator('#reveal').count()) await page.click('#reveal');
      await page.click('.rate.good');
    }
    await page.click('text=End');
    await page.waitForSelector('.hero-figure');
  };

  // two tabs: B was opened before A reviewed anything, then reviews one card of its own
  const a = await open();
  const b = await open();
  await learn(a, 2);
  check((await cardsIn(a)) === 2, 'tab A holds its two reviews');
  await b.waitForFunction(() => Object.keys(Recurser.store.get().cards).length === 2);
  check(true, 'tab B received them without a reload');
  await learn(b, 1);
  check((await stored(b)) === 3, 'after tab B reviews, the stored history holds all three cards (' + (await stored(b)) + ')');
  await a.waitForFunction(() => Object.keys(Recurser.store.get().cards).length === 3);
  await a.waitForTimeout(2200);
  check(Object.keys((await onServer(a)).cards).length === 3, 'the server holds all three');

  // leave a card out, bring it back: the change survives a reload
  const id = await a.evaluate(() => Object.keys(Recurser.store.get().cards)[0]);
  await a.evaluate(i => Recurser.store.setFlag(i, 'suspended'), id);
  await a.waitForTimeout(1600);
  await a.evaluate(i => Recurser.store.setFlag(i, null), id);
  await a.waitForTimeout(1600);
  await reloadHome(a);
  check((await a.evaluate(i => Recurser.store.flag(i), id)) === null, 'a card brought back into reviews stays back after a reload');
  check((await a.evaluate(i => Recurser.model.isSuspended(Recurser.store.get(), i), id)) === false, 'and the scheduler no longer skips it');

  // erase: gone here, in the other tab, on the server, and still gone after a reload
  const saved = await a.evaluate(() => Recurser.store.exportData());
  await a.goto(base + '#sync'); await a.waitForSelector('#pref-new');
  await a.click('text=Erase history');
  await a.click('.confirm >> text=Erase history');
  await a.waitForTimeout(2200);
  check((await cardsIn(a)) === 0, 'erase: this tab is empty');
  await b.waitForFunction(() => Object.keys(Recurser.store.get().cards).length === 0);
  check(true, 'erase: the other tab is empty too');
  const after = await onServer(a);
  check(Object.keys(after.cards).length === 0 && after.log.length === 0 && after.resetAt > 0, 'erase: the server history is empty and carries the erase time');
  await reloadHome(a);
  check((await cardsIn(a)) === 0, 'erase: still empty after a reload');

  // merging a saved history back in outranks the erase
  await a.evaluate(data => Recurser.store.importData(data), saved);
  check((await cardsIn(a)) === 3, 'restore: the saved history is back (' + (await cardsIn(a)) + ' cards)');
  await a.waitForTimeout(2200);
  await reloadHome(a);
  check((await cardsIn(a)) === 3, 'restore: and survives a reload');
  check(Object.keys((await onServer(a)).cards).length === 3, 'restore: the server has it again');

  check(errors.length === 0, 'no script errors' + (errors.length ? ': ' + errors[0] : ''));
  await browser.close();
  console.log(problems.length ? '\n' + problems.length + ' problem(s)' : '\nall sync checks passed');
  process.exit(problems.length ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
