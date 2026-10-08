/* Console integration: credentials stay in memory and never enter a URL. */
(function () {
  "use strict";
  var originalFetch = window.fetch.bind(window);
  var basePath = new URL(".", window.location.href).pathname;
  var parentOrigin = document.referrer ? new URL(document.referrer).origin : "";
  var resolveSession;
  var session = new Promise(function (resolve) { resolveSession = resolve; });
  window.addEventListener("message", function (event) {
    if (event.source !== window.parent || event.origin !== parentOrigin) return;
    var data = event.data;
    if (!data || data.type !== "cmc-healthcheck-session" || !data.token) return;
    if (data.mode !== "session" && data.mode !== "api-token") return;
    resolveSession(data);
  });
  window.fetch = async function (resource, options) {
    var url = new URL(resource, window.location.href);
    if (url.origin !== window.location.origin || !url.pathname.startsWith(basePath)) {
      return originalFetch(resource, options);
    }
    var auth = await session;
    var headers = new Headers(options && options.headers);
    headers.delete("X-Editor-Password");
    headers.set(auth.mode === "session" ? "Authorization" : "X-API-Token",
      auth.mode === "session" ? "Bearer " + auth.token : auth.token);
    var response = await originalFetch(resource, Object.assign({}, options, { headers: headers, cache: "no-store" }));
    if (response.status === 401) {
      window.parent.postMessage({ type: "cmc-healthcheck-expired" }, parentOrigin);
    }
    return response;
  };
  if (window.parent === window) {
    document.addEventListener("DOMContentLoaded", function () {
      document.body.textContent = "Open OCI Health Check from the Cloud Migration Console menu.";
    });
  }
})();
