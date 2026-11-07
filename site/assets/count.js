// Counts that something was done, for Umami (workbench's "Mesure d'audience" and the privacy text): the name of the
// action, never what was typed, pasted or copied. A tool counts once per visit, the Copy button every time. The
// tracker is the page's own Umami script; without it (blocked, or not on this host) nothing happens.
'use strict';

(function () {
  const counted = new Set();
  window.countEvent = (name, every) => {
    if (!every && counted.has(name)) return;
    counted.add(name);
    try { if (window.umami && typeof window.umami.track === 'function') window.umami.track(name); } catch (e) { /* counting never breaks the page */ }
  };
})();
