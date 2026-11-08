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
    for (const path of ["", "library", "activity", "developer", "logs", "pairings", "config", "diagnostic"]) {
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
  for (const path of ["", "library", "library?view=list", "library/0100AAAA00000000", "activity", "events", "devices", "pairings", "config", "sources", "logs", "diagnostic"]) {
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

await test("settings: one bar saves every edited card, and a box finds a setting", { width: 1280, height: 800 }, async (page) => {
  await page.goto(`${BASE}/config`);
  assert.equal(await page.locator("[data-saveall]").isVisible(), false, "no bar before an edit");
  await page.fill("#settings-search", "webhook");
  assert.equal(await page.locator("input[name=webhook]").isVisible(), true);
  assert.equal(await page.locator("input[name=password_env]").isVisible(), false, "the other settings step aside");
  await page.fill("#settings-search", "zzzz");
  assert.equal(await page.locator("#settings-none").isVisible(), true);
  await page.fill("#settings-search", "");
  assert.equal(await page.locator("input[name=password_env]").isVisible(), true);
  await page.fill("input[name=webhook]", "https://example.org/hook");
  assert.equal(await page.locator("[data-saveall]").isVisible(), true, "the bar shows after an edit");
  await page.locator("[data-saveall] button").click();
  await page.waitForURL(/config\?m=/);
  assert.equal(await page.inputValue("input[name=webhook]"), "https://example.org/hook");
  assert.equal(await page.locator("[data-saveall]").isVisible(), false);
  const before = page.url();
  await page.fill("input[name=webhook]", "not an address");
  await page.locator("[data-saveall] button").click();
  await page.locator("form[data-dirty] .error").first().waitFor();
  assert.equal(page.url(), before, "a refused value stays on the page");
});

for (const [scheme, expected] of [["light", "light"], ["dark", "dark"]]) {
  await test(`automatic theme follows a ${scheme} browser`, { width: 1200, height: 800 }, async (page) => {
    await page.emulateMedia({ colorScheme: scheme });
    await page.goto(`${BASE}/`);
    assert.equal(await page.evaluate(() => document.documentElement.dataset.theme), expected);
    const other = scheme === "light" ? "dark" : "light";
    await page.emulateMedia({ colorScheme: other });
    await page.waitForFunction((t) => document.documentElement.dataset.theme === t, other); // and when it changes
    await page.getByRole("button", { name: scheme === "light" ? "Dark theme" : "Light theme" }).click();
    await page.waitForLoadState();
    await page.emulateMedia({ colorScheme: scheme });
    assert.equal(await page.evaluate(() => document.documentElement.dataset.theme), other, "a choice wins over the browser");
  });
}

for (const [label, size] of [["desktop", { width: 1280, height: 800 }], ["phone", { width: 390, height: 800 }]]) {
  await test(`${label}: the language menu lists fifteen languages with their flags and switches the page`, size, async (page) => {
    await page.goto(`${BASE}/config`);
    await page.locator(".langmenu summary").click();
    assert.equal(await page.locator(".langs button").count(), 15);
    const box = await page.locator(".langs").boundingBox();
    assert.ok(box.x >= 0 && box.x + box.width <= size.width, "the menu stays inside the screen");
    assert.ok(await noSideScroll(page), "the open menu does not scroll the page sideways");
    await page.keyboard.press("Escape");
    assert.equal(await page.locator(".langs").isVisible(), false, "Escape closes the menu");
    await page.locator(".langmenu summary").click();
    await page.getByRole("button", { name: "Deutsch" }).click();
    await page.waitForURL(/config/);
    assert.equal(await page.evaluate(() => document.documentElement.lang), "de");
    assert.ok(await page.locator("h1").innerText().then((t) => t !== "Settings"), "the page is translated");
    assert.ok(await noSideScroll(page));
  });
}

await test("the flags are drawn by the bundled font", { width: 1280, height: 800 }, async (page) => {
  await page.goto(`${BASE}/`);
  await page.locator(".langmenu summary").click();
  const family = await page.locator(".langs .flag").first().evaluate((el) => getComputedStyle(el).fontFamily);
  assert.ok(family.startsWith('"Twemoji Flags"'), family);
  const loaded = await page.evaluate(async () => { await document.fonts.ready; return document.fonts.check('16px "Twemoji Flags"', "\u{1F1EB}\u{1F1F7}"); });
  assert.ok(loaded, "the font is loaded");
});

await test("the live block shows a server event at once, not at the next tick", { width: 1200, height: 800 }, async (page) => {
  await page.goto(`${BASE}/activity`);
  await page.waitForFunction(() => document.querySelector("[data-live]").dataset.html !== undefined, null, { timeout: 4000 });
  await page.waitForTimeout(1500); // the stream is idle now: it waits for a change, or five seconds
  const before = await page.locator("[data-live]").first().evaluate((el) => el.dataset.html);
  const began = Date.now();
  await page.request.get(`${BASE.replace("/admin", "")}/api/lantern/v1/apps`); // a request the library counts
  await page.waitForFunction((old) => document.querySelector("[data-live]").dataset.html !== old, before, { timeout: 4000 });
  assert.ok(Date.now() - began < 1800, "the block changed well before the idle wait of five seconds");
});

await test("a changed figure of a live block lights up once, and the header says the pages are live", { width: 1200, height: 800 }, async (page) => {
  await page.goto(`${BASE}/activity`);
  await page.waitForSelector("[data-livestate]:not([hidden]):not(.down)", { timeout: 4000 });
  assert.equal((await page.locator("[data-livestate]").innerText()).trim(), "live");
  await page.waitForFunction(() => document.querySelector("[data-live]").dataset.html !== undefined, null, { timeout: 4000 });
  await page.waitForTimeout(1500);
  await page.request.get(`${BASE.replace("/admin", "")}/api/lantern/v1/apps`); // the counters of the tiles change
  await page.waitForSelector("[data-live] .flash, [data-live] .pop", { timeout: 4000 });
  await page.context().setOffline(true);
  await page.waitForSelector("[data-livestate].down", { timeout: 4000 });
  assert.equal((await page.locator("[data-livestate]").getAttribute("aria-label")), "reconnecting…");
  await page.context().setOffline(false);
  await page.waitForSelector("[data-livestate]:not(.down)", { timeout: 8000 });
});

await test("the rates are drawn under their tiles, and the tab says when something needs a look", { width: 1200, height: 800 }, async (page) => {
  await page.goto(`${BASE}/activity`);
  await page.waitForFunction(() => document.querySelector("svg[data-spark] .line")?.getAttribute("d"), null, { timeout: 4000 });
  await page.goto(`${BASE}/`);
  await page.waitForSelector("[data-badge]", { state: "attached" });
  await page.waitForTimeout(800);
  const badges = await page.evaluate(() => fetch("/admin/api/badges").then((r) => r.json()));
  const look = Object.entries(badges).filter(([k, b]) => k !== "manage" && b.level !== "info").reduce((n, [, b]) => n + b.n, 0);
  const title = await page.title();
  assert.equal(/^\(\d+\) /.test(title) ? Number(title.match(/^\((\d+)\)/)[1]) : 0, look, `the title counts what needs a look (${title})`);
});

await test("Ctrl+K opens the command box: filter, go to a page, search the library", { width: 1200, height: 800 }, async (page) => {
  await page.goto(`${BASE}/`);
  await page.keyboard.press("Control+k");
  await page.locator("dialog.palette[open] input").waitFor();
  assert.ok((await page.locator("dialog.palette li").count()) >= 4, "the pages are listed");
  await page.fill("dialog.palette input", "activ");
  assert.equal((await page.locator("dialog.palette li").first().innerText()).trim(), "Activity");
  await page.keyboard.press("Enter");
  await page.waitForURL(/\/admin\/activity/);
  await page.keyboard.press("Control+k");
  await page.fill("dialog.palette input", "harbor");
  await page.locator("dialog.palette li").last().click();
  await page.waitForURL(/library\?q=harbor/);
  await page.keyboard.press("Control+k");
  await page.keyboard.press("Escape");
  assert.equal(await page.locator("dialog.palette[open]").count(), 0);
});

await test("the library: a title's sheet opens in a panel, and the chosen view is remembered", { width: 1200, height: 800 }, async (page) => {
  await page.goto(`${BASE}/library`);
  await page.locator("li:has-text('Night Harbor') [data-peek]").first().click();
  await page.locator("dialog.peekpanel[open] .hero h1").waitFor();
  assert.equal((await page.locator("dialog.peekpanel h1").innerText()).trim(), "Night Harbor");
  assert.match(await page.locator("dialog.peekpanel .peekbar a").getAttribute("href"), /\/admin\/library\/0100BBBB00000000$/);
  await page.keyboard.press("Escape");
  assert.equal(await page.locator("dialog.peekpanel[open]").count(), 0);
  await page.locator(".views a", { hasText: "List" }).click();
  await page.waitForURL(/view=list/);
  await page.goto(`${BASE}/library`); // no view in the address: the list comes back by itself
  await page.waitForURL(/view=list/);
  await page.locator(".views a", { hasText: "Grid" }).click();
  await page.waitForURL(/view=grid/);
  await page.goto(`${BASE}/library`);
  await page.waitForTimeout(400);
  assert.ok(!page.url().includes("view="), "the grid is the default: no redirect");
});

await test("the command box finds titles by name, and the live blocks look old when the server is lost", { width: 1200, height: 800 }, async (page) => {
  await page.goto(`${BASE}/`);
  await page.keyboard.press("Control+k");
  await page.fill("dialog.palette input", "harbor");
  await page.waitForSelector("dialog.palette li:has-text('Night Harbor')");
  await page.locator("dialog.palette li:has-text('Night Harbor')").click();
  await page.waitForURL(/library\/0100BBBB00000000/);
  await page.goto(`${BASE}/activity`);
  await page.waitForSelector("[data-livestate]:not([hidden]):not(.down)", { timeout: 4000 });
  await page.context().setOffline(true);
  await page.waitForSelector("body[data-down]", { timeout: 4000 });
  assert.ok(Number(await page.locator("[data-live]").first().evaluate((el) => getComputedStyle(el).opacity)) < 1);
  await page.context().setOffline(false);
  await page.waitForSelector("body:not([data-down])", { timeout: 8000 });
});

await test("the library: j and k walk the shelf, Space looks at a title, the arrows go to the next, ? lists the keys", { width: 1200, height: 800 }, async (page) => {
  await page.goto(`${BASE}/library`);
  await page.keyboard.press("j");
  const first = await page.evaluate(() => document.activeElement.getAttribute("href"));
  assert.match(first, /\/admin\/library\/0100/);
  await page.keyboard.press("j");
  const second = await page.evaluate(() => document.activeElement.getAttribute("href"));
  assert.notEqual(first, second);
  await page.keyboard.press("k");
  assert.equal(await page.evaluate(() => document.activeElement.getAttribute("href")), first);
  await page.keyboard.press(" ");
  await page.locator("dialog.peekpanel[open] .hero h1").waitFor();
  const name = (await page.locator("dialog.peekpanel h1").innerText()).trim();
  await page.keyboard.press("ArrowRight");
  await page.waitForFunction((old) => document.querySelector("dialog.peekpanel .hero h1")?.textContent.trim() !== old, name);
  await page.keyboard.press("ArrowLeft");
  await page.waitForFunction((old) => document.querySelector("dialog.peekpanel .hero h1")?.textContent.trim() === old, name);
  await page.keyboard.press("Escape");
  await page.keyboard.press("?");
  assert.ok((await page.locator("dialog.helpbox[open] dt").count()) >= 5);
  await page.keyboard.press("Escape");
  assert.equal(await page.locator("dialog.helpbox[open]").count(), 0);
  await page.fill("#q", "j"); // typing is left alone
  assert.equal(await page.inputValue("#q"), "j");
});

await test("the journal follows the server: a new line slides in by itself", { width: 1200, height: 800 }, async (page) => {
  await page.goto(`${BASE}/events`);
  await page.waitForFunction(() => document.querySelector("[data-live]").dataset.html !== undefined, null, { timeout: 4000 });
  const before = await page.locator(".timeline .ev").count();
  const other = await page.context().newPage();
  await other.goto(`${BASE}/config`);
  await other.fill("input[name=webhook]", `https://example.org/hook-${Date.now()}`);
  await other.locator("[data-saveall] button").click();
  await other.waitForURL(/config\?m=/);
  await page.waitForFunction((n) => document.querySelectorAll(".timeline .ev").length > n, before, { timeout: 8000 });
  assert.ok((await page.locator(".timeline .ev").first().innerText()).includes("Settings saved"));
  await other.close();
});

await test("the activity page as a screen across the room: only the big figures, and a way out", { width: 1280, height: 720 }, async (page) => {
  await page.goto(`${BASE}/activity`);
  assert.ok(await page.locator("body:not(.tv) .top").isVisible());
  await page.locator("[data-tv]").click();
  await page.waitForSelector("body.tv");
  assert.ok(!(await page.locator(".top").isVisible()) && !(await page.locator("footer").isVisible()), "the header and the footer are gone");
  assert.ok(await page.locator(".tile").first().isVisible() && await page.locator(".daychart").isVisible());
  assert.ok(!(await page.locator(".notv").first().isVisible()), "the long tables are left out");
  const size = await page.locator(".tile .n").first().evaluate((el) => parseFloat(getComputedStyle(el).fontSize));
  assert.ok(size >= 48, `the figures are big (${size}px)`);
  assert.ok(await page.evaluate(() => document.documentElement.scrollHeight <= window.innerHeight + 400), "the figures and the day fit a screen, the transfers may go below");
  assert.ok(await page.locator("[data-tv-exit]").isVisible());
  await page.locator("[data-tv-exit]").click();
  await page.waitForSelector("body:not(.tv)");
  assert.ok(await page.locator(".top").isVisible());
  await page.goto(`${BASE}/activity?tv=1`); // a screen that opens the address by itself
  await page.waitForSelector("body.tv");
  await page.keyboard.press("Escape");
  await page.waitForSelector("body:not(.tv)");
});

await test("push: the service worker shows a message of the bridge as an alert, and none when a page is in front", { width: 1200, height: 800 }, async (page) => {
  // The headless browser never grants notifications, so the worker's call to show one is replaced by a spy: the push
  // event is a real one (delivered through the browser's own protocol), what the worker decides to show is read back.
  const context = page.context();
  await page.goto(`${BASE}/config`);
  await page.evaluate(() => navigator.serviceWorker.ready);
  assert.ok((await page.locator("[data-push-note]").textContent()).includes("push service"), "a browser that can push is told how it goes");
  assert.ok(!(await page.locator("#alerts").innerText()).includes("third party"), "the card no longer says that nothing leaves");
  const cdp = await context.newCDPSession(page);
  const registrations = [];
  cdp.on("ServiceWorker.workerRegistrationUpdated", (e) => registrations.push(...e.registrations));
  await cdp.send("ServiceWorker.enable");
  await new Promise((resolve) => setTimeout(resolve, 300));
  const registration = registrations.find((r) => r.scopeURL.endsWith("/admin/"));
  assert.ok(registration, "the worker of the pages is registered");
  const worker = context.serviceWorkers()[0];
  await worker.evaluate(() => {
    self.__shown = [];
    self.registration.showNotification = (title, options) => { self.__shown.push({ title, body: options.body, tag: options.tag, url: options.data.url }); return Promise.resolve(); };
  });
  const shown = () => worker.evaluate(() => self.__shown);
  const deliver = (data) => cdp.send("ServiceWorker.deliverPushMessage", { origin: new URL(BASE).origin, registrationId: registration.registrationId, data: typeof data === "string" ? data : JSON.stringify(data) });
  const message = { title: "Test shelf", body: "Source down: games", tag: "lb-sources", url: "/admin/events" };
  await deliver(message); // a page is in front: it alerts by itself, the worker stays quiet
  await new Promise((resolve) => setTimeout(resolve, 500));
  assert.deepEqual(await shown(), []);
  await page.goto("about:blank"); // no page of the bridge any more
  await new Promise((resolve) => setTimeout(resolve, 300));
  await deliver(message);
  await deliver({ ...message, tag: "lb-update", url: "https://evil.example/" }); // not a page of the bridge: not followed
  await deliver("not json at all"); // a message that is not ours still shows something (a push must always be visible)
  for (let i = 0; i < 30 && (await shown()).length < 3; i++) await new Promise((resolve) => setTimeout(resolve, 150));
  assert.deepEqual(await shown(), [
    { title: "Test shelf", body: "Source down: games", tag: "lb-sources", url: "/admin/events" },
    { title: "Test shelf", body: "Source down: games", tag: "lb-update", url: "/admin/" },
    { title: "Lantern Bridge", body: "", tag: undefined, url: "/admin/" },
  ]);
});

await test("nothing moves for who asked for less motion", { width: 1200, height: 800 }, async (page) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`${BASE}/`);
  const moving = await page.evaluate(() => [...document.querySelectorAll("main *, .livestate")].filter((el) => getComputedStyle(el).animationName !== "none").length);
  assert.equal(moving, 0);
});

for (const [label, size] of [["desktop", { width: 1280, height: 800 }], ["phone", { width: 390, height: 800 }]]) {
  await test(`${label}: the library list can be ticked, and the bar follows`, size, async (page) => {
    await page.goto(`${BASE}/library?view=list`);
    assert.equal(await page.locator("[data-bulk]").isVisible(), false, "no bar before a tick");
    const boxes = page.locator("input[data-pick]");
    const n = await boxes.count();
    assert.ok(n >= 2, "the list has rows");
    await boxes.first().check();
    assert.equal(await page.locator("[data-bulk]").isVisible(), true, "the bar shows after a tick");
    assert.equal(await page.locator("[data-n]").innerText(), "1");
    const href = await page.locator("[data-export]").getAttribute("href");
    assert.ok(href.includes("ids="), "the export link names the ticked titles");
    if (label === "desktop") {
      await page.locator("[data-pick-all]").check();
      assert.equal(await page.locator("[data-n]").innerText(), String(n));
    }
    assert.ok(await noSideScroll(page), "the bar fits the screen");
    await page.locator("[data-clear]").click();
    assert.equal(await page.locator("[data-bulk]").isVisible(), false, "cleared");
    if (label === "desktop") { // a phone shows the rows as cards, with no header to sort by
      assert.equal(await page.locator("th[aria-sort] a").count(), 1, "one column is the sort");
      await page.getByRole("link", { name: /^Size/i }).click();
      await page.waitForURL(/sort=size/);
      assert.match(await page.locator("th[aria-sort] a").innerText(), /size/i, "the clicked column is the sort");
    }
  });
}

await test("the header's buttons are the same size: the language code, the switch, Sign out", { width: 1280, height: 800 }, async (page) => {
  await page.goto(`${BASE}/`);
  const size = (selector) => page.locator(selector).first().evaluate((el) => getComputedStyle(el).fontSize);
  assert.equal(await size(".langmenu summary"), await size(".out"), "the language code is as big as Sign out");
  const heights = await page.evaluate(() => [".langmenu summary", ".modes", ".out"].map((s) => document.querySelector(s).getBoundingClientRect().height));
  assert.ok(Math.max(...heights) - Math.min(...heights) < 4, `the header's buttons are as tall as each other (${heights})`);
});

await test("Convert…: the expected size follows the level and the mode", { width: 1280, height: 800 }, async (page) => {
  await page.goto(`${BASE}/library/0100AAAA00000000`);
  await page.locator("details.convert summary").first().click();
  const text = page.locator("[data-estimate]").first();
  const before = await text.innerText();
  assert.match(before, /%/, "an expected size, with the change in percent");
  await page.locator("details.convert select[name=level]").first().selectOption("3");
  const fast = await text.innerText();
  await page.locator("details.convert select[name=level]").first().selectOption("22");
  const small = await text.innerText();
  assert.ok(fast !== small, `the level changes the figure (${fast} / ${small})`);
  await page.locator("details.convert select[name=mode]").first().selectOption("block");
  assert.ok((await text.innerText()) !== small, "so does the mode");
});

await test("a conversion that fails lights a dot in the menu, readable in both themes", { width: 1280, height: 800 }, async (page) => {
  await page.goto(`${BASE}/library/0100AAAA00000000`);
  await page.locator("details.convert summary").first().click();
  await page.locator("details.convert form button").last().click();
  await page.waitForURL(/tasks/);
  await page.waitForSelector("[data-badge=manage]:not([hidden])", { timeout: 6000 });
  await page.waitForSelector("[data-badge=manage].bad", { timeout: 6000 }); // queued, then failed: info turns red
  assert.equal((await page.locator("[data-badge=tasks]").first().innerText()).trim(), "1");
  await page.locator(".more-btn").click();
  for (const theme of ["dark", "light"]) {
    await page.evaluate((t) => { document.documentElement.dataset.mode = t; document.documentElement.dataset.theme = t; }, theme);
    const ratio = await page.locator("[data-badge=tasks]").first().evaluate((el) => {
      const parse = (c) => c.match(/[\d.]+/g).slice(0, 3).map(Number);
      const lum = ([r, g, b]) => [r, g, b].map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }).reduce((a, v, i) => a + v * [0.2126, 0.7152, 0.0722][i], 0);
      const cs = getComputedStyle(el);
      const a = lum(parse(cs.color)), b = lum(parse(cs.backgroundColor));
      return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
    });
    assert.ok(ratio >= 4.5, `the dot is readable in the ${theme} theme (${ratio.toFixed(2)})`);
  }
});

await browser.close();
server.kill();
process.exit(failed ? 1 : 0);
