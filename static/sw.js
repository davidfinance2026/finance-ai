const CACHE_NAME = "financeai-v3"; // 🔁 troque a versão quando atualizar

// ✅ Essenciais para abrir offline (fallback do index + assets principais)
const CORE_ASSETS = [
  "/", // index.html (fallback offline)
  "/static/manifest.json",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/vendor/chart.umd.min.js", // ✅ Chart.js local (sem CDN)
  "/static/sw.js" // opcional, mas ajuda a manter atualizado no cache
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE_NAME);
      // addAll garante que tudo do CORE seja pré-cacheado
      await cache.addAll(CORE_ASSETS);
      self.skipWaiting();
    })()
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      // limpa caches antigos
      const keys = await caches.keys();
      await Promise.all(keys.map((k) => (k !== CACHE_NAME ? caches.delete(k) : null)));
      self.clients.claim();
    })()
  );
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;

  const url = new URL(event.request.url);

  // ✅ Só controla o mesmo domínio
  if (url.origin !== self.location.origin) {
    // Externo (se existir): tenta rede, se falhar tenta cache.
    event.respondWith(fetch(event.request).catch(() => caches.match(event.request)));
    return;
  }

  // ✅ 1) Navegação/HTML -> Network First + fallback "/" offline
  // - request.mode === "navigate" cobre mudanças de rota/refresh
  // - url.pathname === "/" garante a home
  if (event.request.mode === "navigate" || url.pathname === "/") {
    event.respondWith(networkFirstHtml(event.request));
    return;
  }

  // ✅ 2) /static -> Stale While Revalidate
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(staleWhileRevalidate(event.request));
    return;
  }

  // ✅ 3) Qualquer outra coisa -> Network First com fallback cache
  event.respondWith(networkFirst(event.request));
});

// -----------------------
// Estratégias
// -----------------------

async function networkFirstHtml(request) {
  try {
    // tenta pegar a versão mais nova do HTML
    const fresh = await fetch(request);

    // guarda no cache (para fallback offline)
    const cache = await caches.open(CACHE_NAME);
    cache.put(request, fresh.clone());

    return fresh;
  } catch (e) {
    // offline: tenta o que estiver cacheado
    const cached = await caches.match(request);
    if (cached) return cached;

    // fallback final: devolve o "/" pré-cacheado (index)
    const fallback = await caches.match("/");
    if (fallback) return fallback;

    // sem fallback disponível
    return new Response("Offline", {
      status: 503,
      headers: { "Content-Type": "text/plain; charset=utf-8" }
    });
  }
}

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
