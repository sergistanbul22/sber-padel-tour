// Отключение legacy service worker (09.11.2026).
// Августовский sw.js кэшировал оболочку приложения cache-first, из-за чего
// часть устройств неделями получала старую версию сайта. Этот файл — финал:
// при первом же заходе SW снимает регистрацию с себя и чистит все свои кэши,
// после чего сайт работает как обычная страница (HTTP-кэш max-age=600).
self.addEventListener('install', event => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', event => {
  event.waitUntil(
    Promise.all([
      self.registration.unregister(),
      caches.keys().then(names => Promise.all(names.map(n => caches.delete(n))))
    ]).then(() => self.clients.matchAll())
      .then(clients => clients.forEach(c => c.navigate(c.url)))
  );
});

// Пока SW жив (до перезахода), ничего не перехватываем — прозрачный проход.
self.addEventListener('fetch', () => {});
