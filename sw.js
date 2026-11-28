// LUMEN service worker: keeps the site usable offline without serving a stale
// codec. Network-first everywhere; the cache is only a fallback.
const VERSION = "lumen-v3";
const SHELL = [
  "./",
  "./index.html",
  "./manifest.webmanifest",
  "./web/site.css",
  "./web/site.js",
  "./web/lum.js",
  "./web/lum-worker.js",
  "./web/lum.wasm",
  "./web/lum-viewer.html",
  "./web/icon.svg",
  "./web/icon-192.png",
  "./web/icon-512.png",
  "./docs/assets/formats/png.png",
  "./docs/assets/formats/ornek.lum",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(VERSION)
      .then((cache) => cache.addAll(SHELL).catch(() => undefined))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  event.respondWith(
    fetch(req)
      .then((response) => {
        if (response && response.ok) {
          const copy = response.clone();
          caches.open(VERSION).then((cache) => cache.put(req, copy)).catch(() => undefined);
        }
        return response;
      })
      .catch(async () => {
        const cached = await caches.match(req);
        if (cached) return cached;
        if (req.mode === "navigate") {
          const shell = await caches.match("./index.html");
          if (shell) return shell;
        }
        return new Response("Çevrimdışı ve bu istek önbellekte yok.", {
          status: 503,
          headers: { "content-type": "text/plain; charset=utf-8" },
        });
      }),
  );
});
