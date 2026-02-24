// static/sw.js
const VERSION = "v1.0.0"; // troque quando publicar update
const STATIC_CACHE = `static-${VERSION}`;
const RUNTIME_CACHE = `runtime-${VERSION}`;

const PRECACHE_URLS = [
  "/",                       // se sua rota raiz serve o index.html
  "/static/manifest.json",
  "/static/vendor/chart.umd.min.js",

  // ÍCONES (melhor listar explicitamente)
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/maskable-192.png",
  "/static/icons/maskable-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then((cache) => cache.addAll(PRECACHE_URLS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(
      keys
        .filter((k) => ![STATIC_CACHE, RUNTIME_CACHE].includes(k))
        .map((k) => caches.delete(k))
    );
    await self.clients.claim();
  })());
});

// Estratégias simples:
// - /static/icons/*  -> cache-first (rápido e estável)
// - /static/*        -> stale-while-revalidate (pega cache e atualiza por trás)
// - requests do app  -> network-first (pra não ficar "travado" offline sem querer)
self.addEventListener("fetch", (event) => {
  const req = event.request;

  // só GET
  if (req.method !== "GET") return;

  const url = new URL(req.url);

  // mesma origem
  if (url.origin !== self.location.origin) return;

  // ÍCONES: cache-first
  if (url.pathname.startsWith("/static/icons/")) {
    event.respondWith(cacheFirst(req, RUNTIME_CACHE));
    return;
  }

  // vendor (ou qualquer /static): stale-while-revalidate
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(staleWhileRevalidate(req, STATIC_CACHE));
    return;
  }

  // navegação/páginas: network-first com fallback pro cache (se existir)
  if (req.mode === "navigate") {
    event.respondWith(networkFirst(req, RUNTIME_CACHE));
    return;
  }
});

// ---------- helpers ----------
async function cacheFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(request);
  if (cached) return cached;

  const fresh = await fetch(request);
  if (fresh && fresh.ok) cache.put(request, fresh.clone());
  return fresh;
}

async function staleWhileRevalidate(request, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(request);

  const fetchPromise = fetch(request)
    .then((fresh) => {
      if (fresh && fresh.ok) cache.put(request, fresh.clone());
      return fresh;
    })
    .catch(() => null);

  return cached || (await fetchPromise) || new Response("", { status: 504 });
}

async function networkFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const fresh = await fetch(request);
    if (fresh && fresh.ok) cache.put(request, fresh.clone());
    return fresh;
  } catch (e) {
    const cached = await cache.match(request);
    return cached || new Response("Offline", { status: 503 });
  }
}

self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "SKIP_WAITING") self.skipWaiting();
});
