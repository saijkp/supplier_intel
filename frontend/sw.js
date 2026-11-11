/* Bump this version whenever the static shell changes. Never cache private data. */
const CACHE_VERSION = 'supplier-intel-shell-v3';
const SHELL = ['./index.html', './customers.js', './manifest.webmanifest', './icons/icon-192.png', './icons/icon-512.png', './icons/maskable-512.png', './icons/apple-touch-icon.png'];
const shellURLs = new Set(SHELL.map(path => new URL(path, self.registration.scope).href));
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE_VERSION).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith('supplier-intel-shell-') && key !== CACHE_VERSION).map(key => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  const request = event.request;
  // Exact allowlist: API, Authorization, query tokens and other pages never enter Cache Storage.
  if (request.method !== 'GET' || request.headers.has('Authorization') || !shellURLs.has(request.url)) return;
  const url = new URL(request.url);
  const index = url.pathname.endsWith('/index.html');
  event.respondWith((async () => {
    const cache = await caches.open(CACHE_VERSION);
    if (!index) {
      const cached = await cache.match(request);
      if (cached) return cached;
    }
    try {
      const response = await fetch(request, {cache: 'no-cache'});
      if (!response.ok) throw new Error('Shell unavailable');
      await cache.put(request, response.clone());
      return response;
    } catch (error) {
      const cached = await cache.match(request);
      if (cached) return cached;
      throw error;
    }
  })());
});
