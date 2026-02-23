const CACHE_NAME = "finance-ai-v2";

const STATIC_ASSETS = [
  "/static/vendor/chart.umd.min.js"
];

// Instalação
self.addEventListener("install", event => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      return cache.addAll(STATIC_ASSETS);
    })
  );
});

// Ativação
self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys.map(key => {
          if (key !== CACHE_NAME) {
            return caches.delete(key);
          }
        })
      )
    )
  );
  self.clients.claim();
});

// Fetch
self.addEventListener("fetch", event => {
  const url = new URL(event.request.url);

  // ❌ Nunca cacheia API
  if (url.pathname.startsWith("/ultimos") ||
      url.pathname.startsWith("/resumo") ||
      url.pathname.startsWith("/lancar") ||
      url.pathname.startsWith("/login") ||
      url.pathname.startsWith("/logout") ||
      url.pathname.startsWith("/me")) {
    return;
  }

  // ❌ Nunca cacheia HTML
  if (event.request.mode === "navigate") {
    return;
  }

  // ✅ Cache só para estáticos
  event.respondWith(
    caches.match(event.request).then(response => {
      return response || fetch(event.request);
    })
  );
});
