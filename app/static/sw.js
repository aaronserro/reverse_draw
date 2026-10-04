const CACHE = "reverse-draw-shell-v24";
const SHELL = [
  "/",
  "/admin",
  "/trading",
  "/trading/login",
  "/trading/dashboard",
  "/manifest.webmanifest",
  "/static/styles.css?v=24",
  "/static/campaign.css?v=24",
  "/static/common.js?v=20",
  "/static/public.js?v=23",
  "/static/admin.js?v=22",
  "/static/trading.js?v=20",
  "/static/trading-page.js?v=20",
  "/static/trading-dashboard.css?v=24",
  "/static/trading-dashboard.js?v=20",
  "/static/pwa.js?v=20",
  "/static/hoopp-logo.svg",
  "/static/hoopp-icon.svg",
  "/static/united-way-logo.png",
  "/static/app-icon.svg",
  "/static/app-icon-maskable.svg"
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.ok) {
            const cacheCopy = response.clone();
            event.waitUntil(caches.open(CACHE).then((cache) => cache.put(request, cacheCopy)));
          }
          return response;
        })
        .catch(() => caches.match(request).then((cached) => cached || caches.match("/")))
    );
    return;
  }

  if (url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.ok) {
            const cacheCopy = response.clone();
            event.waitUntil(caches.open(CACHE).then((cache) => cache.put(request, cacheCopy)));
          }
          return response;
        })
        .catch(() => caches.match(request))
    );
  }
});
