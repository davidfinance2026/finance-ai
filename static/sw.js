/* finance-ai SW v4
   - HTML: network-first (com fallback offline)
   - Estáticos (css/js/icons/chart): stale-while-revalidate
   - Fallback: index.html e offline.html no cache
*/

const VERSION = "v4";
const CACHE_HTML = `html-${VERSION}`;
const CACHE_STATIC = `static-${VERSION}`;

const OFFLINE_URL = "/offline.html"; // rota do seu Flask (render_template)
const INDEX_URL = "/";

// ajuste se você tiver mais libs
const STATIC_EXT = [
  ".js", ".css", ".png", ".jpg", ".jpeg", ".svg", ".webp", ".ico",
  ".woff", ".woff2", ".ttf", ".eot", ".json"
];

function isHTMLRequest(request) {
  const accept = request.headers.get("accept") || "";
  return request.mode === "navigate" || accept.includes("text/html");
}

function isStaticRequest(url) {
  // tudo em /static/ é estático
  if (url.pathname.startsWith("/static/")) return true;

  // também considere extensões
  return STATIC_EXT.some(ext => url.pathname.endsWith(ext));
}

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const htmlCache = await caches.open(CACHE_HTML);
    // cacheia HTMLs essenciais para fallback offline
    await htmlCache.addAll([INDEX_URL, OFFLINE_URL]);

    // pré-cache do chart local (se existir)
    const staticCache = await caches.open(CACHE_STATIC);
    await staticCache.addAll([
      "/static/vendor/chart.umd.min.js",
      "/static/manifest.json",
      // se quiser garantir ícones principais:
      // "/static/icons/icon-192.png",
      // "/static/icons/icon-512.png",
    ].filter(Boolean));

    self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(
      keys
        .filter(k => (k.startsWith("html-") && k !== CACHE_HTML) || (k.startsWith("static-") && k !== CACHE_STATIC))
        .map(k => caches.delete(k))
    );
    await self.clients.claim();
  })());
});

self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "SKIP_WAITING") {
    self.skipWaiting();
  }
});

async function networkFirstHTML(request) {
  const cache = await caches.open(CACHE_HTML);

  try {
    const fresh = await fetch(request);
    // só cacheia respostas ok
    if (fresh && fresh.ok) cache.put(request, fresh.clone());
    return fresh;
  } catch (err) {
    // tenta cache do próprio request
    const cached = await cache.match(request);
    if (cached) return cached;

    // fallback offline.html
    const offline = await cache.match(OFFLINE_URL);
    if (offline) return offline;

    // último fallback: index.html
    const index = await cache.match(INDEX_URL);
    if (index) return index;

    return new Response("Offline", { status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" } });
  }
}

async function staleWhileRevalidate(request) {
  const cache = await caches.open(CACHE_STATIC);
  const cached = await cache.match(request);

  const fetchPromise = fetch(request)
    .then((fresh) => {
      if (fresh && fresh.ok) cache.put(request, fresh.clone());
      return fresh;
    })
    .catch(() => null);

  // se tiver cache, devolve cache imediatamente e atualiza em background
  if (cached) {
    eventWait(fetchPromise);
    return cached;
  }

  // se não tiver cache, espera a rede
  const fresh = await fetchPromise;
  if (fresh) return fresh;

  // se falhou tudo
  return new Response("Offline asset", { status: 503 });
}

// helper para não quebrar quando não temos event aqui
function eventWait(promise) {
  // no-op: o navegador ainda vai rodar o fetch
  // (mantém simples e estável)
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);

  // só controla o mesmo domínio
  if (url.origin !== self.location.origin) return;

  // HTML: network-first (com fallback offline)
  if (isHTMLRequest(req)) {
    event.respondWith(networkFirstHTML(req));
    return;
  }

  // Estáticos: stale-while-revalidate (inclui /static/vendor/chart...)
  if (isStaticRequest(url)) {
    event.respondWith((async () => {
      const cache = await caches.open(CACHE_STATIC);
      const cached = await cache.match(req);

      const fetchPromise = fetch(req)
        .then((fresh) => {
          if (fresh && fresh.ok) cache.put(req, fresh.clone());
          return fresh;
        })
        .catch(() => null);

      if (cached) {
        // atualiza em background
        fetchPromise.catch(() => {});
        return cached;
      }

      const fresh = await fetchPromise;
      if (fresh) return fresh;

      return new Response("Offline asset", { status: 503 });
    })());
    return;
  }

  // API (ex: /resumo, /ultimos...) -> network-only (ou você pode adaptar)
  event.respondWith(fetch(req).catch(() => new Response(JSON.stringify({ ok:false, msg:"Offline" }), {
    status: 503,
    headers: { "Content-Type": "application/json; charset=utf-8" }
  })));
});
