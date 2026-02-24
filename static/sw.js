/* static/sw.js
   Estratégias:
   - HTML (navegação): NETWORK-FIRST com fallback para cache do index
   - Static (css/js/img/icons): STALE-WHILE-REVALIDATE
   - Chart local: cache (stale-while-revalidate)
*/

const VERSION = "fa-v1.0.0";
const STATIC_CACHE = `fa-static-${VERSION}`;
const HTML_CACHE = `fa-html-${VERSION}`;

const OFFLINE_FALLBACK_URL = "/"; // fallback offline: index.html (rota "/")

// Arquivos essenciais para iniciar offline
const PRECACHE = [
  OFFLINE_FALLBACK_URL,
  "/static/manifest.json",
  "/static/vendor/chart.umd.min.js",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const staticCache = await caches.open(STATIC_CACHE);
    await staticCache.addAll(PRECACHE);
    self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(
      keys.map((k) => {
        if (k !== STATIC_CACHE && k !== HTML_CACHE) return caches.delete(k);
      })
    );
    await self.clients.claim();
  })());
});

self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "SKIP_WAITING") self.skipWaiting();
});

function isNavigationRequest(request) {
  return request.mode === "navigate" ||
    (request.method === "GET" && request.headers.get("accept") && request.headers.get("accept").includes("text/html"));
}

function isStaticAsset(url) {
  return (
    url.pathname.startsWith("/static/") ||
    url.pathname.endsWith(".js") ||
    url.pathname.endsWith(".css") ||
    url.pathname.endsWith(".png") ||
    url.pathname.endsWith(".jpg") ||
    url.pathname.endsWith(".jpeg") ||
    url.pathname.endsWith(".webp") ||
    url.pathname.endsWith(".svg") ||
    url.pathname.endsWith(".ico")
  );
}

async function networkFirst(request) {
  const cache = await caches.open(HTML_CACHE);
  try {
    const response = await fetch(request);
    // cacheia apenas respostas OK e do mesmo origin
    if (response && response.ok) {
      cache.put(request, response.clone());
      // também cacheia o fallback "/" quando navegar
      if (new URL(request.url).pathname === "/") {
        cache.put(OFFLINE_FALLBACK_URL, response.clone());
      }
    }
    return response;
  } catch (err) {
    // fallback: tenta cache da página solicitada, senão "/", senão qualquer HTML cacheado
    const cached = await cache.match(request);
    if (cached) return cached;

    const cachedHome = await cache.match(OFFLINE_FALLBACK_URL);
    if (cachedHome) return cachedHome;

    const staticCache = await caches.open(STATIC_CACHE);
    const cachedStaticHome = await staticCache.match(OFFLINE_FALLBACK_URL);
    if (cachedStaticHome) return cachedStaticHome;

    return new Response("Offline", { status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" } });
  }
}

async function staleWhileRevalidate(request) {
  const cache = await caches.open(STATIC_CACHE);
  const cached = await cache.match(request);
  const fetchPromise = fetch(request)
    .then((response) => {
      if (response && response.ok) cache.put(request, response.clone());
      return response;
    })
    .catch(() => null);

  return cached || (await fetchPromise) || new Response("", { status: 504 });
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);

  if (url.origin !== self.location.origin) return;

  // HTML: network-first
  if (isNavigationRequest(req)) {
    event.respondWith(networkFirst(req));
    return;
  }

  // Static: SWR
  if (isStaticAsset(url)) {
    event.respondWith(staleWhileRevalidate(req));
    return;
  }
});
