// Network-only: never cache private video segments, API responses, or edits.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", event => {
    if (event.request.mode === "navigate") {
        event.respondWith(fetch(event.request).catch(() => new Response(
            "<h1>NeoClip is offline</h1><p>Reconnect to your server, then reload. Videos are not stored offline.</p>",
            {status: 503, headers: {"Content-Type": "text/html"}}
        )));
    }
});
