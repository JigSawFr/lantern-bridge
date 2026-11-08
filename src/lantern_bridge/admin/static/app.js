// The pages' behaviour, all of it optional: without this script every form still works.
// Overview: the hashing figures refresh. Logs: follow the stream, pause, search. Pairings: the tab shows how
// many wait. Everywhere: a confirmation dialog, a toast for what was saved, copy buttons, a password reveal.
(() => {
  const say = (name) => document.body.dataset[name] || "";
  let toast = () => {};
  const get = () => fetch("/admin/api/state", { credentials: "same-origin" }).then((r) => (r.ok ? r.json() : Promise.reject(r.status)));

  // Import: a chosen file fills the text box beside it (the file never leaves the browser until it is sent).
  document.addEventListener("change", (e) => {
    const input = e.target;
    if (!input.matches || !input.matches("input[type=file][data-fill]") || !input.files[0]) return;
    const box = document.getElementById(input.dataset.fill);
    input.files[0].text().then((text) => { if (box) box.value = text; }).catch(() => {});
  });

  // -- motion: numbers count, bars glide, a changed value lights up once; nothing moves for who asked for less ------
  const calm = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const tween = (el, from, to, step, ms = 650) => {
    const id = (el.lbTween || 0) + 1;
    el.lbTween = id;
    if (calm || from === to || !Number.isFinite(from)) return step(to, true);
    const start = performance.now();
    const frame = (now) => {
      if (el.lbTween !== id) return; // a newer value took over
      const t = Math.min(1, (now - start) / ms);
      step(from + (to - from) * (1 - (1 - t) ** 3), t === 1);
      if (t < 1) requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
  };
  const flash = (el, name = "flash") => {
    if (calm) return;
    el.classList.remove(name);
    void el.offsetWidth; // the animation starts again
    el.classList.add(name);
  };
  const WHOLE = /^\d+$/;
  const count = (el, text) => {
    const old = el.textContent;
    if (old === text) return;
    if (WHOLE.test(old) && WHOLE.test(text)) tween(el, Number(old), Number(text), (v, end) => { el.textContent = end ? text : String(Math.round(v)); });
    else el.textContent = text;
    flash(el, "pop");
  };
  const glide = (bar, max, value) => {
    const from = Number(bar.value);
    bar.max = max;
    tween(bar, from, value, (v) => { bar.value = v; });
  };

  // -- overview -----------------------------------------------------------------------------------------
  const bind = (state) => {
    for (const el of document.querySelectorAll("[data-bind]")) {
      const value = state[el.dataset.bind];
      if (value !== undefined && value !== null) count(el, String(value));
    }
    const total = Number(state.total) > 0;
    const working = Number(state.pending) > 0;
    for (const el of document.querySelectorAll("[data-hashing]")) el.hidden = !working;
    for (const el of document.querySelectorAll("[data-done]")) el.hidden = !total || working;
    for (const el of document.querySelectorAll("[data-idle]")) el.hidden = total;
    const bar = document.getElementById("progress");
    if (bar) {
      glide(bar, state.total || 1, Number(state.hashed) || 0);
      bar.classList.toggle("working", working);
      const card = bar.closest(".card");
      if (card) card.classList.toggle("working", working);
    }
  };
  // -- the live state in the header: every stream says whether it is up; one down reads "reconnecting…" -------
  const status = (() => {
    const el = document.querySelector("[data-livestate]");
    const down = new Set();
    let seen = false;
    const show = () => {
      if (!el || !seen) return;
      const up = down.size === 0, text = up ? el.dataset.on : el.dataset.off;
      el.hidden = false;
      document.body.toggleAttribute("data-down", !up);
      el.classList.toggle("down", !up);
      el.title = text;
      el.setAttribute("aria-label", text);
      const txt = el.querySelector(".txt");
      if (txt) txt.textContent = text;
    };
    window.addEventListener("offline", () => { down.add("network"); show(); });
    window.addEventListener("online", () => { down.delete("network"); show(); });
    return (key, up) => {
      seen = true;
      if (up) down.delete(key); else down.add(key);
      show();
    };
  })();
  // One server-sent stream per followed address: the server sends the body when it changes. Without
  // EventSource (or when the stream cannot open) the same address is asked again every two seconds.
  const follow = (url, apply) => {
    let timer = 0;
    const poll = () => {
      if (timer) return;
      timer = setInterval(() => {
        if (document.hidden) return;
        fetch(url, { credentials: "same-origin" })
          .then((r) => (r.ok ? r.text() : Promise.reject(r.status)))
          .then((text) => { status(url, true); apply(text); })
          .catch(() => status(url, false));
      }, 2000);
    };
    if (!window.EventSource) return poll();
    const source = new EventSource(`/admin/live?u=${encodeURIComponent(url)}`);
    source.onopen = () => status(url, true);
    source.onmessage = (e) => { status(url, true); try { apply(JSON.parse(e.data)); } catch (_) { /* a broken line is skipped */ } };
    source.onerror = () => { status(url, false); if (source.readyState === EventSource.CLOSED) { source.close(); poll(); } };
  };

  // -- Convert…: the result's expected size follows the level and the mode ---------------------------------------
  for (const form of document.querySelectorAll("form[data-estimates]")) {
    let table = {};
    try { table = JSON.parse(form.dataset.estimates); } catch (_) { continue; }
    const text = form.querySelector("[data-estimate]"), note = form.querySelector("[data-estimate-note]");
    const refresh = () => {
      const level = form.elements.level ? form.elements.level.value : "", mode = form.elements.mode ? form.elements.mode.value : "";
      const found = table[level ? `${level}:${mode}` : "-"];
      if (!found || !text) return;
      text.textContent = found.text;
      if (note) note.textContent = found.measured ? note.parentElement.dataset.noteMeasured : note.parentElement.dataset.noteTypical;
    };
    form.addEventListener("change", refresh);
  }

  // -- the dots of the menu: where something needs a look, or is going on --------------------------------------
  const dots = (badges) => {
    for (const el of document.querySelectorAll("[data-badge]")) {
      const found = badges[el.dataset.badge];
      el.hidden = !found;
      if (found) { el.textContent = String(found.n); el.className = `dot ${found.level}`; }
    }
    tab(badges);
  };
  // The tab itself says so: "(2)" in front of the title when something needs a look, and a coloured dot on the
  // icon (red: broken, amber: worth a look, blue: going on), so that it shows from another tab too.
  const baseTitle = document.title;
  const icon = document.querySelector("link[rel=icon]");
  const mark = new Image();
  mark.src = icon ? icon.href : "";
  const LEVEL = { bad: "#ff5a4f", wait: "#ffc23d", info: "#33bbff" };
  let drawn = "";
  const tab = (badges) => {
    const list = Object.entries(badges).filter(([key]) => key !== "manage");
    const look = list.filter(([, b]) => b.level !== "info").reduce((n, [, b]) => n + b.n, 0);
    document.title = look ? `(${look}) ${baseTitle}` : baseTitle;
    const level = list.some(([, b]) => b.level === "bad") ? "bad" : list.some(([, b]) => b.level === "wait") ? "wait" : list.length ? "info" : "";
    if (!icon || level === drawn || !mark.complete || !mark.naturalWidth) return;
    drawn = level;
    try {
      const canvas = document.createElement("canvas");
      canvas.width = canvas.height = 64;
      const ctx = canvas.getContext("2d");
      ctx.drawImage(mark, 0, 0, 64, 64);
      if (level) {
        ctx.beginPath(); ctx.arc(48, 16, 14, 0, 7); ctx.fillStyle = "#0b1424"; ctx.fill();
        ctx.beginPath(); ctx.arc(48, 16, 10, 0, 7); ctx.fillStyle = LEVEL[level]; ctx.fill();
      }
      icon.type = "image/png";
      icon.href = canvas.toDataURL("image/png");
    } catch (_) { /* an icon that cannot be drawn stays as it was */ }
  };
  // What shows up while the page is open is said where the eyes are: a toast, once, when a count grows.
  const announce = (badges, was) => {
    for (const key of ["overview", "pairings", "sources", "tasks"]) {
      const now = badges[key], text = say(`t${key[0].toUpperCase()}${key.slice(1)}`);
      if (now && now.level !== "info" && text && (!was[key] || now.n > was[key].n || was[key].level === "info")) toast(text, now.level === "bad" ? "bad" : "wait", `/admin/${key === "overview" ? "" : key === "tasks" ? "tasks" : key}`);
    }
  };
  // Alerts: when the dots show something new that needs a look, a browser that allowed it shows a notification
  // (through the service worker, so that an installed app gets it too). Only this device decides, and only while
  // the pages are open: nothing goes through a third party. The texts are kept in the browser when alerts are
  // turned on, so that any page can show them.
  const ALERTS = "lb-alerts", TEXTS = "lb-alert-texts";
  const store = (key, value) => { try { if (value === null) localStorage.removeItem(key); else if (value !== undefined) localStorage.setItem(key, value); return localStorage.getItem(key); } catch (_) { return null; } };
  const alertsOn = () => "Notification" in window && Notification.permission === "granted" && store(ALERTS) === "1";
  let before = null;
  const notify = (badges) => {
    if (before !== null && alertsOn() && navigator.serviceWorker) {
      let texts = {};
      try { texts = JSON.parse(store(TEXTS) || "{}"); } catch (_) { /* skipped */ }
      for (const key of ["overview", "pairings", "sources", "tasks"]) {
        const now = badges[key], was = before[key];
        if (now && now.level !== "info" && texts[key] && (!was || now.n > was.n || was.level === "info")) {
          navigator.serviceWorker.ready.then((reg) => reg.showNotification(texts.title || "Lantern Bridge", { body: texts[key], tag: `lb-${key}`, icon: "/admin/static/icon-192.png", data: { url: "/admin/" } })).catch(() => {});
        }
      }
    }
    if (before !== null) announce(badges, before);
    before = badges;
  };
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/admin/sw.js", { scope: "/admin/" }).catch(() => {});
  const card = document.getElementById("alerts");
  if (card) {
    const state = card.querySelector("[data-notify-state]"), button = card.querySelector("[data-notify-toggle]");
    const labels = { on: button.textContent, off: card.dataset.turnOff };
    const show = () => {
      if (!("Notification" in window) || !("serviceWorker" in navigator)) { state.textContent = card.dataset.unsupported; button.hidden = true; return; }
      if (Notification.permission === "denied") { state.textContent = card.dataset.blocked; button.hidden = true; return; }
      state.textContent = alertsOn() ? card.dataset.on : card.dataset.off;
      button.textContent = alertsOn() ? labels.off : labels.on;
    };
    button.addEventListener("click", async () => {
      try {
        if (alertsOn()) store(ALERTS, "0");
        else if ((await Notification.requestPermission()) === "granted") {
          const { title, overview, pairings, sources, tasks } = card.dataset;
          store(TEXTS, JSON.stringify({ title, overview, pairings, sources, tasks }));
          store(ALERTS, "1");
        }
      } catch (_) { /* skipped */ }
      show();
    });
    show();
  }
  if (document.querySelector("[data-badge]")) {
    follow("/admin/api/badges", (text) => { try { const found = typeof text === "string" ? JSON.parse(text) : text; dots(found); notify(found); } catch (_) { /* skipped */ } });
  }

  if (document.getElementById("progress")) {
    get().then(bind).catch(() => {});
    follow("/admin/api/state", (text) => { try { bind(JSON.parse(text)); } catch (_) { /* skipped */ } });
  }

  // -- tables: on a narrow screen each row becomes a card, so every cell carries its column's name -------
  const labelTables = (root) => {
    for (const table of root.querySelectorAll("table")) {
      const heads = [...table.querySelectorAll("thead th")].map((th) => th.textContent.trim());
      for (const row of table.querySelectorAll("tbody tr")) {
        [...row.children].forEach((td, i) => { if (heads[i] && td.colSpan === 1) td.dataset.label = heads[i]; });
      }
    }
  };
  labelTables(document);

  // -- moments read as "3 min ago" for a day, then as the exact time (which stays in the tooltip) -----------
  const rtf = typeof Intl.RelativeTimeFormat === "function" ? new Intl.RelativeTimeFormat(document.documentElement.lang || "en", { numeric: "auto", style: "short" }) : null;
  const ago = (root) => {
    if (!rtf) return;
    for (const el of root.querySelectorAll("time.ago[datetime]")) {
      const seconds = (Date.parse(el.getAttribute("datetime")) - Date.now()) / 1000;
      if (!Number.isFinite(seconds) || seconds > 5 || seconds < -86400) continue;
      const [value, unit] = seconds > -60 ? [Math.round(seconds), "second"] : seconds > -3600 ? [Math.round(seconds / 60), "minute"] : [Math.round(seconds / 3600), "hour"];
      el.textContent = rtf.format(value, unit);
    }
  };
  ago(document);
  setInterval(() => { if (!document.hidden) ago(document); }, 30000);

  // -- blocks that follow the server: their HTML arrives when it changes ------------------------------------
  // The new HTML takes the block's place; what changed in it is shown: a cell or a figure lights up once, a
  // number counts to its new value, a bar glides, a row that was not there slides in.
  // The two rates of the activity page are drawn as they come: one sample each time the block changes, kept for
  // the last two minutes (and across pages of the tab), drawn as a line under the tile.
  const WINDOW = 120000;
  let samples = [];
  try { samples = JSON.parse(sessionStorage.getItem("lb-samples") || "[]"); } catch (_) { /* none */ }
  const sparks = (root) => {
    const holder = root.querySelector("[data-sample]");
    if (!holder) return;
    const now = Date.now();
    const [bytes, requests] = holder.dataset.sample.split(",").map(Number);
    if (!samples.length && holder.dataset.seed) {
      // the bridge remembers the last minute: the curve starts with it instead of empty
      samples = holder.dataset.seed.split(";").filter(Boolean).map((p) => { const [t, b, r] = p.split(":").map(Number); return [t * 1000, b, r]; });
    }
    samples = samples.filter((s) => now - s[0] <= WINDOW);
    if (!samples.length || now - samples[samples.length - 1][0] > 400) samples.push([now, bytes, requests]);
    try { sessionStorage.setItem("lb-samples", JSON.stringify(samples)); } catch (_) { /* skipped */ }
    for (const svg of root.querySelectorAll("svg[data-spark]")) {
      const k = Number(svg.dataset.spark) + 1;
      const top = Math.max(1, ...samples.map((s) => s[k]));
      const point = (s) => `${(((s[0] - (now - WINDOW)) / WINDOW) * 120).toFixed(1)},${(26 - (s[k] / top) * 24).toFixed(1)}`;
      const last = samples[samples.length - 1];
      const line = samples.map(point).join(" L") + ` L120,${(26 - (last[k] / top) * 24).toFixed(1)}`;
      svg.querySelector(".line").setAttribute("d", `M${line}`);
      svg.querySelector(".fill").setAttribute("d", `M${line} L120,28 L${point(samples[0]).split(",")[0]},28Z`);
    }
  };
  const PICK = "td, .tile .n, .pill, progress";
  const swap = (live, html) => {
    const next = document.createElement("div");
    next.innerHTML = html;
    ago(next);
    const was = [...live.querySelectorAll(PICK)], now = [...next.querySelectorAll(PICK)];
    const ROWS = "tbody tr, .timeline .ev";
    const rows = new Set([...live.querySelectorAll(ROWS)].map((tr) => tr.textContent.trim()));
    const changes = [];
    if (was.length === now.length && live.querySelectorAll(ROWS).length === next.querySelectorAll(ROWS).length) {
      now.forEach((el, i) => {
        const old = was[i];
        if (el.tagName === "PROGRESS") { if (old.tagName === "PROGRESS") changes.push(["bar", el, Number(old.value)]); return; }
        if (el.querySelector(PICK) || el.querySelector("time.ago")) return; // its parts speak for it; a moment ages by itself
        const before = old.textContent.trim(), after = el.textContent.trim();
        if (before !== after) changes.push([el.matches(".tile .n") ? "count" : "flash", el, before]);
      });
    }
    live.replaceChildren(...next.childNodes);
    sparks(live);
    for (const [kind, el, before] of changes) {
      if (kind === "bar") { const to = Number(el.value); el.value = before; glide(el, Number(el.max), to); }
      else if (kind === "count") { const text = el.textContent; el.textContent = before; count(el, text); }
      else flash(el);
    }
    if (!changes.length && rows.size) {
      for (const tr of live.querySelectorAll(ROWS)) if (!rows.has(tr.textContent.trim())) flash(tr, "enter");
    }
  };
  for (const live of document.querySelectorAll("[data-live]")) {
    if (!live.dataset.live.startsWith("/")) continue;
    follow(live.dataset.live, (html) => {
      if (document.hidden || html === live.dataset.html) return;
      if (live.contains(document.activeElement) && document.activeElement !== document.body) return; // not under a click or a keyboard
      swap(live, html);
      live.dataset.html = html;
      labelTables(live);
    });
  }

  // -- the settings pages sit under "Manage" on a wide screen; without script (or on a narrow one) they are a flat row -----
  document.documentElement.classList.add("js");
  const more = document.querySelector(".more-btn");
  if (more) {
    more.hidden = false;
    const shut = (focus) => {
      more.setAttribute("aria-expanded", "false");
      if (focus) more.focus();
    };
    more.addEventListener("click", () => more.setAttribute("aria-expanded", String(more.getAttribute("aria-expanded") !== "true")));
    document.addEventListener("click", (e) => { if (!e.target.closest(".more")) shut(false); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && more.getAttribute("aria-expanded") === "true") shut(true); });
    document.querySelector(".more").addEventListener("focusout", (e) => { if (!e.currentTarget.contains(e.relatedTarget)) shut(false); });
  }

  // -- the menu scrolls sideways on a narrow screen: the current page is brought into view ----------------
  const here = document.querySelector("nav a[aria-current]");
  if (here && here.scrollIntoView) here.scrollIntoView({ block: "nearest", inline: "center" });

  // -- "Add a source": a folder has no shop user or password --------------------------------------------
  const kind = document.querySelector("select[data-kind]");
  if (kind) {
    const where = kind.form.elements.where;
    const adjust = () => {
      const shop = kind.value !== "folder";
      for (const el of kind.form.querySelectorAll("[data-shop-only]")) {
        el.hidden = !shop;
        for (const input of el.querySelectorAll("input")) input.disabled = !shop;
      }
      for (const el of kind.form.querySelectorAll("[data-folder-only]")) {
        el.hidden = shop;
        for (const input of el.querySelectorAll("input")) input.disabled = shop;
      }
      where.placeholder = shop ? where.dataset.phShop : where.dataset.phFolder;
    };
    kind.addEventListener("change", adjust);
    adjust();
  }

  // -- the library's filters apply as they change (the search waits for a pause in the typing) ------------
  for (const form of document.querySelectorAll("form[data-autosubmit]")) {
    let timer = 0;
    // the list under the filters is greyed and shimmers until the next page comes
    form.addEventListener("submit", () => { for (const list of document.querySelectorAll(".shelf, main .scroll")) list.setAttribute("aria-busy", "true"); });
    form.addEventListener("change", () => form.requestSubmit());
    form.addEventListener("input", (e) => {
      if (e.target.type !== "search") return;
      clearTimeout(timer);
      timer = setTimeout(() => form.requestSubmit(), 350);
    });
  }

  // -- the library's list: tick titles, then hash them again or export them -----------------------------------
  const bulk = document.querySelector("[data-bulk]");
  if (bulk) {
    const picks = () => [...document.querySelectorAll("input[data-pick]")];
    const all = document.querySelector("[data-pick-all]");
    const sync = () => {
      const ids = picks().filter((b) => b.checked).map((b) => b.value);
      bulk.hidden = ids.length === 0;
      bulk.querySelector("[data-n]").textContent = String(ids.length);
      bulk.querySelector("input[name=ids]").value = ids.join(",");
      bulk.querySelector("[data-export]").href = "/admin/library/export?format=csv&ids=" + encodeURIComponent(ids.join(","));
      if (all) { all.checked = ids.length > 0 && ids.length === picks().length; all.indeterminate = ids.length > 0 && !all.checked; }
    };
    document.addEventListener("change", (e) => {
      if (e.target === all) for (const b of picks()) b.checked = all.checked;
      if (e.target === all || (e.target.matches && e.target.matches("input[data-pick]"))) sync();
    });
    bulk.querySelector("[data-clear]").addEventListener("click", () => { for (const b of picks()) b.checked = false; sync(); });
  }

  // -- the language menu closes on a click elsewhere and on Escape ---------------------------------------------
  const langs = document.querySelector(".langmenu");
  if (langs) {
    document.addEventListener("click", (e) => { if (!langs.contains(e.target)) langs.open = false; });
    langs.addEventListener("keydown", (e) => { if (e.key === "Escape") { langs.open = false; langs.querySelector("summary").focus(); } });
  }

  // -- a settings card that was edited says so; one bar saves every edited card, and a box finds a setting -----
  const cards = [...document.querySelectorAll("form[data-dirty]")];
  const bar = document.querySelector("[data-saveall]");
  const edited = () => cards.filter((f) => f.classList.contains("dirty") && !f.hasAttribute("data-solo"));
  const refresh = () => {
    if (!bar) return;
    const n = edited().length;
    bar.hidden = n === 0;
    const count = bar.querySelector("[data-count]");
    if (count) count.textContent = String(n);
  };
  for (const form of cards) {
    const note = form.querySelector("[data-dirty-note]");
    const mark = () => { form.classList.add("dirty"); if (note) note.hidden = false; refresh(); };
    form.addEventListener("input", mark);
    form.addEventListener("change", mark);
  }
  if (bar) {
    document.body.classList.add("saveall");
    const button = bar.querySelector("button");
    button.addEventListener("click", async () => {
      button.disabled = true;
      let last = location.href;
      for (const form of edited()) {
        for (const old of form.querySelectorAll(":scope > .error[data-saveall-error]")) old.remove();
        const reply = await fetch(form.action, { method: "POST", body: new URLSearchParams(new FormData(form)) });
        if (reply.ok && /[?&]m=/.test(reply.url)) { last = reply.url; continue; }
        const problem = new DOMParser().parseFromString(await reply.text(), "text/html").querySelector(".error");
        const line = document.createElement("p");
        line.className = "error";
        line.setAttribute("role", "alert");
        line.setAttribute("data-saveall-error", "");
        line.textContent = problem ? problem.textContent : bar.dataset.failed;
        form.prepend(line);
        line.scrollIntoView({ block: "center" });
        button.disabled = false;
        return;
      }
      location.href = last;
    });
  }
  const find = document.getElementById("settings-search");
  if (find) {
    find.closest("[data-search-box]").hidden = false;
    const none = document.getElementById("settings-none");
    find.addEventListener("input", () => {
      const q = find.value.trim().toLowerCase();
      let shown = 0;
      for (const label of document.querySelectorAll("form[data-dirty] label")) {
        const names = [...label.querySelectorAll("input, select, textarea")].map((i) => `${i.name} ${i.placeholder || ""}`);
        const hit = !q || `${label.textContent} ${names.join(" ")}`.toLowerCase().includes(q);
        label.hidden = !hit;
        if (hit) shown += 1;
      }
      for (const group of document.querySelectorAll("form[data-dirty] fieldset, form.card")) {
        const rows = group.querySelectorAll("label");
        group.hidden = q !== "" && rows.length > 0 && [...rows].every((r) => r.hidden);
      }
      if (none) none.hidden = shown > 0 || !q;
    });
  }

  // -- a title with files waiting to be hashed refreshes itself until they are done ---------------------
  if (document.querySelector("[data-watch-pending]")) {
    setTimeout(() => { if (!document.querySelector("dialog[open]")) location.reload(); }, 3000);
  }

  // -- Ctrl/Cmd+K: one box to go to a page, to search the library, or to ask for a scan ----------------------------
  const opener = document.querySelector("[data-palette-open]");
  if (opener && typeof HTMLDialogElement === "function") {
    const mac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
    opener.querySelector("kbd").textContent = mac ? "⌘K" : "Ctrl K";
    opener.hidden = false;
    let box = null, input, list, items = [], at = 0, titles = [], asked = 0, wait = 0;
    const build = () => {
      box = document.createElement("dialog");
      box.className = "palette";
      box.setAttribute("aria-label", say("search"));
      input = document.createElement("input");
      input.type = "search";
      input.placeholder = say("search");
      input.setAttribute("aria-label", say("search"));
      input.autocomplete = "off";
      list = document.createElement("ul");
      list.setAttribute("role", "listbox");
      box.append(input, list);
      document.body.append(box);
      input.addEventListener("input", () => { render(); lookup(); });
      input.addEventListener("keydown", (e) => {
        if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); at = (at + (e.key === "ArrowDown" ? 1 : items.length - 1)) % Math.max(1, items.length); mark(); }
        else if (e.key === "Enter") { e.preventDefault(); if (items[at]) items[at].run(); }
      });
      box.addEventListener("click", (e) => { if (e.target === box) box.close(); });
    };
    const pages = () => {
      const seen = new Set();
      return [...document.querySelectorAll("header nav a[href]")].map((a) => {
        const label = [...a.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join("").trim();
        return { label, run: () => { location.href = a.href; } };
      }).filter((p) => p.label && !seen.has(p.label) && seen.add(p.label));
    };
    // The titles that match come from the bridge once the typing pauses; they join the list when they arrive.
    const lookup = () => {
      clearTimeout(wait);
      const q = input.value.trim();
      if (q.length < 2) { titles = []; asked++; list.removeAttribute("aria-busy"); render(); return; }
      list.setAttribute("aria-busy", "true");
      wait = setTimeout(() => {
        const mine = ++asked;
        fetch(`/admin/library/find?q=${encodeURIComponent(q)}`, { credentials: "same-origin" })
          .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
          .then((rows) => { if (mine === asked) { titles = rows; list.removeAttribute("aria-busy"); render(true); } })
          .catch(() => { if (mine === asked) list.removeAttribute("aria-busy"); });
      }, 160);
    };
    const mark = () => {
      [...list.children].forEach((li, i) => { li.setAttribute("aria-selected", String(i === at)); if (i === at) li.scrollIntoView({ block: "nearest" }); });
    };
    const render = (keep) => {
      const q = input.value.trim().toLowerCase();
      items = pages().filter((p) => !q || p.label.toLowerCase().includes(q));
      if (document.body.dataset.csrf && (!q || say("scan").toLowerCase().includes(q))) {
        items.push({ label: say("scan"), run: () => {
          const form = document.createElement("form");
          form.method = "post"; form.action = "/admin/scan"; form.hidden = true;
          for (const [name, value] of [["csrf", document.body.dataset.csrf], ["action", "scan"]]) { const f = document.createElement("input"); f.name = name; f.value = value; form.append(f); }
          document.body.append(form); form.submit();
        } });
      }
      if (q) for (const t of titles) items.push({ label: t.name, hint: t.id, run: () => { location.href = `/admin/library/${t.id}`; } });
      if (q) items.push({ label: `“${input.value.trim()}”`, run: () => { location.href = `/admin/library?q=${encodeURIComponent(input.value.trim())}`; } });
      at = keep ? Math.min(at, Math.max(0, items.length - 1)) : 0;
      list.replaceChildren(...items.map((item, i) => {
        const li = document.createElement("li");
        li.setAttribute("role", "option");
        li.textContent = item.label;
        if (item.hint) { const id = document.createElement("code"); id.textContent = item.hint; li.append(" ", id); }
        li.addEventListener("click", () => item.run());
        li.addEventListener("mousemove", () => { at = i; mark(); });
        return li;
      }));
      mark();
    };
    const open = () => {
      if (!box) build();
      if (box.open) return;
      input.value = "";
      titles = [];
      render();
      box.showModal();
      input.focus();
    };
    opener.addEventListener("click", open);
    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "k") { e.preventDefault(); if (box && box.open) box.close(); else open(); }
    });
  }

  // -- the library keeps the view you chose (grid or list) ------------------------------------------------------
  const views = document.querySelector(".views");
  if (views) {
    const VIEW = "lb-view";
    const params = new URLSearchParams(location.search);
    if (!params.get("view") && store(VIEW) === "list") { params.set("view", "list"); location.replace(`${location.pathname}?${params}`); }
    views.addEventListener("click", (e) => {
      const a = e.target.closest && e.target.closest("a[href*='view=']");
      if (a) store(VIEW, new URL(a.href).searchParams.get("view") === "list" ? "list" : "grid");
    });
  }

  // -- a title's sheet beside the shelf, without leaving it; the arrows go to the next or the previous one -----------
  let peekAt = null; // opens the sheet of the title at this place in the shelf (set once the panel exists)
  if (document.querySelector("[data-peek]") && typeof HTMLDialogElement === "function") {
    let panel = null, body, shown = -1;
    const buttons = () => [...document.querySelectorAll("[data-peek]")];
    const close = () => { if (panel && panel.open) panel.close(); };
    const step = (by) => { const to = shown + by; if (to >= 0 && to < buttons().length) peekAt(to); };
    const make = () => {
      panel = document.createElement("dialog");
      panel.className = "peekpanel";
      const bar = document.createElement("div");
      bar.className = "peekbar";
      const go = document.createElement("a");
      go.className = "button small";
      go.textContent = `${say("open")} →`;
      const move = document.createElement("span");
      move.className = "peekmove";
      const arrow = (text, name, by) => {
        const b = document.createElement("button");
        b.type = "button"; b.className = "ghost small"; b.textContent = text; b.setAttribute("aria-label", say(name)); b.title = say(name);
        b.addEventListener("click", () => step(by));
        return b;
      };
      move.append(arrow("←", "previous", -1), arrow("→", "next", 1));
      const x = document.createElement("button");
      x.type = "button"; x.className = "ghost small"; x.textContent = "×"; x.setAttribute("aria-label", say("cancel"));
      x.addEventListener("click", close);
      bar.append(go, move, x);
      body = document.createElement("div");
      body.className = "peekbody";
      panel.append(bar, body);
      panel.addEventListener("click", (e) => { if (e.target === panel) close(); });
      panel.addEventListener("keydown", (e) => {
        if (e.key === "ArrowRight" || e.key === "ArrowLeft") { e.preventDefault(); step(e.key === "ArrowRight" ? 1 : -1); }
      });
      panel.addEventListener("close", () => { const list = buttons(); if (list[shown]) list[shown].focus(); });
      panel.go = go; panel.move = move;
      document.body.append(panel);
    };
    peekAt = (index) => {
      const list = buttons();
      if (!list[index]) return;
      if (!panel) make();
      shown = index;
      const href = list[index].dataset.peek;
      panel.go.href = href;
      panel.move.children[0].disabled = index === 0;
      panel.move.children[1].disabled = index === list.length - 1;
      body.replaceChildren();
      body.setAttribute("aria-busy", "true");
      if (!panel.open) panel.showModal();
      fetch(href, { credentials: "same-origin" }).then((r) => (r.ok ? r.text() : Promise.reject(r.status))).then((html) => {
        if (shown !== index) return; // the arrows went on
        const doc = new DOMParser().parseFromString(html, "text/html");
        const parts = [...doc.querySelectorAll("main > .hero, main > .shots, main > .atlas")];
        if (!parts.length) { location.href = href; return; }
        body.replaceChildren(...parts.map((p) => document.importNode(p, true)));
        body.removeAttribute("aria-busy");
      }).catch(() => { if (shown === index) location.href = href; });
    };
    document.addEventListener("click", (e) => {
      const button = e.target.closest && e.target.closest("[data-peek]");
      if (button) peekAt(buttons().indexOf(button));
    });
  }

  // -- the activity page on a screen across the room: only the big figures, the day and the transfers --------------
  const tvOn = document.querySelector("[data-tv]");
  if (tvOn) {
    const body = document.body, exit = document.querySelector("[data-tv-exit]");
    const set = (on) => {
      body.classList.toggle("tv", on);
      if (exit) exit.hidden = !on;
      if (!on && document.fullscreenElement) document.exitFullscreen().catch(() => {});
    };
    tvOn.addEventListener("click", () => {
      set(true);
      if (document.documentElement.requestFullscreen) document.documentElement.requestFullscreen().catch(() => {}); // refused: the page still fills the window
    });
    if (exit) exit.addEventListener("click", () => set(false));
    document.addEventListener("fullscreenchange", () => { if (!document.fullscreenElement && body.classList.contains("tv")) set(false); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && body.classList.contains("tv") && !document.querySelector("dialog[open]")) set(false); });
    if (new URLSearchParams(location.search).get("tv") === "1") set(true); // a screen that opens the address by itself
  }

  // -- keys: j and k walk the shelf (or the list), Space looks at the title, ? lists them ---------------------------
  {
    const typing = (el) => !!el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
    const rows = () => [...document.querySelectorAll(".shelf > li a.game, table.dense tbody tr td:nth-child(2) a")];
    let help = null;
    const showHelp = () => {
      if (typeof HTMLDialogElement !== "function") return;
      if (!help) {
        help = document.createElement("dialog");
        help.className = "helpbox";
        help.setAttribute("aria-label", say("shortcuts"));
        const title = document.createElement("h2");
        title.textContent = say("shortcuts");
        const list = document.createElement("dl");
        const rowsOf = [["Ctrl K", say("search")], ["/", say("search")], ["j", say("next")], ["k", say("previous")], ["Space", say("open")], ["?", say("shortcuts")]];
        for (const [key, label] of rowsOf) {
          const dt = document.createElement("dt"), kbd = document.createElement("kbd");
          kbd.className = "key"; kbd.textContent = key; dt.append(kbd);
          const dd = document.createElement("dd"); dd.textContent = label;
          list.append(dt, dd);
        }
        help.append(title, list);
        help.addEventListener("click", (e) => { if (e.target === help) help.close(); });
        document.body.append(help);
      }
      if (help.open) help.close(); else help.showModal();
    };
    document.addEventListener("keydown", (e) => {
      if (e.ctrlKey || e.metaKey || e.altKey || typing(e.target) || e.target.closest("dialog[open]") && e.key !== "?") return;
      if (e.key === "?") { e.preventDefault(); showHelp(); return; }
      const list = rows();
      if (!list.length) return;
      const here = list.findIndex((a) => a === document.activeElement || a.closest("li, tr").contains(document.activeElement));
      if (e.key === "j" || e.key === "k") {
        e.preventDefault();
        const to = Math.max(0, Math.min(list.length - 1, here < 0 ? 0 : here + (e.key === "j" ? 1 : -1)));
        list[to].focus();
        list[to].scrollIntoView({ block: "nearest" });
      } else if (e.key === " " && here >= 0 && peekAt) {
        const button = list[here].closest("li").querySelector("[data-peek]");
        if (button) { e.preventDefault(); peekAt([...document.querySelectorAll("[data-peek]")].indexOf(button)); }
      }
    });
  }

  // -- "/" jumps to the search box, as on most sites; Escape leaves it ---------------------------------------
  const search = document.getElementById("q");
  if (search) {
    document.addEventListener("keydown", (e) => {
      const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement ? document.activeElement.tagName : "");
      if (e.key === "/" && !typing && !e.ctrlKey && !e.metaKey) { e.preventDefault(); search.focus(); search.select(); }
      else if (e.key === "Escape" && document.activeElement === search) search.blur();
    });
  }

  // -- logs -------------------------------------------------------------------------------------------
  const log = document.getElementById("log");
  if (log && window.EventSource) {
    let paused = false;
    const grep = document.getElementById("grep");
    const pause = document.getElementById("pause");
    const live = document.getElementById("live");
    const stamp = (t) => new Date(t * 1000).toLocaleString();
    const matches = (row) => !grep || !grep.value || row.textContent.toLowerCase().includes(grep.value.toLowerCase());
    const refilter = () => { for (const row of log.children) row.hidden = !matches(row); };
    const add = (l) => {
      const row = document.createElement("div");
      row.className = `ln ${l.level.toLowerCase()}`;
      for (const [tag, text] of [["time", stamp(l.time)], ["b", l.level], ["span", l.message]]) {
        const part = document.createElement(tag);
        part.textContent = text;
        row.append(part, " ");
      }
      row.hidden = !matches(row);
      log.append(row);
      while (log.children.length > 1000) log.firstChild.remove();
    };
    log.scrollTop = log.scrollHeight;
    const source = new EventSource(`/admin/logs/stream?level=${encodeURIComponent(log.dataset.level)}&after=${log.dataset.last}`);
    source.onmessage = (e) => {
      if (paused) return;
      const stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 24;
      add(JSON.parse(e.data));
      if (stick) log.scrollTop = log.scrollHeight;
    };
    source.onerror = () => { if (live) { live.textContent = live.dataset.offline; live.className = "pill wait"; } };
    source.onopen = () => { if (live) { live.textContent = live.dataset.live; live.className = "pill ok"; } };
    if (grep) grep.addEventListener("input", refilter);
    if (pause) pause.addEventListener("click", () => {
      paused = !paused;
      pause.textContent = paused ? pause.dataset.resume : pause.dataset.pause;
      if (!paused) log.scrollTop = log.scrollHeight;
    });
    refilter();
  }

  // -- pairings: the tab says how many wait, and the page follows --------------------------------------
  const pairings = document.getElementById("pairings");
  const waiting = Number(pairings ? pairings.dataset.waiting : 0);
  if (pairings) {
    if (waiting > 0) document.title = `(${waiting}) ${document.title}`;
    setInterval(() => get().then((state) => {
      const typing = document.activeElement && document.activeElement.tagName === "INPUT";
      if (String(state.waiting) !== pairings.dataset.waiting && !typing) location.reload();
    }).catch(() => {}), 3000);
  }

  // -- a confirmation dialog in place of the browser's -------------------------------------------------
  const dialog = document.createElement("dialog");
  dialog.className = "ask";
  dialog.innerHTML = '<form method="dialog"><p></p><div class="row"><button value="no" class="ghost"></button><button value="yes" class="danger"></button></div></form>';
  document.body.append(dialog);
  const ask = (message) => new Promise((resolve) => {
    dialog.querySelector("p").textContent = message;
    const [no, yes] = dialog.querySelectorAll("button");
    no.textContent = say("cancel");
    yes.textContent = say("confirmLabel");
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "yes"), { once: true });
    dialog.returnValue = "no";
    dialog.showModal();
    no.focus();
  });
  document.addEventListener("click", (e) => {
    const trigger = e.target.closest && e.target.closest("[data-confirm]");
    if (!trigger || trigger.dataset.confirmed || !dialog.showModal) return;
    e.preventDefault();
    ask(trigger.dataset.confirm).then((yes) => {
      if (!yes) return;
      trigger.dataset.confirmed = "1";
      trigger.click();
    });
  });

  // -- what was saved is said once, then goes -----------------------------------------------------------
  const toasts = document.getElementById("toasts");
  toast = (text, level = "good", href = "") => {
    if (!toasts || document.hidden) return;
    const note = document.createElement(href ? "a" : "p");
    note.className = `toast ${level}`;
    note.setAttribute("role", "status");
    note.textContent = text;
    if (href) note.href = href;
    toasts.append(note);
    while (toasts.children.length > 3) toasts.firstElementChild.remove();
    setTimeout(() => note.classList.add("gone"), 7000);
    setTimeout(() => note.remove(), 7600);
  };
  for (const note of document.querySelectorAll("[data-toast]")) {
    if (!toasts) break;
    note.classList.add("toast");
    toasts.append(note);
    setTimeout(() => note.classList.add("gone"), 6000);
    setTimeout(() => note.remove(), 6600);
  }

  // -- copy ---------------------------------------------------------------------------------------------
  document.addEventListener("click", (e) => {
    const copy = e.target.closest && e.target.closest("[data-copy-text]");
    if (!copy || !navigator.clipboard) return;
    navigator.clipboard.writeText(copy.dataset.copyText).then(() => {
      const label = copy.textContent;
      copy.textContent = say("copied");
      setTimeout(() => { copy.textContent = label; }, 1500);
    }).catch(() => {});
  });

  // -- a password can be looked at, a form is sent once ---------------------------------------------------
  for (const toggle of document.querySelectorAll("[data-reveal]")) {
    const input = toggle.previousElementSibling;
    toggle.addEventListener("click", () => {
      const shown = input.type === "text";
      input.type = shown ? "password" : "text";
      toggle.textContent = shown ? say("show") : say("hide");
    });
  }
  document.addEventListener("submit", (e) => {
    const form = e.target;
    if (form.method !== "post") return;
    setTimeout(() => {
      for (const button of form.querySelectorAll("button:not([type=button])")) {
        button.disabled = true;
        button.setAttribute("aria-busy", "true");
        if (!button.classList.contains("link")) button.textContent = say("working");
      }
    }, 0);
  });
  window.addEventListener("pageshow", (e) => {
    if (e.persisted) location.reload(); // back to a form: it comes up fresh, its buttons usable
  });
})();
