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
