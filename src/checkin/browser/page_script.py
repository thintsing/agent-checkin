"""注入页面上下文的 JS 模板：在已登录网页里探测 token、按业务 code 调状态/签到接口。
token 全程留在浏览器内，脚本只拿回脱敏后的判定信息。"""
from __future__ import annotations

from typing import Optional

_JS = r"""
(async () => {
  const MODE = "__MODE__";
  const STATUS_PATH = "__STATUS_PATH__";
  const STATUS_METHOD = "__STATUS_METHOD__";
  const STATUS_BODY = "__STATUS_BODY__";
  const TRIGGER_PATH = "__TRIGGER_PATH__";
  const TRIGGER_METHOD = "__TRIGGER_METHOD__";
  const TRIGGER_BODY = "__TRIGGER_BODY__";
  const TOKEN_KEY = "__TOKEN_KEY__";
  const CHECKED_IN_FLAG = "__CHECKED_IN_FLAG__";

  const redact = v => (v && v.length > 12) ? (v.slice(0, 6) + "***" + v.slice(-4)) : "***";

  function tokenCandidates() {
    const out = [];
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      let v = ""; try { v = localStorage.getItem(k) || ""; } catch (e) {}
      if (v && v.length > 16 && /token|jwt|access|auth/i.test(k) && !/^\s*(function|\{)/.test(v))
        out.push({ k, len: v.length });
    }
    return out;
  }

  function getToken() {
    if (TOKEN_KEY && localStorage.getItem(TOKEN_KEY)) return { key: TOKEN_KEY, val: localStorage.getItem(TOKEN_KEY) };
    const c = tokenCandidates();
    if (c.length) return { key: c[0].k, val: localStorage.getItem(c[0].k) };
    return null;
  }

  // 只回传响应体 data 里的标量字段（bool / number / 短字符串），
  // 用来判断"今天到底领没领"。不回传嵌套结构，避免把无关内容带出页面。
  function extractFlags(parsed) {
    const d = parsed && parsed.data;
    if (!d || typeof d !== "object") return null;
    const out = {};
    for (const k of Object.keys(d)) {
      const v = d[k];
      if (typeof v === "boolean" || typeof v === "number") out[k] = v;
      else if (typeof v === "string" && v.length <= 40) out[k] = v;
      if (Object.keys(out).length >= 20) break;
    }
    return out;
  }

  async function call(path, method, body) {
    const t = getToken();
    const headers = { "Accept": "application/json" };
    // 带 body 就必须声明 Content-Type：fetch 默认发 text/plain;charset=UTF-8，
    // 部分网关不按 JSON 解析甚至直接拒掉。踩过这个坑，别删。
    if (body) headers["Content-Type"] = "application/json";
    if (t) headers["Authorization"] = "Bearer " + t.val;
    let resp;
    try {
      resp = await fetch(path, {
        method, headers, credentials: "include",
        body: (method === "GET" || method === "HEAD") ? undefined : (body || "{}"),
      });
    } catch (e) { return { netError: String(e), tokenKey: t ? t.key : null }; }
    const ct = resp.headers.get("content-type") || "";
    let parsed = null, raw = null;
    if (ct.includes("json")) { try { parsed = await resp.json(); } catch (e) { raw = "invalid-json"; } }
    else { raw = (await resp.text()).slice(0, 160); }
    return {
      http: resp.status,
      code: parsed && (parsed.code !== undefined ? parsed.code : (parsed.data && parsed.data.code)),
      message: parsed && (parsed.message || parsed.msg),
      flags: extractFlags(parsed),
      body: parsed !== null ? parsed : raw,
      tokenKey: t ? t.key : null,
      tokenRedacted: t ? redact(t.val) : null,
    };
  }

  const diag = { href: location.href, tokenCandidates: tokenCandidates() };
  if (MODE === "probe") {
    diag.statusCall = await call(STATUS_PATH, STATUS_METHOD, STATUS_BODY);
    return diag;
  }

  // trigger 模式：先查状态。若服务端已声明"今日已签"，直接收工 ——
  // 少发一次写请求，就少一分被风控盯上的理由，也避免把自己统计成异常高频。
  diag.statusCall = await call(STATUS_PATH, STATUS_METHOD, STATUS_BODY);
  const flags = diag.statusCall && diag.statusCall.flags;
  if (CHECKED_IN_FLAG && flags && flags[CHECKED_IN_FLAG] === true) {
    diag.skippedBecauseCheckedIn = true;
    return diag;
  }

  diag.triggerCall = await call(TRIGGER_PATH, TRIGGER_METHOD, TRIGGER_BODY);
  return diag;
})()
"""


def build(
    mode: str,
    status: Optional[tuple],
    trigger: Optional[tuple],
    token_key: str = "",
    checked_in_flag: str = "",
) -> str:
    """status/trigger 为 (method, path, body) 三元组，可为 None。

    安全要点：所有注入值都以 json.dumps 编码成 **JS 字符串字面量**（含引号），
    而不是裸替换进模板。否则 body 里的 `"` / `\\` 会把 JS 字符串提前闭合，
    生成语法错误的脚本 —— 例如 body={"a":1} 直接拼进双引号里就变成
    `const TRIGGER_BODY = "{"a": 1}";`，浏览器一解析就抛异常。
    """
    import json as _json

    def unpack(a):
        if not a:
            return ("GET", "", {})
        method, path, body = a
        return (method or "GET", path or "", body if body is not None else {})

    def body_text(b) -> str:
        if b is None:
            return "{}"
        return _json.dumps(b, ensure_ascii=False) if isinstance(b, (dict, list)) else str(b)

    sm, sp, sb = unpack(status)
    tm, tp, tb = unpack(trigger)

    def js_literal(value: str) -> str:
        return _json.dumps(str(value), ensure_ascii=False)

    replacements = (
        ("__MODE__", mode),
        ("__STATUS_PATH__", sp),
        ("__STATUS_METHOD__", sm),
        ("__STATUS_BODY__", body_text(sb)),
        ("__TRIGGER_PATH__", tp),
        ("__TRIGGER_METHOD__", tm),
        ("__TRIGGER_BODY__", body_text(tb)),
        ("__TOKEN_KEY__", token_key or ""),
        ("__CHECKED_IN_FLAG__", checked_in_flag or ""),
    )

    js = _JS
    for placeholder, value in replacements:
        # 模板里占位符是带引号的（"__MODE__"），连引号一起替换成合法字面量
        js = js.replace(f'"{placeholder}"', js_literal(value))
    return js
