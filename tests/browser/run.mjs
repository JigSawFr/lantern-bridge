// The admin pages in a real browser, at a desktop and a phone width: what the HTML tests cannot see.
// Usage: node tests/browser/run.mjs   (needs `playwright`; starts tests/browser/serve.py itself)
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createRequire } from "node:module";

const { chromium } = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || "playwright");
const PORT = process.env.PORT || "8765";
const BASE = `http://127.0.0.1:${PORT}/admin`;
const PASSWORD = "correct horse battery staple";

const server = spawn(process.env.PYTHON || "python", ["tests/browser/serve.py", PORT], { stdio: ["ignore", "pipe", "inherit"] });
await new Promise((resolve, reject) => {
  server.stdout.on("data", (chunk) => String(chunk).includes("ready") && setTimeout(resolve, 500));
  server.on("exit", (code) => reject(new Error(`the test server stopped (${code})`)));
});

// CHROME: a browser already on the machine (the CI runner's Chrome); without it Playwright's own Chromium
const browser = await chromium.launch(
  process.env.CHROME ? { executablePath: process.env.CHROME, args: ["--no-sandbox"] } : {},
);
let failed = 0;
const test = async (name, size, body) => {
  const context = await browser.newContext({ viewport: size });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error)));
  try {
    await page.goto(`${BASE}/login`);
    await page.fill("input[name=name]", "boss");
    await page.fill("input[name=password]", PASSWORD);
    await page.getByRole("button", { name: "Sign in" }).click();
    await page.waitForURL(`${BASE}/`);
    await body(page);
    assert.deepEqual(errors, [], "no script error");
    console.log(`ok   ${name}`);
  } catch (error) {
    failed++;
    console.log(`FAIL ${name}\n     ${error.message}`);
  } finally {
    await context.close();
  }
};
const noSideScroll = (page) =>
  page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth);

for (const [label, size] of [["desktop", { width: 1280, height: 800 }], ["phone", { width: 390, height: 800 }]]) {
  await test(`${label}: every page fits the width`, size, async (page) => {
    for (const path of ["", "library", "activity", "developer", "logs", "pairings", "config"]) {
      await page.goto(`${BASE}/${path}`);
      assert.ok(await noSideScroll(page), `${path || "overview"} scrolls sideways`);
    }
  });

  await test(`${label}: the library filters as you type`, size, async (page) => {
    await page.goto(`${BASE}/library`);
    await page.fill("#q", "harbor");
    await page.waitForURL(/q=harbor/);
    const text = await page.locator("main").innerText();
    assert.ok(text.includes("Night Harbor") && !text.includes("Aurora Quest"));
  });

  await test(`${label}: a title page opens from the shelf`, size, async (page) => {
    await page.goto(`${BASE}/library`);
    await page.getByText("Kite Race").first().click();
    await page.waitForURL(/library\/0100CCCC00000000/);
    assert.ok((await page.locator("main").innerText()).includes("Kite Race"));
  });

  await test(`${label}: the activity block follows the server by a stream`, size, async (page) => {
    await page.goto(`${BASE}/activity`);
    const got = page.waitForResponse((r) => r.url().includes("/admin/live") && r.headers()["content-type"].startsWith("text/event-stream"));
    await page.reload();
    await got;
    // the first message of the stream is the block's content: the page stores it when it applies it
    await page.waitForFunction(() => document.querySelector("[data-live]").dataset.html !== undefined, null, { timeout: 4000 });
  });

  await test(`${label}: without EventSource the block is asked again every two seconds`, size, async (page) => {
    await page.addInitScript(() => { Object.defineProperty(window, "EventSource", { value: undefined }); });
    await page.goto(`${BASE}/activity`);
    const live = page.locator("[data-live]");
    await live.evaluate((el) => { el.innerHTML = "<p id=stale>stale</p>"; });
    await page.waitForSelector("#stale", { state: "detached", timeout: 6000 });
  });
}

await test("phone: a table row becomes a labelled card", { width: 390, height: 800 }, async (page) => {
  await page.goto(`${BASE}/sources`);
  assert.ok((await page.locator("td[data-label]").count()) > 0, "cells carry their column's name");
});

await test("desktop: the settings pages sit under Manage, open with a click, close with Escape", { width: 1280, height: 800 }, async (page) => {
  const button = page.locator(".more-btn");
  const list = page.locator("#more-list");
  assert.ok(await button.isVisible() && !(await list.isVisible()), "closed to start with");
  await button.click();
  assert.equal(await button.getAttribute("aria-expanded"), "true");
  assert.ok(await list.getByRole("link", { name: "Settings" }).isVisible());
  await page.keyboard.press("Escape");
  assert.ok(!(await list.isVisible()), "Escape closes it");
  assert.ok(await button.evaluate((el) => el === document.activeElement), "and gives the focus back");
  await button.click();
  await page.mouse.click(5, 400);
  assert.ok(!(await list.isVisible()), "a click elsewhere closes it");
});

await test("phone: the settings pages are in the scrolling row, no Manage button", { width: 390, height: 800 }, async (page) => {
  assert.ok(!(await page.locator(".more-btn").isVisible()));
  assert.ok(await page.locator("#more-list").getByRole("link", { name: "Logs" }).isVisible());
});

await test("desktop: the search shortcut", { width: 1280, height: 800 }, async (page) => {
  await page.goto(`${BASE}/library`);
  await page.keyboard.press("/");
  assert.equal(await page.evaluate(() => document.activeElement && document.activeElement.id), "q");
});

await test("light theme: the text of every page has an AA contrast", { width: 1200, height: 900 }, async (page) => {
  await page.context().addCookies([{ name: "lb_theme", value: "light", url: `${BASE}/` }]);
  for (const path of ["", "library", "library?view=list", "library/0100AAAA00000000", "activity", "events", "devices", "pairings", "config", "sources", "logs"]) {
    await page.goto(`${BASE}/${path}`);
    const bad = await page.evaluate(() => {
      const lum = (c) => { const a = c.map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }); return 0.2126 * a[0] + 0.7152 * a[1] + 0.0722 * a[2]; };
      const parse = (s) => { const m = s.match(/rgba?\(([^)]+)\)/); if (!m) return null; const v = m[1].split(/[ ,/]+/).filter(Boolean).map(Number); return { rgb: v.slice(0, 3), a: v.length > 3 ? v[3] : 1 }; };
      const background = (el) => {
        let base = [255, 255, 255];
        const chain = []; for (let e = el; e; e = e.parentElement) chain.push(e);
        for (const e of chain.reverse()) { const c = parse(getComputedStyle(e).backgroundColor); if (c && c.a > 0) base = base.map((v, i) => Math.round(c.rgb[i] * c.a + v * (1 - c.a))); }
        return base;
      };
      const out = [];
      for (const el of document.querySelectorAll("body *")) {
        if (!([...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim()))) continue;
        if (el.closest(".cover")) continue; // a title's initials sit on a gradient
        const cs = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        if (cs.visibility === "hidden" || cs.display === "none" || !r.width || !r.height) continue;
        const fg = parse(cs.color);
        if (!fg || fg.a === 0) continue; // text filled by a gradient
        const bg = background(el);
        const mix = fg.rgb.map((v, i) => Math.round(v * fg.a + bg[i] * (1 - fg.a)));
        const hi = Math.max(lum(mix), lum(bg)), lo = Math.min(lum(mix), lum(bg));
        const big = parseFloat(cs.fontSize) >= 24 || (parseFloat(cs.fontSize) >= 18.66 && Number(cs.fontWeight) >= 700);
        const ratio = (hi + 0.05) / (lo + 0.05);
        if (ratio < (big ? 3 : 4.5)) out.push(`${el.tagName.toLowerCase()} "${el.textContent.trim().slice(0, 24)}" ${ratio.toFixed(2)}`);
      }
      return out;
    });
    assert.deepEqual(bad, [], `${path || "overview"}: too little contrast`);
  }
});

await browser.close();
server.kill();
process.exit(failed ? 1 : 0);
