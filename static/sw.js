const CACHE_NAME = "financeai-v2"; // 🔁 troque a versão quando atualizar

// O mínimo pra app abrir mesmo offline
const CORE_ASSETS = [
  "/",
  "/static/manifest.json",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",

  // ✅ Chart.js local (offline real)
  "/static/vendor/chart.umd.min.js",
  // opcional (se você subir o .map também)
  // "/static/vendor/chart.umd.min.js.map",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(CORE_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.map((k) => (k !== CACHE_NAME ? caches.delete(k) : null)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;

  const url = new URL(event.request.url);

  // Só controla requests do mesmo domínio
  if (url.origin !== self.location.origin) {
    // Para CDNs externas (caso você use alguma), tenta rede, se falhar tenta cache
    event.respondWith(fetch(event.request).catch(() => caches.match(event.request)));
    return;
  }

  // 1) HTML "/" e navegação -> Network First (sempre atualiza após deploy)
  if (url.pathname === "/" || event.request.mode === "navigate") {
    event.respondWith(networkFirst(event.request));
    return;
  }

  // 2) Arquivos /static -> Stale While Revalidate (rápido e atualiza)
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(staleWhileRevalidate(event.request));
    return;
  }

  // 3) Default -> Network First com fallback
  event.respondWith(networkFirst(event.request));
});

// ---------- Estratégias ----------

async function networkFirst(request) {
  try {
    const fresh = await fetch(request);
    const cache = await caches.open(CACHE_NAME);
    cache.put(request, fresh.clone());
    return fresh;
  } catch (e) {
    const cached = await caches.match(request);
    if (cached) return cached;

    // fallback do HTML para "/"
    if (request.mode === "navigate") {
      const fallback = await caches.match("/");
      if (fallback) return fallback;
    }
    throw e;
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
