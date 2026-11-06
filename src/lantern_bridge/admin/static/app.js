// The pages' behaviour, all of it optional: without this script every form still works.
// Overview: the hashing figures refresh. Logs: follow the stream, pause, search. Pairings: the tab shows how
// many wait. Everywhere: a confirmation dialog, a toast for what was saved, copy buttons, a password reveal.
(() => {
  const say = (name) => document.body.dataset[name] || "";
  const get = () => fetch("/admin/api/state", { credentials: "same-origin" }).then((r) => (r.ok ? r.json() : Promise.reject(r.status)));

  // -- overview ---------------------------------------------------------------------------------------
  const bind = (state) => {
    for (const el of document.querySelectorAll("[data-bind]")) {
      const value = state[el.dataset.bind];
      if (value !== undefined && value !== null) el.textContent = value;
    }
    const bar = document.getElementById("progress");
    if (bar) { bar.max = state.total || 1; bar.value = state.hashed; }
  };
  if (document.getElementById("progress")) {
    const tick = () => get().then(bind).catch(() => {});
    tick();
    setInterval(tick, 2000);
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
        if (!button.classList.contains("link")) button.textContent = say("working");
      }
    }, 0);
  });
  window.addEventListener("pageshow", (e) => {
    if (e.persisted) location.reload(); // back to a form: it comes up fresh, its buttons usable
  });
})();
