import puppeteer from 'puppeteer';

(async () => {
  const browser = await puppeteer.launch();
  const page = await browser.newPage();
  
  page.on('console', msg => {
    if (msg.type() === 'error')
      console.log(`[CONSOLE ERROR] ${msg.text()}`);
  });

  page.on('pageerror', error => {
    console.log(`[UNCAUGHT] ${error.message}`);
  });

  try {
    await page.goto('http://localhost:5173', { waitUntil: 'networkidle0', timeout: 10000 });
    const content = await page.evaluate(() => {
        let text = document.body.innerText;
        return text.includes('Security') && text.includes('Traffic Flow') ? 'Graph found!' : 'No graph found.';
    });
    console.log(content);
  } catch (err) {
    console.log(`Nav error: ${err.message}`);
  }
  
  await browser.close();
})();
