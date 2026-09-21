/* FinClue 金线索 · service worker
 *
 * 策略：
 *  - 页面导航（.html / navigate）：network-first —— 日报每 3 天更新，
 *    必须让最新一期先出来；断网时退回缓存，最后退回 index.html。
 *  - 静态资源（icons / manifest）：cache-first。
 *
 * 版本号：改了静态资源清单（ASSETS）时要手动 +1，否则老缓存不清。
 */
var CACHE = "finclue-v1";
var ASSETS = [
  "./",
  "./index.html",
  "./manifest.webmanifest",
  "./icons/icon-192.png",
  "./icons/icon-512.png",
  "./icons/icon-maskable-512.png",
  "./icons/apple-touch-icon.png",
  "./icons/favicon-32.png"
];

self.addEventListener("install", function (e) {
  e.waitUntil(
    caches.open(CACHE)
      .then(function (c) { return c.addAll(ASSETS); })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (e) {
  e.waitUntil(
    caches.keys()
      .then(function (keys) {
        return Promise.all(keys.filter(function (k) { return k !== CACHE; })
          .map(function (k) { return caches.delete(k); }));
      })
      .then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (e) {
  if (e.request.method !== "GET") return;
  var url = new URL(e.request.url);
  if (url.origin !== location.origin) return;

  var isNav = e.request.mode === "navigate" || url.pathname.endsWith(".html");
  if (isNav) {
    e.respondWith(
      fetch(e.request).then(function (resp) {
        var copy = resp.clone();
        caches.open(CACHE).then(function (c) { c.put(e.request, copy); });
        return resp;
      }).catch(function () {
        return caches.match(e.request)
          .then(function (m) { return m || caches.match("./index.html"); });
      })
    );
  } else {
    e.respondWith(
      caches.match(e.request).then(function (m) {
        return m || fetch(e.request).then(function (resp) {
          var copy = resp.clone();
          caches.open(CACHE).then(function (c) { c.put(e.request, copy); });
          return resp;
        });
      })
    );
  }
});
