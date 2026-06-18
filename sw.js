// sw.js — save this as a separate file alongside raw-material-purchasing.html
// Must be served from the SAME origin as the app (same domain/path)

self.addEventListener("push", (event) => {
  const data = event.data?.json() || {};
  const title = data.title || "Expol RM Alert";
  const options = {
    body: data.body || "",
    icon: "/icon-192.png",
    badge: "/icon-72.png",
    tag: "expol-rm-alert",       // replaces previous notification of same tag
    renotify: true,
    requireInteraction: data.title?.includes("CRITICAL"), // stay on screen for critical
    data: { url: data.url || "/" },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = event.notification.data?.url || "/";
  event.waitUntil(clients.openWindow(url));
});

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(clients.claim()));
