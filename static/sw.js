const VERSION = "v2.0.1"; // troque quando fizer update
const STATIC_CACHE = `static-${VERSION}`;
const RUNTIME_CACHE = `runtime-${VERSION}`;

const PRECACHE = [
  "/",                              // index
  "/static/manifest.json",
  "/static/vendor/chart.umd.min.js",

  // ícones principais (opcional mas recomendado)
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/maskable-192.png",
  "/static/icons/maskable-512.png",
];

// INSTALL (seguro: não quebra se 1 arquivo faltar)
self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(STATIC_CACHE);

    // tenta adicionar 1 por 1 (se falhar, ignora e continua)
    await Promise.all(
      PRECACHE.map(async (url) => {
        try {
          const req = new Request(url, { cache: "reload" });
          const res = await fetch(req);
          if (res.ok) await cache.put(req, res.clone());
        } catch (e) {
          // ignora falhas de precache
        }
      })
    );

    self.skipWaiting();
  })());
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

  // Navegação → network first (com fallback pro / do cache)
  if (req.mode === "navigate") {
    event.respondWith(networkFirst(req));
    return;
  }

  // ÍCONES → cache first (runtime)
  if (url.pathname.startsWith("/static/icons/")) {
    event.respondWith(cacheFirst(req));
    return;
  }

  // /static → stale while revalidate (cache de estáticos)
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(staleWhileRevalidate(req));
    return;
  }

  // demais GETs → network (sem interferir)
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
  }).catch(() => null);

  return cached || fetchPromise || new Response("", { status: 504 });
}

async function networkFirst(request) {
  const cache = await caches.open(RUNTIME_CACHE);
  try {
    const fresh = await fetch(request);
    if (fresh.ok) cache.put(request, fresh.clone());
    return fresh;
  } catch {
    // fallback: tenta voltar para o index do precache
    const cachedRoot =
      (await caches.match("/")) ||
      (await caches.match(request));

    return cachedRoot || new Response("Offline", { status: 503 });
  }
}
