// Overview: the hashing figures refresh every two seconds. Logs: follow the stream.
(() => {
  const bind = (state) => {
    for (const el of document.querySelectorAll("[data-bind]")) {
      const value = state[el.dataset.bind];
      if (value !== undefined && value !== null) el.textContent = value;
    }
    const bar = document.getElementById("progress");
    if (bar) { bar.max = state.total || 1; bar.value = state.hashed; }
  };
  if (document.getElementById("progress")) {
    const tick = () => fetch("/admin/api/state", { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then(bind)
      .catch(() => {});
    tick();
    setInterval(tick, 2000);
  }
  const log = document.getElementById("log");
  if (log && window.EventSource) {
    log.scrollTop = log.scrollHeight;
    const stamp = (t) => new Date(t * 1000).toLocaleString();
    const source = new EventSource(`/admin/logs/stream?level=${encodeURIComponent(log.dataset.level)}&after=${log.dataset.last}`);
    const live = document.getElementById("live");
    source.onmessage = (e) => {
      const l = JSON.parse(e.data);
      const stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
      log.append(`${stamp(l.time)} ${l.level.padEnd(7)} ${l.message}\n`);
      if (stick) log.scrollTop = log.scrollHeight;
    };
    source.onerror = () => { if (live) live.textContent = "reconnecting…"; };
    source.onopen = () => { if (live) live.textContent = "live"; };
  }
  const pairings = document.getElementById("pairings");
  if (pairings) {
    setInterval(() => fetch("/admin/api/state", { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((state) => {
        const typing = document.activeElement && document.activeElement.tagName === "INPUT";
        if (String(state.waiting) !== pairings.dataset.waiting && !typing) location.reload();
      })
      .catch(() => {}), 3000);
  }
  document.addEventListener("click", (e) => {
    const ask = e.target.closest && e.target.closest("[data-confirm]");
    if (ask && !confirm(ask.dataset.confirm)) e.preventDefault();
  });
})();
