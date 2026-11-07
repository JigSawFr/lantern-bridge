// SPDX-FileCopyrightText: 2026 JigSawFr
// SPDX-License-Identifier: MIT
// The automatic theme: the page is dark until this runs, then it follows the browser's own preference, now and
// whenever that changes. Loaded in the head, without defer, so the first paint is already the right one.
(() => {
  const root = document.documentElement;
  if (root.dataset.mode !== "auto" || !window.matchMedia) return;
  const light = window.matchMedia("(prefers-color-scheme: light)");
  const apply = () => { root.dataset.theme = light.matches ? "light" : "dark"; };
  apply();
  light.addEventListener("change", apply);
})();
