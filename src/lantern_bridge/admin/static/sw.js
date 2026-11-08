// The service worker of the administration pages. It makes the pages installable and shows a notification when a
// page asks it to; it keeps nothing: the pages are private and are always read from the bridge.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (event) => {
  if (event.request.mode !== "navigate") return;
  // Offline: one plain line instead of the browser's error page. Nothing is cached.
  event.respondWith(
    fetch(event.request).catch(
      () => new Response("The bridge does not answer.", { status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" } }),
    ),
  );
});
// A message from the bridge through the browser's push service: shown as an alert. A page that is in front already
// says it by itself (and a notification of the same tag replaces the one of the page, so a hidden tab is not told twice).
self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (_) { /* a message that is not ours says nothing */ }
  const url = typeof data.url === "string" && data.url.startsWith("/admin/") ? data.url : "/admin/";
  event.waitUntil(
    self.clients.matchAll({ type: "window" }).then((all) => {
      if (all.some((client) => client.visibilityState === "visible")) return undefined;
      return self.registration.showNotification(String(data.title || "Lantern Bridge"), {
        body: String(data.body || ""), tag: typeof data.tag === "string" ? data.tag : undefined,
        icon: "/admin/static/icon-192.png", data: { url },
      });
    }),
  );
});
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = event.notification.data && event.notification.data.url ? event.notification.data.url : "/admin/";
  event.waitUntil(
    self.clients.matchAll({ type: "window" }).then((all) => {
      for (const client of all) if ("focus" in client) { client.navigate(target); return client.focus(); }
      return self.clients.openWindow(target);
    }),
  );
});
