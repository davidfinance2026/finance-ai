const CACHE_NAME = "financeai-v3";     // 🔁 troque a versão quando atualizar
const CDN_CACHE = "financeai-cdn-v3";  // cache separado pra libs externas

// O mínimo pra app abrir mesmo offline
const CORE_ASSETS = [
  "/",
  "/static/manifest.json",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

// Rotas que NÃO devem ser cacheadas (API / endpoints dinâmicos)
const API_PREFIXES = [
  "/me",
  "/login",
  "/logout",
  "/create_user",
  "/admin/create_user",
  "/lancar",
  "/lancamento/",
  "/ultimos",
  "/resumo",
  "/export.csv",
  "/export.pdf",
  "/health",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(CORE_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    // limpa caches antigos
    const keys = await caches.keys();
    await Promise.all(
      keys.map((k) => {
        if (k !== CACHE_NAME && k !== CDN_CACHE) return caches.delete(k);
        return null;
      })
    );

    // melhora navegação (quando suportado)
    if (self.registration.navigationPreload) {
      await self.registration.navigationPreload.enable();
    }

    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  const req = event.request;

  // não mexe com métodos que não são GET
  if (req.method !== "GET") return;

  const url = new URL(req.url);

  // 0) Nunca intercepta API/dinâmico (evita cache de sessão/dados)
  if (url.origin === self.location.origin && isApiLike(url.pathname)) {
    event.respondWith(fetch(req));
    return;
  }

  // 1) Navegação/HTML -> Network First + fallback offline "/"
  if (req.mode === "navigate" || url.pathname === "/") {
    event.respondWith(networkFirstHtml(req, event));
    return;
  }

  // 2) /static -> Stale While Revalidate (rápido e atualiza)
  if (url.origin === self.location.origin && url.pathname.startsWith("/static/")) {
    event.respondWith(staleWhileRevalidate(req, CACHE_NAME));
    return;
  }

  // 3) CDNs (Chart.js etc) -> Cache First (pra não quebrar offline)
  if (url.origin !== self.location.origin && isCdn(url)) {
    event.respondWith(cacheFirst(req, CDN_CACHE));
    return;
  }

  // 4) Default -> Network First com fallback ao cache
  event.respondWith(networkFirst(req, CACHE_NAME));
});

// ---------- Helpers ----------

function isApiLike(pathname) {
  // prefixos diretos
  for (const p of API_PREFIXES) {
    if (p.endsWith("/") && pathname.startsWith(p)) return true;
    if (!p.endsWith("/") && pathname === p) return true;
  }
  return false;
}

function isCdn(url) {
  // adicione/remova domínios conforme seu uso
  return (
    url.hostname.includes("cdn.jsdelivr.net") ||
    url.hostname.includes("unpkg.com") ||
    url.hostname.includes("cdnjs.cloudflare.com")
  );
}

// ---------- Estratégias ----------

async function networkFirstHtml(request, event) {
  // tenta usar navigation preload quando disponível
  try {
    const preload = event.preloadResponse ? await event.preloadResponse : null;
    if (preload) {
      const cache = await caches.open(CACHE_NAME);
      cache.put(request, preload.clone());
      return preload;
    }
  } catch (_) {}

  try {
    const fresh = await fetch(request);
    const cache = await caches.open(CACHE_NAME);
    cache.put(request, fresh.clone());
    return fresh;
  } catch (e) {
    // fallback para "/" (app shell)
    const cachedRoot = await caches.match("/");
    if (cachedRoot) return cachedRoot;

    // último fallback: HTML simples offline
    return new Response(
      `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
      <title>Offline</title></head><body style="font-family:system-ui;background:#0b1020;color:#eaf0ff;padding:18px">
      <h2>Você está offline</h2><p>Conecte-se à internet e tente novamente.</p></body></html>`,
      { headers: { "Content-Type": "text/html; charset=utf-8" }, status: 200 }
    );
  }
}

async function networkFirst(request, cacheName) {
  try {
    const fresh = await fetch(request);
    const cache = await caches.open(cacheName);
    cache.put(request, fresh.clone());
    return fresh;
  } catch (e) {
    const cached = await caches.match(request);
    if (cached) return cached;
    return new Response("", { status: 504 });
  }
}

async function staleWhileRevalidate(request, cacheName) {
  const cached = await caches.match(request);

  const fetchPromise = fetch(request)
    .then(async (fresh) => {
      const cache = await caches.open(cacheName);
      cache.put(request, fresh.clone());
      return fresh;
    })
    .catch(() => null);

  return cached || (await fetchPromise) || new Response("", { status: 504 });
}

async function cacheFirst(request, cacheName) {
  const cached = await caches.match(request);
  if (cached) return cached;

  try {
    const fresh = await fetch(request, { cache: "no-store" });
    const cache = await caches.open(cacheName);
    cache.put(request, fresh.clone());
    return fresh;
  } catch (e) {
    return new Response("", { status: 504 });
  }
}
