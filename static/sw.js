const VERSION = "v2.0.0"; // troque quando fizer update
const STATIC_CACHE = `static-${VERSION}`;
const RUNTIME_CACHE = `runtime-${VERSION}`;

const PRECACHE = [
  "/",                              // index
  "/static/manifest.json",
  "/static/vendor/chart.umd.min.js",

  // ícones principais
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/maskable-192.png",
  "/static/icons/maskable-512.png",
];

// INSTALL
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then(cache => cache.addAll(PRECACHE))
  );
  self.skipWaiting();
});

// ACTIVATE
self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(
      keys
        .filter(k => ![STATIC_CACHE, RUNTIME_CACHE].includes(k))
        .map(k => caches.delete(k))
    );
    await self.clients.claim();
  })());
});

// FETCH
self.addEventListener("fetch", (event) => {
  const req = event.request;

  if (req.method !== "GET") return;

  const url = new URL(req.url);

  // Só mesma origem
  if (url.origin !== self.location.origin) return;

  // ÍCONES → cache first
  if (url.pathname.startsWith("/static/icons/")) {
    event.respondWith(cacheFirst(req));
    return;
  }

  // /static → stale while revalidate
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(staleWhileRevalidate(req));
    return;
  }

  // Navegação → network first
  if (req.mode === "navigate") {
    event.respondWith(networkFirst(req));
  }
});


// =====================
// Estratégias
// =====================

async function cacheFirst(request) {
  const cache = await caches.open(RUNTIME_CACHE);
  const cached = await cache.match(request);
  if (cached) return cached;

  const fresh = await fetch(request);
  if (fresh.ok) cache.put(request, fresh.clone());
  return fresh;
}

async function staleWhileRevalidate(request) {
  const cache = await caches.open(STATIC_CACHE);
  const cached = await cache.match(request);

  const fetchPromise = fetch(request).then(response => {
    if (response.ok) cache.put(request, response.clone());
    return response;
  });

  return cached || fetchPromise;
}

async function networkFirst(request) {
  const cache = await caches.open(RUNTIME_CACHE);
  try {
    const fresh = await fetch(request);
    if (fresh.ok) cache.put(request, fresh.clone());
    return fresh;
  } catch {
    const cached = await cache.match(request);
    return cached || new Response("Offline", { status: 503 });
  }
}
