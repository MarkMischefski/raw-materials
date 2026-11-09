// sw.js — push notification service worker for desktop.html / mobile.html
// Must be served from the SAME origin as the app (same domain/path).
// Relative URLs below resolve against this script's location (works under a GitHub Pages subpath).

self.addEventListener("push", (event) => {
  const data = event.data?.json() || {};
  const title = data.title || "Expol RM Alert";
  const options = {
    body: data.body || "",
    icon: "icon-192.png",
    badge: "icon-72.png",
    tag: "expol-rm-alert",       // replaces previous notification of same tag
    renotify: true,
    requireInteraction: data.critical ?? /critical/i.test(data.title || ""), // stay on screen for critical
    data: { url: data.url || "./" },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = event.notification.data?.url || "./";
  event.waitUntil(clients.openWindow(url));
});

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(clients.claim()));
