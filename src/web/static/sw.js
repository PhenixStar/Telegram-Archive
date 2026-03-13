/**
 * Service Worker for Telegram Archive Web Push Notifications + Offline Caching.
 *
 * Handles:
 * - Receiving push messages from the server
 * - Displaying notifications to the user
 * - Handling notification clicks (opening the relevant chat)
 * - App shell caching for offline viewing
 * - API stale-while-revalidate for recent chat data
 * - Media cache-first (immutable files)
 */

const CACHE_NAME      = 'telegram-archive-v2';
const APP_SHELL_CACHE = 'tg-shell-v1';
const API_CACHE       = 'tg-api-v1';
const MEDIA_CACHE     = 'tg-media-v1';

const KNOWN_CACHES = [CACHE_NAME, APP_SHELL_CACHE, API_CACHE, MEDIA_CACHE];

// App shell assets to pre-cache on install
const APP_SHELL_URLS = [
    '/',
    '/static/manifest.json',
    '/static/favicon.ico'
];

const API_CACHE_LIMIT   = 50;
const MEDIA_CACHE_LIMIT = 200;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Remove oldest entries from a cache if it exceeds `limit`. */
async function trimCache(cacheName, limit) {
    const cache = await caches.open(cacheName);
    const keys  = await cache.keys();
    if (keys.length > limit) {
        const excess = keys.slice(0, keys.length - limit);
        await Promise.all(excess.map((key) => cache.delete(key)));
    }
}

// ---------------------------------------------------------------------------
// Install — pre-cache app shell
// ---------------------------------------------------------------------------

self.addEventListener('install', (event) => {
    console.log('[SW] Installing service worker v2');
    event.waitUntil(
        caches.open(APP_SHELL_CACHE).then((cache) => {
            return cache.addAll(APP_SHELL_URLS);
        }).then(() => {
            // Activate immediately without waiting for old SW to be released
            self.skipWaiting();
        }).catch((err) => {
            console.error('[SW] App shell pre-cache failed:', err);
            // Still skip waiting even if pre-cache fails
            self.skipWaiting();
        })
    );
});

// ---------------------------------------------------------------------------
// Activate — delete stale caches
// ---------------------------------------------------------------------------

self.addEventListener('activate', (event) => {
    console.log('[SW] Activating service worker v2');
    event.waitUntil(
        caches.keys().then((cacheNames) => {
            return Promise.all(
                cacheNames
                    .filter((name) => !KNOWN_CACHES.includes(name))
                    .map((name) => {
                        console.log('[SW] Deleting old cache:', name);
                        return caches.delete(name);
                    })
            );
        }).then(() => self.clients.claim())
    );
});

// ---------------------------------------------------------------------------
// Fetch — routing strategies
// ---------------------------------------------------------------------------

self.addEventListener('fetch', (event) => {
    const { request } = event;
    const url = new URL(request.url);

    // Only handle same-origin requests (skip CDN / external)
    if (url.origin !== self.location.origin) {
        return; // network-only, browser handles it
    }

    // Navigation requests (HTML pages) — network-first, fallback to shell cache
    if (request.mode === 'navigate') {
        event.respondWith(networkFirstNavigate(request));
        return;
    }

    // API: chats list and messages — stale-while-revalidate
    if (isApiCacheable(url.pathname)) {
        event.respondWith(staleWhileRevalidate(request, API_CACHE, API_CACHE_LIMIT));
        return;
    }

    // Media files — cache-first (immutable content)
    if (url.pathname.startsWith('/media/')) {
        event.respondWith(cacheFirst(request, MEDIA_CACHE, MEDIA_CACHE_LIMIT));
        return;
    }

    // Everything else — network-only (static assets served by FastAPI, etc.)
});

/** Returns true for API paths worth caching for offline use. */
function isApiCacheable(pathname) {
    if (pathname === '/api/chats') return true;
    // /api/chats/<id>/messages
    if (/^\/api\/chats\/[^/]+\/messages/.test(pathname)) return true;
    return false;
}

// ---------------------------------------------------------------------------
// Strategy: network-first for navigation
// ---------------------------------------------------------------------------

async function networkFirstNavigate(request) {
    try {
        const networkResponse = await fetch(request);
        // Store successful response in shell cache for offline fallback
        if (networkResponse.ok) {
            const cache = await caches.open(APP_SHELL_CACHE);
            cache.put(request, networkResponse.clone());
        }
        return networkResponse;
    } catch (_err) {
        // Offline — serve cached shell
        const cached = await caches.match(request, { cacheName: APP_SHELL_CACHE })
            || await caches.match('/', { cacheName: APP_SHELL_CACHE });
        if (cached) return cached;
        // Last resort: bare offline response
        return new Response('<h1>Offline</h1><p>Please reconnect to view new messages.</p>', {
            headers: { 'Content-Type': 'text/html' }
        });
    }
}

// ---------------------------------------------------------------------------
// Strategy: stale-while-revalidate
// ---------------------------------------------------------------------------

async function staleWhileRevalidate(request, cacheName, limit) {
    const cache  = await caches.open(cacheName);
    const cached = await cache.match(request);

    // Fire network request in background regardless
    const fetchPromise = fetch(request).then((networkResponse) => {
        if (networkResponse.ok) {
            cache.put(request, networkResponse.clone());
            trimCache(cacheName, limit);
        }
        return networkResponse;
    }).catch((err) => {
        console.warn('[SW] SWR network error:', err);
        return null;
    });

    // Return cached version immediately if available, otherwise wait for network
    return cached || fetchPromise;
}

// ---------------------------------------------------------------------------
// Strategy: cache-first
// ---------------------------------------------------------------------------

async function cacheFirst(request, cacheName, limit) {
    const cache  = await caches.open(cacheName);
    const cached = await cache.match(request);
    if (cached) return cached;

    try {
        const networkResponse = await fetch(request);
        if (networkResponse.ok) {
            cache.put(request, networkResponse.clone());
            trimCache(cacheName, limit);
        }
        return networkResponse;
    } catch (err) {
        console.warn('[SW] Cache-first network error:', err);
        return new Response('', { status: 503, statusText: 'Offline' });
    }
}

// ---------------------------------------------------------------------------
// Push notification handlers (unchanged)
// ---------------------------------------------------------------------------

self.addEventListener('push', (event) => {
    console.log('[SW] Push received');

    let payload = {
        title: 'Telegram Archive',
        body: 'New message received',
        icon: '/static/favicon.ico',
        badge: '/static/favicon.ico',
        tag: 'telegram-archive',
        data: {}
    };

    try {
        if (event.data) {
            const data = event.data.json();
            payload = {
                title: data.title || payload.title,
                body: data.body || payload.body,
                icon: data.icon || payload.icon,
                badge: payload.badge,
                tag: data.tag || payload.tag,
                data: data.data || {},
                timestamp: data.timestamp ? new Date(data.timestamp).getTime() : Date.now(),
                requireInteraction: false,
                renotify: true,
                silent: false
            };
        }
    } catch (e) {
        console.error('[SW] Failed to parse push payload:', e);
        if (event.data) {
            payload.body = event.data.text();
        }
    }

    const options = {
        body: payload.body,
        icon: payload.icon,
        badge: payload.badge,
        tag: payload.tag,
        data: payload.data,
        timestamp: payload.timestamp,
        requireInteraction: payload.requireInteraction,
        renotify: payload.renotify,
        silent: payload.silent,
        vibrate: [200, 100, 200]
    };

    event.waitUntil(
        self.registration.showNotification(payload.title, options)
    );
});

// Notification click event - handle user clicking on notification
self.addEventListener('notificationclick', (event) => {
    console.log('[SW] Notification clicked');

    const notification = event.notification;
    const data = notification.data || {};

    notification.close();

    // Determine the URL to open
    let url = '/';
    if (data.url) {
        url = data.url;
    } else if (data.chat_id) {
        url = `/?chat=${data.chat_id}`;
        if (data.message_id) {
            url += `&msg=${data.message_id}`;
        }
    }

    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true })
            .then((windowClients) => {
                // Check if there's already a window open
                for (const client of windowClients) {
                    if ('focus' in client) {
                        return client.focus().then(() => {
                            if (client.url !== url && 'navigate' in client) {
                                return client.navigate(url);
                            }
                            // Post message to the client to navigate/highlight
                            client.postMessage({
                                type: 'NOTIFICATION_CLICK',
                                data: data
                            });
                        });
                    }
                }
                // No existing window, open a new one
                if (clients.openWindow) {
                    return clients.openWindow(url);
                }
            })
    );
});

// Handle notification close
self.addEventListener('notificationclose', (event) => {
    console.log('[SW] Notification closed');
});

// Handle push subscription expiry/renewal (auto-resubscribe)
self.addEventListener('pushsubscriptionchange', (event) => {
    console.log('[SW] Push subscription changed, re-subscribing...');
    event.waitUntil(
        self.registration.pushManager.subscribe(
            event.oldSubscription ? event.oldSubscription.options : { userVisibleOnly: true }
        ).then((newSub) => {
            return fetch('/api/push/subscribe', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify(newSub.toJSON())
            });
        }).then((response) => {
            if (response.ok) {
                console.log('[SW] Re-subscribed after subscription change');
            } else {
                console.error('[SW] Re-subscribe failed:', response.status);
            }
        }).catch((err) => {
            console.error('[SW] Re-subscribe error:', err);
        })
    );
});

// Handle messages from the main page
self.addEventListener('message', (event) => {
    console.log('[SW] Message received:', event.data);

    if (event.data && event.data.type === 'SKIP_WAITING') {
        self.skipWaiting();
    }
});
