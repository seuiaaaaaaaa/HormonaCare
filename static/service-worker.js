const CACHE_NAME = "hormonacare-static-v49";
const PAGE_CACHE_NAME = "hormonacare-pages-v49";
const API_CACHE_NAME = "hormonacare-api-v49";
const STATIC_VERSION = "20260520-offline-page-api-cache";
const OFFLINE_URL = "/offline";
const PAGE_FALLBACK_URLS = [
    "/dashboard",
    "/",
    "/alerts",
    "/medications",
    "/lifestyle",
    "/mental-health",
    "/calendar",
    "/appointments",
];
const STATIC_ASSETS = [
    OFFLINE_URL,
    `/static/css/style.css?v=${STATIC_VERSION}`,
    `/static/js/app.js?v=${STATIC_VERSION}`,
    "/static/css/style.css",
    "/static/js/app.js",
    "/static/manifest.json",
    "/static/icons/icon-192.png",
    "/static/icons/icon-512.png",
    "/static/icons/apple-touch-icon.png",
];

self.addEventListener("install", (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS))
    );
    self.skipWaiting();
});

self.addEventListener("activate", (event) => {
    event.waitUntil(
        caches.keys().then((keys) =>
            Promise.all(
                keys
                    .filter((key) => key !== CACHE_NAME && key !== PAGE_CACHE_NAME && key !== API_CACHE_NAME)
                    .map((key) => caches.delete(key))
            )
        )
    );
    self.clients.claim();
});

self.addEventListener("fetch", (event) => {
    if (event.request.method !== "GET") {
        return;
    }
    const requestUrl = new URL(event.request.url);
    const isSameOrigin = requestUrl.origin === self.location.origin;
    const isStaticAsset = isSameOrigin && requestUrl.pathname.startsWith("/static/");
    const isApiRequest = isSameOrigin && requestUrl.pathname.startsWith("/api/");
    const acceptsHtml = event.request.headers.get("accept") || "";
    const isPageNavigation = event.request.mode === "navigate" || acceptsHtml.includes("text/html");

    if (isApiRequest) {
        event.respondWith(
            fetch(event.request).then((networkResponse) => {
                if (networkResponse && networkResponse.ok && networkResponse.type === "basic") {
                    const responseToCache = networkResponse.clone();
                    caches.open(API_CACHE_NAME).then((cache) => cache.put(event.request, responseToCache));
                }
                return networkResponse;
            }).catch(() =>
                caches.match(event.request).then((cachedResponse) => cachedResponse || new Response(
                    JSON.stringify({
                        ok: false,
                        message: "Offline and no cached API data is available yet.",
                    }),
                    {
                        status: 503,
                        headers: {
                            "Content-Type": "application/json",
                            "X-HormonaCare-Offline": "true",
                        },
                    }
                ))
            )
        );
        return;
    }

    if (isSameOrigin && isPageNavigation) {
        event.respondWith(
            fetch(event.request).then((networkResponse) => {
                if (networkResponse && networkResponse.status === 200 && networkResponse.type === "basic") {
                    const responseToCache = networkResponse.clone();
                    caches.open(PAGE_CACHE_NAME).then((cache) => cache.put(event.request, responseToCache));
                }
                return networkResponse;
            }).catch(() =>
                caches.match(event.request)
                    .then((cachedResponse) => {
                        if (cachedResponse) {
                            return cachedResponse;
                        }
                        return caches.open(PAGE_CACHE_NAME).then((cache) =>
                            PAGE_FALLBACK_URLS.reduce(
                                (promise, url) => promise.then((match) => match || cache.match(url)),
                                Promise.resolve(null)
                            )
                        );
                    })
                    .then((cachedPage) => cachedPage || caches.match(OFFLINE_URL))
            )
        );
        return;
    }

    if (!isStaticAsset) {
        return;
    }

    const isFreshAsset = requestUrl.pathname.endsWith(".css") || requestUrl.pathname.endsWith(".js");
    if (isFreshAsset) {
        event.respondWith(
            fetch(event.request).then((networkResponse) => {
                if (!networkResponse || networkResponse.status !== 200 || networkResponse.type !== "basic") {
                    return networkResponse;
                }
                const responseToCache = networkResponse.clone();
                caches.open(CACHE_NAME).then((cache) => cache.put(event.request, responseToCache));
                return networkResponse;
            }).catch(() => caches.match(event.request).then((cachedResponse) => cachedResponse || caches.match(requestUrl.pathname)))
        );
        return;
    }

    event.respondWith(
        caches.match(event.request).then((cachedResponse) => {
            if (cachedResponse) {
                return cachedResponse;
            }
            return fetch(event.request).then((networkResponse) => {
                if (!networkResponse || networkResponse.status !== 200 || networkResponse.type !== "basic") {
                    return networkResponse;
                }
                const responseToCache = networkResponse.clone();
                caches.open(CACHE_NAME).then((cache) => cache.put(event.request, responseToCache));
                return networkResponse;
            });
        })
    );
});

self.addEventListener("push", (event) => {
    const payload = (() => {
        if (!event.data) {
            return {};
        }
        try {
            return event.data.json();
        } catch (error) {
            return {
                body: event.data.text(),
            };
        }
    })();

    const title = payload.title || "HormonaCare";
    const options = {
        body: payload.body || "You have a new reminder.",
        tag: payload.tag || `hormonacare-${Date.now()}`,
        icon: payload.icon || "/static/icons/icon-192.png",
        badge: payload.badge || "/static/icons/icon-192.png",
        requireInteraction: payload.requireInteraction !== false,
        data: {
            url: payload.url || "/",
            type: payload.type || "push",
            tag: payload.tag || "",
        },
    };

    event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
    event.notification.close();

    const targetUrl = (() => {
        try {
            return new URL(event.notification.data && event.notification.data.url ? event.notification.data.url : "/", self.location.origin).href;
        } catch (error) {
            return new URL("/", self.location.origin).href;
        }
    })();

    event.waitUntil(
        self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clientList) => {
            const target = new URL(targetUrl);
            const matchingClient = clientList.find((client) => {
                try {
                    return new URL(client.url).origin === target.origin;
                } catch (error) {
                    return false;
                }
            });

            if (matchingClient) {
                if ("navigate" in matchingClient && matchingClient.url !== targetUrl) {
                    return matchingClient.navigate(targetUrl).then((client) => {
                        if (client && "focus" in client) {
                            return client.focus();
                        }
                        return undefined;
                    });
                }

                if ("focus" in matchingClient) {
                    return matchingClient.focus();
                }
            }

            if (self.clients.openWindow) {
                return self.clients.openWindow(targetUrl);
            }

            return undefined;
        })
    );
});
