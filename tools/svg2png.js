// Render SVG files to PNG with the local Chrome.
// Usage: node svg2png.js jobs.json   (jobs = [{"src": "...svg", "dst": "...png"}])
// Prints one JSON line per job: {"dst": ..., "width": <css px>, "height": <css px>}
const fs = require("fs");
const path = require("path");
const puppeteer = require("puppeteer-core");

const CHROME = process.env.CHROME_PATH;  // set by build/build.py
const SCALE = 3;

(async () => {
  const jobs = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
  const browser = await puppeteer.launch({ executablePath: CHROME, args: ["--no-sandbox"] });
  const page = await browser.newPage();
  for (const job of jobs) {
    const svg = fs.readFileSync(job.src, "utf8");
    await page.setViewport({ width: 2400, height: 2400, deviceScaleFactor: SCALE });
    await page.setContent(
      `<html><body style="margin:0;background:#fff">${svg}</body></html>`,
      { waitUntil: "load" }
    );
    // An SVG with only a viewBox fills the viewport. Give it its natural size.
    await page.evaluate(() => {
      const s = document.querySelector("svg");
      const vb = s.viewBox && s.viewBox.baseVal;
      if (vb && vb.width && (!s.getAttribute("width") || s.getAttribute("width").endsWith("%"))) {
        s.setAttribute("width", vb.width);
        s.setAttribute("height", vb.height);
      }
    });
    const el = await page.$("svg");
    const box = await el.boundingBox();
    await el.screenshot({ path: job.dst, omitBackground: false });
    console.log(JSON.stringify({ dst: job.dst, width: box.width, height: box.height }));
  }
  await browser.close();
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
