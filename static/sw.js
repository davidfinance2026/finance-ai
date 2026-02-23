/* static/sw.js */
const CACHE_NAME = "financeai-v3"; // 🔁 troque a versão quando atualizar

// ✅ arquivos essenciais para abrir offline
const CORE_ASSETS = [
  "/",                    // index (fallback p/ cache)
  "/offline.html",        // fallback offline real
  "/static/manifest.json",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",

  // ✅ Chart local (sem CDN)
  "/static/vendor/chart.umd.min.js",
];

// -------------------------
// INSTALL
// -------------------------
self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE_NAME);
      await cache.addAll(CORE_ASSETS);
      await self.skipWaiting();
    })()
  );
});

// -------------------------
// ACTIVATE
// -------------------------
self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(keys.map((k) => (k !== CACHE_NAME ? caches.delete(k) : null)));
      await self.clients.claim();
    })()
  );
});

// -------------------------
// FETCH
// -------------------------
self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;

  const url = new URL(event.request.url);

  // Só controla requests do mesmo domínio
  if (url.origin !== self.location.origin) return;

  // ✅ 1) Navegação/HTML -> Network First com fallback offline.html
  // (serve pra "/" e pra qualquer rota que o Flask renderiza)
  if (event.request.mode === "navigate") {
    event.respondWith(networkFirstHTML(event.request));
    return;
  }

  // ✅ 2) /static -> Stale While Revalidate
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(staleWhileRevalidate(event.request));
    return;
  }

  // ✅ 3) Default -> Network First com fallback de cache
  event.respondWith(networkFirst(event.request));
});

// -------------------------
// Estratégias
// -------------------------

// HTML: tenta rede, salva cache e se falhar cai no /offline.html
async function networkFirstHTML(request) {
  try {
    const fresh = await fetch(request);
    const cache = await caches.open(CACHE_NAME);
    cache.put(request, fresh.clone());

    // ✅ também garante que "/" fica sempre cacheado como fallback
    if (new URL(request.url).pathname === "/") {
      cache.put("/", fresh.clone());
    }
    return fresh;
  } catch (e) {
    // tenta cache da própria rota
    const cached = await caches.match(request);
    if (cached) return cached;

    // tenta fallback do index "/"
    const cachedIndex = await caches.match("/");
    if (cachedIndex) return cachedIndex;

    // fallback offline real
    const offline = await caches.match("/offline.html");
    if (offline) return offline;

    return new Response("Offline", { status: 503, headers: { "Content-Type": "text/plain" } });
  }
}

// Default: tenta rede, se falhar usa cache
async function networkFirst(request) {
  try {
    const fresh = await fetch(request);
    const cache = await caches.open(CACHE_NAME);
    cache.put(request, fresh.clone());
    return fresh;
  } catch (e) {
    const cached = await caches.match(request);
    if (cached) return cached;
    return new Response("", { status: 504 });
  }
}

// Static: responde cache se existir e atualiza em background
async function staleWhileRevalidate(request) {
  const cached = await caches.match(request);

  const fetchPromise = fetch(request)
    .then(async (fresh) => {
      const cache = await caches.open(CACHE_NAME);
      cache.put(request, fresh.clone());
      return fresh;
    })
    .catch(() => null);

  return cached || (await fetchPromise) || new Response("", { status: 504 });
}
