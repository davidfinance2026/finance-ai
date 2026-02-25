// Finance AI - Service Worker (v3) - avoids caching HTML
const VERSION = "v3.0.0";
const CACHE_NAME = `finance-ai-${VERSION}`;

const CORE_ASSETS = [
  "/static/manifest.json",
  "/static/sw.js",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(CORE_ASSETS)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.map((k) => (k !== CACHE_NAME ? caches.delete(k) : Promise.resolve())))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);

  if (url.origin !== self.location.origin) return;

  // Network-first for navigations (HTML)
  if (req.mode === "navigate") {
    event.respondWith(fetch(req));
    return;
  }

  // Network-first for API
  if (url.pathname.startsWith("/api/")) {
    event.respondWith(fetch(req));
    return;
  }

  // Cache-first for static assets
  event.respondWith(
    caches.match(req).then((cached) => {
      if (cached) return cached;
      return fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(CACHE_NAME).then((cache) => {
          if (req.method === "GET" && res.ok) cache.put(req, copy);
        });
        return res;
      });
    })
  );
});
