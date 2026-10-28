self.addEventListener('push', function (event) {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = { body: event.data ? event.data.text() : '' }; }
  const title = data.title || 'VigilCam';
  const options = {
    body: data.body || data.message || '',
    icon: data.icon || '/static/icon.png',
    badge: data.badge || '/static/icon.png',
    data: data.url || data.direct_url || '/'
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  const url = (event.notification.data && event.notification.data) || '/';
  event.waitUntil(clients.openWindow(url));
});