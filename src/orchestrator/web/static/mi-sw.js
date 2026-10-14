// The patient app's service worker (served at /mi/sw.js, scope /mi/). It keeps only the
// app's own files, for a quick start and a page when offline. Nothing from the API is
// ever stored here: a patient's data stays on the server, not in the phone's cache.
"use strict";

const CACHE = "mi-v1";
const SHELL = ["/mi/", "/static/ui.css", "/static/mi.js", "/static/icons/app-192.png", "/static/icons/app-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))));
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin || !SHELL.includes(url.pathname)) {
    return; // the API and everything else: straight to the network, never cached
  }
  // Network first, so an update is seen at once; the copy only serves when offline.
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(event.request, copy));
        return response;
      })
      .catch(() => caches.match(event.request)),
  );
});
