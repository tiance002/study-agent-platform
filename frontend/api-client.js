/* Browser API and request identity utilities. */
(function () {
  "use strict";

  class ApiError extends Error {
    constructor(status, payload) {
      super(payload && payload.message ? payload.message : "请求未完成");
      this.status = status;
      this.payload = payload || {};
    }
  }

  const writeMethods = new Set(["POST", "PUT", "PATCH", "DELETE"]);
  const requestKey = (scope) => {
    const safeScope = String(scope).replace(/[^A-Za-z0-9._-]/g, "-").slice(0, 80) || "web";
    return `${safeScope}-${crypto.randomUUID()}`;
  };
  const runStorageKey = (principalId, projectId, conversationId) =>
    `study:last-run:${principalId}:${projectId}:${conversationId}`;

  function savedSelection(principalId) {
    try {
      return JSON.parse(localStorage.getItem(`study:selection:${principalId}`)) || {};
    } catch (_) {
      return {};
    }
  }

  async function api(method, path, body, options) {
    const settings = options || {};
    const headers = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (writeMethods.has(method) && settings.idempotent !== false) {
      headers["Idempotency-Key"] = settings.key || requestKey("web");
    }
    const response = await fetch(path, {
      method,
      credentials: "same-origin",
      headers,
      signal: settings.signal,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await response.text();
    let payload = null;
    try {
      payload = text ? JSON.parse(text) : null;
    } catch (error) {
      payload = { message: text || "服务器返回了无法读取的响应" };
    }
    if (response.status === 401) {
      window.dispatchEvent(new Event("study:session-expired"));
    }
    if (!response.ok) throw new ApiError(response.status, payload);
    return payload;
  }

  window.StudyApi = { ApiError, requestKey, runStorageKey, savedSelection, api };
})();
