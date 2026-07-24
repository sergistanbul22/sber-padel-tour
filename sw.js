const CACHE_NAME = 'sberpadel-v1';
const urlsToCache = [
  '/sber-padel-tour/',
  '/sber-padel-tour/sber-padel-tour.html'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(urlsToCache))
      .catch(err => console.log('[SW] Cache install error:', err))
  );
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(cacheNames => {
      return Promise.all(
        cacheNames
          .filter(name => name !== CACHE_NAME)
          .map(name => caches.delete(name))
      );
    })
  );
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  event.respondWith(
    caches.match(event.request)
      .then(response => {
        if (response) return response;
        return fetch(event.request).catch(() => {
          // Если оффлайн и запрос к странице — показываем кэш
          if (event.request.mode === 'navigate') {
            return caches.match('/sber-padel-tour/');
          }
        });
      })
  );
});
