const CACHE_NAME = "financeai-v3";

const CORE_ASSETS = [
  "/",                         // index.html
  "/static/manifest.json",
  "/static/vendor/chart.umd.min.js",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png"
];

// ================= INSTALL =================
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(CORE_ASSETS);
    })
  );
  self.skipWaiting();
});

// ================= ACTIVATE =================
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.map((k) => (k !== CACHE_NAME ? caches.delete(k) : null))
      )
    )
  );
  self.clients.claim();
});

// ================= FETCH =================
self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;

  const url = new URL(event.request.url);

  // Só intercepta mesmo domínio
  if (url.origin !== self.location.origin) return;

  // HTML (navegação) → NETWORK FIRST
  if (event.request.mode === "navigate") {
    event.respondWith(networkFirst(event.request));
    return;
  }

  // Arquivos estáticos → STALE WHILE REVALIDATE
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(staleWhileRevalidate(event.request));
    return;
  }

  // Default → network-first
  event.respondWith(networkFirst(event.request));
});

// ================= ESTRATÉGIAS =================

async function networkFirst(request) {
  try {
    const fresh = await fetch(request);
    const cache = await caches.open(CACHE_NAME);
    cache.put(request, fresh.clone());
    return fresh;
  } catch (err) {
    const cached = await caches.match(request);
    if (cached) return cached;

    // fallback para index.html offline
    if (request.mode === "navigate") {
      return caches.match("/");
    }

    throw err;
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

  return cached || (await fetchPromise);
}
