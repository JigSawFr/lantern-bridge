// The screenshots of the Bridge's page (site/assets/shots/), taken from tools/demo_serve.py's made-up library.
// Usage: node tools/site_shots.mjs   (needs `playwright`; PYTHON= the venv's Python, CHROME= a browser already there)
import { spawn } from "node:child_process";
import { mkdirSync } from "node:fs";
import { createRequire } from "node:module";

const { chromium } = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || "playwright");
const PORT = process.env.PORT || "8771";
const BASE = `http://127.0.0.1:${PORT}/admin`;
const PASSWORD = "correct horse battery staple";
const PAGES = [["overview", ""], ["library", "library"], ["sources", "sources"], ["accounts", "accounts"], ["console", null], ["operators", "operators"]];

const server = spawn(process.env.PYTHON || "python", ["tools/demo_serve.py", PORT], { stdio: ["ignore", "pipe", "inherit"] });
await new Promise((resolve, reject) => {
  server.stdout.on("data", (chunk) => String(chunk).includes("ready") && setTimeout(resolve, 500));
  server.on("exit", (code) => reject(new Error(`the demo server stopped (${code})`)));
});
const browser = await chromium.launch(process.env.CHROME ? { executablePath: process.env.CHROME, args: ["--no-sandbox"] } : {});
mkdirSync("site/assets/shots", { recursive: true });
try {
  const context = await browser.newContext({ viewport: { width: 1200, height: 760 }, deviceScaleFactor: 1 });
  const page = await context.newPage();
  await page.goto(`${BASE}/login`);
  await page.fill("input[name=name]", "maya");
  await page.fill("input[name=password]", PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(`${BASE}/`);
  for (const [name, path] of PAGES) {
    if (path === null) {
      // a console's own page: the one of the child's console, reached from the list
      await page.goto(`${BASE}/devices`);
      await page.getByRole("link", { name: "Kids room" }).click();
      await page.waitForURL(/\/admin\/devices\/[^/]+$/);
    } else {
      await page.goto(`${BASE}/${path}`);
    }
    // The library lives in a temporary folder: the page shows a plausible path instead.
    await page.evaluate(() => document.querySelectorAll("code").forEach((c) => { c.textContent = c.textContent.replace(/\/tmp\/[^/]+\/games/, "/srv/games"); }));
    await page.waitForTimeout(600);
    await page.screenshot({ path: `site/assets/shots/${name}.jpg`, type: "jpeg", quality: 82 });
    console.log("shot", name);
  }
} finally {
  await browser.close();
  server.kill();
}
