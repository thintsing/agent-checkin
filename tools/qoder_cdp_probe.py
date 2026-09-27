r"""Qoder 客户端 CDP 侦察工具（只读为主，--click 时才派发一次鼠标事件）。

用途：验证 Qoder CN 桌面端能否被 CDP 接管，以及「用量面板 / 领取入口」是否存在于当前 UI。
背景与踩坑记录见 .workbuddy/memory/2026-09-25.md 与技能 electron-app-cdp-attach-windows。

前置（关键）：Qoder 必须以调试端口启动，且必须落在交互式桌面。
    Start-Process 直接启动会静默失败（主进程在 Chromium 初始化前退出，只剩 native-messaging-host）。
    可行方式 = 计划任务（-LogonType Interactive）：

    $exe = (Get-ChildItem "$env:LOCALAPPDATA\Programs\Qoder CN\.qoder-versions\*\Qoder CN.exe" |
            Sort-Object FullName -Descending | Select-Object -First 1).FullName
    $a = New-ScheduledTaskAction -Execute $exe -Argument "--remote-debugging-port=9335"
    $p = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName QoderCDP -Action $a -Principal $p -Settings (New-ScheduledTaskSettingsSet) -Force
    Start-ScheduledTask -TaskName QoderCDP

验证端口真的开了：看 %APPDATA%\<dataDirectoryName>\DevToolsActivePort
（Qoder CN 的 dataDirectoryName = com.qodercn.app.stable，见 resources/product.json）

用法：
    python tools/qoder_cdp_probe.py              # 只读侦察
    python tools/qoder_cdp_probe.py --click      # 额外用真实鼠标事件点一次左下角用户菜单
"""
import argparse
import json
import os
import time
import urllib.request

import websocket


def _recipe_debug_port(default: int = 9335) -> int:
    """端口以 recipes/qoder.yaml 的 client.debug_port 为单一事实源（同 win_shortcut_args.py）。"""
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "recipes", "qoder.yaml")
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

MAIN_URL_PREFIX = "qoder-cn-app://renderer"

SCAN_JS = r"""
(() => {
  const out = {url: location.href, vw: innerWidth, vh: innerHeight};
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const cn = el => { const c = el.className; return typeof c === 'string' ? c : (c && c.baseVal) || ''; };
  const box = el => { const r = el.getBoundingClientRect(); return {
      tag: el.tagName, cls: cn(el).slice(0, 56),
      aria: el.getAttribute('aria-label'), title: el.getAttribute('title'),
      text: ((el.innerText || '') + '').trim().slice(0, 56).replace(/\n/g, '|'),
      x: Math.round(r.left), y: Math.round(r.top),
      w: Math.round(r.width), h: Math.round(r.height) }; };

  const KWS = ['用量', '积分', 'credits', 'Credits', '额度', '订阅', '套餐', '升级',
               '会员', '奖励', 'reward', '领取', '礼物'];
  out.kwText = [...document.querySelectorAll('*')]
    .filter(el => { if (!vis(el)) return false;
      const t = ((el.innerText || '') + '').trim();
      return t && t.length < 60 && KWS.some(k => t.includes(k)); })
    .slice(0, 30).map(box);

  out.iframes = [...document.querySelectorAll('iframe')]
    .map(f => ({src: (f.src || '').slice(0, 150), w: f.clientWidth, h: f.clientHeight}));

  // 左下角账号入口（官方口径的「用量面板」就在这一带）
  out.userEntry = [...document.querySelectorAll('[aria-label]')]
    .filter(el => vis(el) && /用户菜单|账户|账号|设置/.test(el.getAttribute('aria-label') || ''))
    .slice(0, 10).map(box);

  return out;
})()
"""

OVERLAY_JS = r"""
(() => {
  const cn = el => { const c = el.className; return typeof c === 'string' ? c : (c && c.baseVal) || ''; };
  const out = [];
  for (const el of document.querySelectorAll('*')) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const z = parseInt(cs.zIndex, 10) || 0;
    const r = el.getBoundingClientRect();
    const t = ((el.innerText || '') + '').trim();
    if (!t || r.width < 60 || r.height < 24) continue;
    const nearLeft = r.left < 320 && r.bottom > innerHeight - 420 && r.bottom <= innerHeight + 5;
    if (!(z >= 30 || nearLeft)) continue;
    out.push({z: z, cls: cn(el).slice(0, 54), text: t.slice(0, 90).replace(/\n/g, '|'),
              x: Math.round(r.left), y: Math.round(r.top),
              w: Math.round(r.width), h: Math.round(r.height)});
  }
  out.sort((a, b) => b.z - a.z);
  return out.slice(0, 20);
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


def click_user_menu(ws):
    pos = call(ws, "(() => {const b=document.querySelector('[aria-label=\"打开用户菜单\"]');"
                   "if(!b) return null; const r=b.getBoundingClientRect();"
                   "return {x:Math.round(r.left+r.width/2), y:Math.round(r.top+r.height/2)};})()", 5)
    if not pos:
        print("  未找到用户菜单入口")
        return
    x, y = pos["x"], pos["y"]
    print(f"  入口中心 = ({x},{y})")
    send(ws, "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, 6)
    time.sleep(0.2)
    send(ws, "Input.dispatchMouseEvent",
         {"type": "mousePressed", "x": x, "y": y, "button": "left", "buttons": 1, "clickCount": 1}, 7)
    time.sleep(0.12)
    send(ws, "Input.dispatchMouseEvent",
         {"type": "mouseReleased", "x": x, "y": y, "button": "left", "buttons": 0, "clickCount": 1}, 8)
    time.sleep(2.5)
    print("\n  浮层内容：")
    for it in call(ws, OVERLAY_JS, 9) or []:
        print(f"    z={it['z']:<4} {it['x']},{it['y']} {it['w']}x{it['h']}  {it['text']!r}")
    # ESC 收起
    for t in ("keyDown", "keyUp"):
        send(ws, "Input.dispatchKeyEvent",
             {"type": t, "key": "Escape", "code": "Escape",
              "windowsVirtualKeyCode": 27, "nativeVirtualKeyCode": 27}, 10)
        time.sleep(0.05)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--click", action="store_true", help="额外点一次左下角用户菜单")
    args = ap.parse_args()

    print("=" * 62)
    print("步骤 1：调试端口")
    print("=" * 62)
    try:
        v = http_json("/json/version")
        print(f"  OK  {v.get('Browser')}")
    except Exception as e:
        print(f"  FAIL  {type(e).__name__}: {e}")
        print("  => 客户端没带 --remote-debugging-port 启动（注意：必须用交互式桌面方式拉起）")
        return 2

    print("\n" + "=" * 62)
    print("步骤 2：target 清单")
    print("=" * 62)
    ts = http_json("/json")
    for t in ts:
        print(f"  [{t.get('type'):<8}] {(t.get('url') or '')[:80]}")

    pages = [t for t in ts if t.get("type") == "page"
             and (t.get("url") or "").startswith(MAIN_URL_PREFIX)]
    if not pages:
        print("\n  未找到主渲染进程")
        return 3

    ws = websocket.create_connection(pages[0]["webSocketDebuggerUrl"], timeout=20,
                                     suppress_origin=True)
    print("\n" + "=" * 62)
    print("步骤 3：DOM 侦察")
    print("=" * 62)
    info = call(ws, SCAN_JS)
    print(f"  {info['vw']}x{info['vh']}")

    print(f"\n  关键词命中（{len(info['kwText'])}）：")
    for it in info["kwText"]:
        print(f"    <{it['tag']}> {it['x']},{it['y']}  {it['text']!r}")

    print(f"\n  账号/设置类入口（{len(info['userEntry'])}）：")
    for it in info["userEntry"]:
        print(f"    <{it['tag']}> {it['x']},{it['y']} {it['w']}x{it['h']}  "
              f"aria={it['aria']!r} text={it['text']!r}")

    print(f"\n  iframe（{len(info['iframes'])}）：")
    for f in info["iframes"]:
        print(f"    {f['w']}x{f['h']}  {f['src']}")

    if args.click:
        print("\n" + "=" * 62)
        print("步骤 4：点击左下角用户菜单（真实鼠标事件）")
        print("=" * 62)
        click_user_menu(ws)

    ws.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
