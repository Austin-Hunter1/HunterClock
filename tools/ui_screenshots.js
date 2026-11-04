// Re-render the UI screenshots in docs/img/ui.
//
//   make sim          # in one terminal
//   node tools/ui_screenshots.js
//
// Needs playwright with a Chromium build available. Every page is rendered at
// desktop (1280px) and phone (390px) width, and the script fails loudly if a
// page logs a JavaScript error or scrolls sideways -- which is the actual point
// of running it, the pictures are a by-product.

const { chromium } = require('playwright');
const PORT = process.env.PORT || '8080';
const OUT = '/home/user/HunterClock/docs/img/ui';
(async () => {
  let failures = 0;
  const b = await chromium.launch();
  const pages = [['dashboard','/'],['control','/control'],['assist','/assist'],['simulate','/simulate'],['logs','/logs'],['config','/config'],['system','/system']];
  for (const [vp, w, h, scale] of [['desktop',1280,1000,1],['phone',390,844,1]]) {
    const ctx = await b.newContext({ viewport:{width:w,height:h}, deviceScaleFactor:scale });
    for (const [name, path] of pages) {
      const p = await ctx.newPage();
      const errs = [];
      p.on('pageerror', e => errs.push('PAGEERROR: ' + e.message));
      p.on('console', m => { if (m.type()==='error') errs.push('console: ' + m.text()); });
      await p.goto(`http://127.0.0.1:${PORT}${path}`, { waitUntil: 'networkidle' });
      await p.waitForTimeout(2500);
      const info = await p.evaluate(() => ({
        overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
        banner: (document.getElementById('banner')||{}).textContent?.slice(0,60),
        rows: document.querySelectorAll('#people-rows tr').length,
        hands: document.querySelectorAll('#dial-hands path').length,
        panels: document.querySelectorAll('.hand-panel').length,
      }));
      const bad = info.overflow || errs.length;
      if (bad) failures += 1;
      console.log(`${vp.padEnd(8)} ${name.padEnd(10)} overflow=${info.overflow} hands=${info.hands} rows=${info.rows} panels=${info.panels} errors=${errs.length ? errs.join(' | ') : 'none'}`);
      await p.screenshot({ path: `${OUT}/${name}-${vp}.png`, fullPage: vp==='phone' });
      await p.close();
    }
    await ctx.close();
  }
  await b.close();
  if (failures) {
    console.error(`\n${failures} page render(s) had an error or overflowed. Screenshots were still written.`);
    process.exit(1);
  }
  console.log('\nAll pages rendered cleanly at both widths.');
})();
