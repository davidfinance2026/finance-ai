// Finance AI - Service Worker (simple offline cache)
const VERSION = "v1.0.0";
const CACHE_NAME = `finance-ai-${VERSION}`;

const CORE_ASSETS = [
  "/",
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

  // Only handle same-origin
  if (url.origin !== self.location.origin) return;

  // Network-first for API
  if (url.pathname.startsWith("/api/")) {
    event.respondWith(
      fetch(req)
        .then((res) => res)
        .catch(() => caches.match(req).then((c) => c || new Response(JSON.stringify({ error: "offline" }), {
          headers: { "Content-Type": "application/json; charset=utf-8" },
          status: 503
        })))
    );
    return;
  }

  // Cache-first for everything else (static/pages)
  event.respondWith(
    caches.match(req).then((cached) => {
      if (cached) return cached;
      return fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(CACHE_NAME).then((cache) => {
          // Cache successful GETs
          if (req.method === "GET" && res.ok) cache.put(req, copy);
        });
        return res;
      });
    })
  );
});
