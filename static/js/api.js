/* ============================================================
   LlamaMonitor — API wrapper（Phase 15, spec §43/§44）
   全应用唯一 fetch 入口：api.get / api.post / api.put。
   - AbortController 超时（默认 30s；spec §44：不能无限挂）
   - HTTP 非 2xx -> ApiError{status, body}
   - JSON parse 失败 -> ApiError{parse:true}
   - 网络错误/超时 -> ApiError{network:true}
   所有轮询的"保留上次数据"策略在调用方处理（catch 后忽略），
   这里只负责把失败变成一致的错误对象（spec §99：无 unhandled promise——
   调用方必须 catch 或 .catch，审计时检查）。
   ============================================================ */
(function () {
  "use strict";

  var DEFAULT_TIMEOUT_MS = 30000;

  function ApiError(url, info) {
    var e = new Error(info.message || url);
    e.name = "ApiError";
    e.url = url;
    e.status = info.status;
    e.body = info.body;
    e.network = !!info.network;
    e.parse = !!info.parse;
    return e;
  }

  async function request(method, url, body, timeoutMs) {
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, timeoutMs || DEFAULT_TIMEOUT_MS);
    try {
      var opts = { signal: ctrl.signal };
      if (method !== "GET") {
        opts.method = method;
        opts.headers = { "Content-Type": "application/json" };
        opts.body = JSON.stringify(body === undefined ? {} : body);
      }
      var res;
      try {
        res = await fetch(url, opts);
      } catch (e) {
        var timedOut = e && e.name === "AbortError";
        throw ApiError(url, { network: true, message: timedOut ? "timeout after " + (timeoutMs || DEFAULT_TIMEOUT_MS) + "ms" : "network error" });
      }
      var data = null;
      var text;
      try {
        text = await res.text();
        data = text ? JSON.parse(text) : null;
      } catch (e) {
        if (!res.ok) {
          throw ApiError(url, { status: res.status, message: "HTTP " + res.status, body: { raw: text } });
        }
        throw ApiError(url, { status: res.status, parse: true, message: "invalid JSON" });
      }
      if (!res.ok) {
        throw ApiError(url, { status: res.status, message: "HTTP " + res.status, body: data });
      }
      return data;
    } finally {
      clearTimeout(timer);
    }
  }

  window.LM = window.LM || {};
  LM.api = {
    ApiError: ApiError,
    get: function (url, timeoutMs) { return request("GET", url, undefined, timeoutMs); },
    post: function (url, body, timeoutMs) { return request("POST", url, body, timeoutMs); },
    put: function (url, body, timeoutMs) { return request("PUT", url, body, timeoutMs); },
  };
})();
