/* sw.js - Finance AI (v3)
   - HTML (navegação) => Network First + fallback offline
   - /static/*        => Stale-While-Revalidate
   - cache Chart.js local (sem CDN)
*/

const CACHE_VERSION = "v3";
const CACHE_NAME = `financeai-${CACHE_VERSION}`;

// Ajuste se seu Chart local tiver outro nome/caminho:
const CHART_LOCAL_PATHS = [
  "/static/vendor/chart.umd.min.js",
  "/static/vendor/chart.min.js",
  "/static/chart.min.js",
];

// ✅ O "mínimo" pro app abrir offline.
// Repare que eu uso /offline.html como fallback consistente.
const CORE_ASSETS = [
  "/",
  "/offline.html", // ✅ fallback offline do HTML
  "/static/manifest.json",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  ...CHART_LOCAL_PATHS, // ✅ tenta cachear Chart local também
];

// -------------------- INSTALL --------------------
self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE_NAME);

      // Tenta adicionar tudo. Se algum Chart não existir, não quebra a instalação.
      await cache.addAll(
        CORE_ASSETS.filter((p) => !CHART_LOCAL_PATHS.includes(p))
      );

      // Chart: tenta um por um (porque pode não existir todos)
      for (const p of CHART_LOCAL_PATHS) {
        try {
          await cache.add(p);
        } catch (e) {
          // ignora se não existir esse arquivo específico
        }
      }
    })()
  );

  self.skipWaiting();
});

// -------------------- ACTIVATE --------------------
self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys.map((k) => (k !== CACHE_NAME ? caches.delete(k) : null))
      );

      await self.clients.claim();
    })()
  );
});

// -------------------- FETCH --------------------
self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;

  const url = new URL(event.request.url);

  // Só controla requests do mesmo domínio
  if (url.origin !== self.location.origin) {
    // Para terceiros (se existir), tenta rede, se falhar usa cache
    event.respondWith(fetch(event.request).catch(() => caches.match(event.request)));
    return;
  }

  // ✅ 1) Navegação/HTML -> Network First + fallback offline
  if (event.request.mode === "navigate") {
    event.respondWith(networkFirstHTML(event.request));
    return;
  }

  // ✅ 2) Arquivos estáticos -> Stale While Revalidate
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(staleWhileRevalidate(event.request));
    return;
  }

  // ✅ 3) Default -> Network First com fallback de cache
  event.respondWith(networkFirst(event.request));
});

// -------------------- Strategies --------------------
async function networkFirstHTML(request) {
  try {
    const fresh = await fetch(request);

    // Cacheia também a página navegada (ex.: "/")
    const cache = await caches.open(CACHE_NAME);
    cache.put(request, fresh.clone());

    return fresh;
  } catch (e) {
    // 1) tenta cache do próprio request
    const cached = await caches.match(request);
    if (cached) return cached;

    // 2) fallback offline (HTML)
    // Use /offline.html para evitar depender de cachear "/" corretamente.
    const offline = await caches.match("/offline.html");
    if (offline) return offline;

    // 3) último fallback: tenta "/"
    const home = await caches.match("/");
    if (home) return home;

    throw e;
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
    throw e;
  }
}

async function staleWhileRevalidate(request) {
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(request);

  const fetchPromise = fetch(request)
    .then((fresh) => {
      cache.put(request, fresh.clone());
      return fresh;
    })
    .catch(() => null);

  return cached || (await fetchPromise) || new Response("", { status: 504 });
}
