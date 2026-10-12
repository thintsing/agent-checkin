r"""Trae CN 客户端 CDP 侦察工具（只读为主，--click 时才派发鼠标事件）。

用途：确认 Trae CN 能否被 CDP 接管，并定位「账号菜单 → 每日签到」入口的真实 DOM。

前置：Trae 必须以调试端口（9338）启动。端口 = `recipes/trae.yaml` 的 client.debug_port。
      带端口启动 = 用**改了参数的桌面快捷方式**启动（tools/win_shortcut_args.py --app trae）。

为什么这个脚本比 qoder_cdp_probe.py 复杂
----------------------------------------
Trae 是 **VSCode 1.107 内核**，DOM 大量使用 **shadow DOM**（`.monaco-workbench` 下
到处是自定义元素的分片 shadow root）。`document.querySelectorAll()` **穿不透**它们，
直接用会得出"页面上什么都没有"的错误结论。故这里统一用 deepQuery 递归 ShadowRoot。

用法：
    python tools/trae_cdp_probe.py                # 只读侦察
    python tools/trae_cdp_probe.py --click        # 额外点一次左下角账号入口，再扫浮层
"""
import argparse
import json
import os
import time
import urllib.request

import websocket


def _recipe_debug_port(default: int = 9338) -> int:
    """端口以 recipes/trae.yaml 的 client.debug_port 为单一事实源。"""
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "recipes", "trae.yaml")
    try:
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                s = line.split("#", 1)[0].strip()
                if s.startswith("debug_port:"):
                    return int(s.split(":", 1)[1].strip())
    except Exception:
        pass
    return default


PORT = _recipe_debug_port()
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 穿透 shadow DOM 的通用探测脚本。
# 关键词覆盖：账号上下文、签到/积分上下文、VSCode 账户入口（中英）。
SCAN_JS = r"""
(() => {
  const vis = el => { try { const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0; } catch (e) { return false; } };
  const cn = el => { const c = el.className; return typeof c === 'string' ? c : (c && c.baseVal) || ''; };
  const txt = el => ((el.innerText || el.textContent || '') + '').trim();

  // 递归穿透 ShadowRoot（VSCode 的 UI 大量藏在里面，querySelectorAll 够不到）
  const deepAll = (root, sel, out, depth) => {
    out = out || []; depth = depth || 0;
    if (depth > 12) return out;
    let list = [];
    try { list = root.querySelectorAll(sel); } catch (e) { return out; }
    for (const el of list) out.push(el);
    let kids = [];
    try { kids = root.querySelectorAll('*'); } catch (e) { return out; }
    for (const el of kids) if (el.shadowRoot) deepAll(el.shadowRoot, sel, out, depth + 1);
    return out;
  };

  const box = el => { const r = el.getBoundingClientRect(); return {
      tag: el.tagName, cls: cn(el).slice(0, 48),
      aria: el.getAttribute('aria-label'), role: el.getAttribute('role'),
      text: txt(el).slice(0, 48).replace(/\n/g, '|'),
      x: Math.round(r.left), y: Math.round(r.top),
      w: Math.round(r.width), h: Math.round(r.height) }; };

  const out = {url: location.href, vw: innerWidth, vh: innerHeight};

  // 1) 所有带 aria-label 的可见元素（找账号入口 / 签到项的主要依据）
  out.aria = deepAll(document, '[aria-label]')
    .filter(el => vis(el))
    .slice(0, 200)
    .map(box);
  out.ariaCount = out.aria.length;

  // 2) 关键词命中的短文本节点
  const KWS = ['签到', '积分', 'credits', 'Credits', '额度', '奖励', '领取', '账户',
               '账号', 'Account', 'usage', 'Usage', '订阅', '升级', '会员', '今日'];
  out.kw = deepAll(document, '*')
    .filter(el => { if (!vis(el)) return false;
      const t = txt(el);
      return t && t.length < 50 && KWS.some(k => t.includes(k)); })
    .slice(0, 60).map(box);

  // 3) renderer 环境线索：能否直接触达命令系统 / require
  out.env = {
    hasRequire: typeof require,
    hasProcess: typeof process,
    vscodeGlobal: typeof window.vscode,
    monaco: !!document.querySelector('.monaco-workbench'),
    keys: Object.keys(window).filter(k => /vscode|command|icube|trae/i.test(k)).slice(0, 20)
  };

  return out;
})()
"""

OVERLAY_JS = r"""
(() => {
  const cn = el => { const c = el.className; return typeof c === 'string' ? c : (c && c.baseVal) || ''; };
  const txt = el => ((el.innerText || el.textContent || '') + '').trim();
  const deepAll = (root, sel, out, depth) => {
    out = out || []; depth = depth || 0;
    if (depth > 12) return out;
    let list = []; try { list = root.querySelectorAll(sel); } catch (e) { return out; }
    for (const el of list) out.push(el);
    let kids = []; try { kids = root.querySelectorAll('*'); } catch (e) { return out; }
    for (const el of kids) if (el.shadowRoot) deepAll(el.shadowRoot, sel, out, depth + 1);
    return out;
  };
  const out = [];
  for (const el of deepAll(document, '*')) {
    let cs; try { cs = getComputedStyle(el); } catch (e) { continue; }
    if (!cs || cs.display === 'none' || cs.visibility === 'hidden') continue;
    const z = parseInt(cs.zIndex, 10) || 0;
    const r = el.getBoundingClientRect();
    const t = txt(el);
    if (!t || r.width < 50 || r.height < 20) continue;
    // 浮层判据：高 z-index、或出现在左下角一带
    const nearLeft = r.left < 360 && r.bottom > innerHeight - 520 && r.bottom <= innerHeight + 5;
    if (!(z >= 20 || nearLeft)) continue;
    out.push({z: z, tag: el.tagName, cls: cn(el).slice(0, 46),
              text: t.slice(0, 70).replace(/\n/g, '|'),
              aria: el.getAttribute('aria-label'),
              x: Math.round(r.left), y: Math.round(r.top),
              w: Math.round(r.width), h: Math.round(r.height)});
  }
  out.sort((a, b) => b.z - a.z);
  return out.slice(0, 30);
})()
"""


def call(ws, expr, mid=1):
    ws.send(json.dumps({"id": mid, "method": "Runtime.evaluate",
                        "params": {"expression": expr, "returnByValue": True}}))
    while True:
        m = json.loads(ws.recv())
        if m.get("id") == mid:
            r = m.get("result", {})
            if r.get("exceptionDetails"):
                raise RuntimeError(r["exceptionDetails"].get("text"))
            return r.get("result", {}).get("value")


def send(ws, method, params, mid):
    ws.send(json.dumps({"id": mid, "method": method, "params": params}))
    while True:
        m = json.loads(ws.recv())
        if m.get("id") == mid:
            return "error" not in m


def http_json(path):
    with _LOCAL.open(f"http://127.0.0.1:{PORT}{path}", timeout=5) as r:
        return json.loads(r.read().decode())


def click_at(ws, x, y, mid0=200):
    """真实鼠标事件序列（Trae 的 VSCode UI 绑 pointerdown，单靠 JS click() 常无效）。"""
    send(ws, "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, mid0)
    time.sleep(0.15)
    send(ws, "Input.dispatchMouseEvent",
         {"type": "mousePressed", "x": x, "y": y, "button": "left", "buttons": 1, "clickCount": 1}, mid0 + 1)
    time.sleep(0.12)
    send(ws, "Input.dispatchMouseEvent",
         {"type": "mouseReleased", "x": x, "y": y, "button": "left", "buttons": 0, "clickCount": 1}, mid0 + 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--click", action="store_true", help="点一次左下角账号入口，再扫浮层")
    args = ap.parse_args()

    print("=" * 62)
    print("步骤 1：调试端口")
    print("=" * 62)
    try:
        v = http_json("/json/version")
        print(f"  OK  {v.get('Browser')}")
    except Exception as e:
        print(f"  FAIL  {type(e).__name__}: {e}")
        print("  => Trae 没带 --remote-debugging-port 启动。用改了参数的桌面快捷方式重启它。")
        return 2

    print("\n" + "=" * 62)
    print("步骤 2：target 清单")
    print("=" * 62)
    ts = http_json("/json")
    for t in ts:
        print(f"  [{t.get('type'):<8}] {(t.get('url') or '')[:90]}")

    pages = [t for t in ts if t.get("type") == "page"]
    if not pages:
        print("\n  未找到任何 page target")
        return 3
    # 主窗口 = 第一个 page（VSCode 的第一个 page 通常是 workbench / 空窗口）
    main_t = pages[0]
    print(f"\n  选定主 target: {main_t.get('url','')[:80]}")

    ws = websocket.create_connection(main_t["webSocketDebuggerUrl"], timeout=20,
                                     suppress_origin=True)
    print("\n" + "=" * 62)
    print("步骤 3：DOM 侦察（已穿透 ShadowRoot）")
    print("=" * 62)
    info = call(ws, SCAN_JS)
    print(f"  {info['vw']}x{info['vh']}   url={info['url'][:80]}")
    print(f"  renderer 环境: {info['env']}")

    print(f"\n  aria-label 元素（{info['ariaCount']} 个，列前 60）：")
    for it in info["aria"][:60]:
        print(f"    <{it['tag']}> {it['x']},{it['y']} {it['w']}x{it['h']}  "
              f"aria={it['aria']!r} text={it['text']!r}")

    print(f"\n  关键词命中（{len(info['kw'])}）：")
    for it in info["kw"]:
        print(f"    <{it['tag']}> {it['x']},{it['y']}  text={it['text']!r}")

    if args.click:
        print("\n" + "=" * 62)
        print("步骤 4：点左下角账号入口 → 扫浮层")
        print("=" * 62)
        # 左下角账号入口：aria 含 账户/账号/Account/管理，或位于左下角
        cand = [a for a in info["aria"]
                if a["x"] < 200 and a["y"] > info["vh"] - 120]
        if not cand:
            cand = [a for a in info["aria"]
                    if any(k in (a["aria"] or "") for k in ("账户", "账号", "Account", "用户"))]
        if not cand:
            print("  未找到候选入口（左下角或含账户关键词的 aria 元素）")
        else:
            tgt = cand[0]
            cx = tgt["x"] + tgt["w"] // 2
            cy = tgt["y"] + tgt["h"] // 2
            print(f"  点击候选: aria={tgt['aria']!r} text={tgt['text']!r} @({cx},{cy})")
            click_at(ws, cx, cy)
            time.sleep(2.5)
            print("\n  浮层内容：")
            for it in call(ws, OVERLAY_JS, 300) or []:
                print(f"    z={it['z']:<4} <{it['tag']}> {it['x']},{it['y']} "
                      f"{it['w']}x{it['h']}  aria={it['aria']!r} text={it['text']!r}")

    ws.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
