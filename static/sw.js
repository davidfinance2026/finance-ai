/* static/sw.js */
'use strict';

const VERSION = 'v1.0.0';
const CACHE_NAME = `finance-ai-${VERSION}`;

const CORE_ASSETS = [
  '/',               // index
  '/offline.html',   // offline fallback
  '/static/manifest.json',
  '/static/vendor/chart.umd.min.js',
  // se você tiver ícones, adicione aqui:
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    await cache.addAll(CORE_ASSETS);
    self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.map((k) => (k !== CACHE_NAME ? caches.delete(k) : null)));
    await self.clients.claim();
  })());
});

// Permite o app mandar "SKIP_WAITING"
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});

function isApiRequest(url) {
  // não cachear endpoints dinâmicos
  return (
    url.pathname.startsWith('/login') ||
    url.pathname.startsWith('/logout') ||
    url.pathname.startsWith('/me') ||
    url.pathname.startsWith('/lancar') ||
    url.pathname.startsWith('/ultimos') ||
    url.pathname.startsWith('/resumo') ||
    url.pathname.startsWith('/dashboard') ||
    url.pathname.startsWith('/metas') ||
    url.pathname.startsWith('/lancamento/') ||
    url.pathname.startsWith('/export.')
  );
}

// Network-first para navegação; cache-first para assets
self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // só mesma origem
  if (url.origin !== self.location.origin) return;

  // não interferir em API
  if (isApiRequest(url)) return;

  // Navegação (abrir a página): network-first com fallback offline
  if (req.mode === 'navigate') {
    event.respondWith((async () => {
      try {
        const fresh = await fetch(req);
        const cache = await caches.open(CACHE_NAME);
        cache.put(req, fresh.clone()).catch(() => {});
        return fresh;
      } catch (e) {
        const cache = await caches.open(CACHE_NAME);
        return (await cache.match(req)) || (await cache.match('/offline.html'));
      }
    })());
    return;
  }

  // Assets: cache-first
  event.respondWith((async () => {
    const cache = await caches.open(CACHE_NAME);
    const cached = await cache.match(req);
    if (cached) return cached;

    try {
      const fresh = await fetch(req);
      // cacheia somente GET e respostas ok
      if (req.method === 'GET' && fresh && fresh.ok) {
        cache.put(req, fresh.clone()).catch(() => {});
      }
      return fresh;
    } catch (e) {
      // fallback se for html/asset
      if (req.headers.get('accept')?.includes('text/html')) {
        return (await cache.match('/offline.html')) || Response.error();
      }
      return Response.error();
    }
  })());
});
