r"""一次性抓取 QQ 群的 `group_openid`（抓完就再也用不到 WS 了）。

为什么需要这一步
----------------
发群消息的接口要的是 `group_openid`，它**不是群号** —— 只能从**机器人收到群内 @消息**
的事件里取。所以流程是：运行本脚本 → 到群里 @ 一下机器人 → 脚本打印 openid 并回写配置。

为什么用"群聊"而不是私聊
------------------------
QQ 官方对**私聊主动消息**限制为**每月 4 条**，每天推必然超限；群聊无此限制（每分钟 20 条）。
所以请建一个只有你和机器人的群。

⚠ 这个脚本会**短暂占用该 AppID 的 WebSocket 连接**。如果你用的是 WorkBuddy 里
   正在使用的那同一个机器人，WorkBuddy 的 QQ 通道会在这期间掉线（断开后一般会自动重连）。
   **想完全不影响它，就去 QQ 开放平台另建一个专用机器人。**

前置
----
1. QQ 开放平台建机器人 → 拿 AppID / AppSecret；机器人状态需为**已上线**；
   开发设置里开启「**群聊消息**」权限。
2. 建一个群（只有你一人即可），把机器人拉进群；
   群设置 → 机器人 → 打开「**机器人主动在群聊内发言**」。
3. 填好 `data/notify.json` 里的 `app_id` / `app_secret`（或命令行传）。

用法
----
    .venv\Scripts\python.exe tools\qq_bot_openid.py
    # 然后到群里 @机器人 发一句话，脚本会打印 GROUP_OPENID 并回写 data/notify.json
"""
import argparse
import json
import os
import sys
import time
import urllib.request

import websocket

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
GATEWAY_URL = "https://api.sgroup.qq.com/gateway"
# 群聊 + 单聊事件（GROUP_AT_MESSAGE_CREATE 属于这一位）
INTENT_GROUP_AND_C2C = 1 << 25
OP_HELLO, OP_IDENTIFY, OP_HEARTBEAT, OP_DISPATCH = 10, 2, 1, 0


def _opener():
    # 显式绕开系统代理：本机会注入 HTTP(S)_PROXY，走它反而连不上 QQ 域名
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _request(url, payload=None, headers=None, timeout=15):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with _opener().open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def default_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_cfg(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_openid(path, cfg, openid):
    qq = dict((cfg or {}).get("qq_bot") or {})
    qq["group_openid"] = openid
    cfg = dict(cfg or {})
    cfg["qq_bot"] = qq
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    return qq


def _listen_once(ws_url, token, app_id, deadline):
    """开一次 WS 会话、等群里的 @ 事件。

    返回 `group_openid`；到点或连接被断开都返回 None。

    **为什么要能重连**（2026-10-19 实测）：同一个 AppID 只允许**一条** WS 连接。
    如果这个机器人同时在别处跑（比如 WorkBuddy 的 QQ 通道），我们一上线就会被它
    挤掉 —— `ws.recv()` 直接抛 `WebSocketConnectionClosedException`。这不是偶发，
    是**必然**，所以不能像以前那样一断就崩，必须由上层重连。
    """
    ws = websocket.create_connection(ws_url, timeout=30)
    ws.settimeout(30)
    seq = None
    last_hb = time.time()
    identified = False
    ready_n = 0
    try:
        while time.time() < deadline:
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                # 心跳：Hello 之后必须定期发，否则会被服务端断开
                if identified and time.time() - last_hb > 40:
                    ws.send(json.dumps({"op": OP_HEARTBEAT, "d": seq}))
                    last_hb = time.time()
                continue
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            op, t = msg.get("op"), msg.get("t")
            if msg.get("s") is not None:
                seq = msg.get("s")
            if op == OP_HELLO:
                ws.send(json.dumps({"op": OP_IDENTIFY, "d": {
                    "token": "QQBot %s" % token,
                    "intents": INTENT_GROUP_AND_C2C,
                    "shard": [0, 1],
                }}))
            elif op == OP_DISPATCH and t == "READY":
                identified = True
                ready_n += 1
                print("[OK] 第 %d 次连通，窗口已开 —— 就现在 @（存活通常只有几秒）" % ready_n)
            elif op == OP_DISPATCH and t == "C2C_MESSAGE_CREATE":
                # 最常见的操作失误：在**私聊**里给机器人发消息。这里拿不到群 openid，
                # 而且官方对私聊主动消息限制为每月 4 条，根本撑不住每日推送。
                print("[!] 收到的是**私聊**消息，里面没有群 openid")
                print("    => 请到**群里** @ 机器人（不是私聊窗口），并确认群里已开启")
                print("       「机器人主动在群聊内发言」")
            elif op == OP_DISPATCH and t in (
                    "GROUP_AT_MESSAGE_CREATE", "GROUP_ADD_ROBOT"):
                d = msg.get("d") or {}
                openid = str(d.get("group_openid") or "").strip()
                if not openid:
                    continue
                return openid
    except websocket.WebSocketConnectionClosedException:
        print("[!] 连接被断开（多半是这个机器人同时在别处在线，抢走了同一条 WS）")
        return None
    except websocket.WebSocketException as e:
        print("[!] WS 异常：%s" % e)
        return None
    finally:
        try:
            ws.close()
        except Exception:                                       # noqa: BLE001
            pass
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=default_root(),
                    help="项目根目录（默认 = 本脚本上一级）")
    ap.add_argument("--app-id", default="")
    ap.add_argument("--app-secret", default="")
    ap.add_argument("--timeout", type=int, default=240,
                    help="等待群内 @消息的秒数（默认 240）")
    args = ap.parse_args()

    cfg_path = os.path.join(args.root, "data", "notify.json")
    cfg = load_cfg(cfg_path)
    qq = cfg.get("qq_bot") or {}
    app_id = (args.app_id or qq.get("app_id") or "").strip()
    secret = (args.app_secret or qq.get("app_secret") or "").strip()
    if not app_id or not secret:
        print("[X] 缺少 AppID / AppSecret。")
        print("    先填 %s 的 qq_bot.app_id / app_secret，或用 --app-id / --app-secret 传入。" % cfg_path)
        return 2

    print("=" * 58)
    print("QQ group_openid 抓取")
    print("=" * 58)
    print("AppID: %s" % app_id)

    try:
        tok = _request(TOKEN_URL, {"appId": app_id, "clientSecret": secret})
        token = str((tok or {}).get("access_token") or "").strip()
    except Exception as e:                                       # noqa: BLE001
        print("[X] 取 access_token 失败：%s" % e)
        print("    => 多半是 AppSecret 不对（注意平台只在生成时显示一次，二次查看会重置）。")
        return 3
    if not token:
        print("[X] 取 access_token 失败，返回体：%s" % json.dumps(tok, ensure_ascii=False)[:200])
        return 3
    print("[OK] access_token 已获取（凭据有效）")

    headers = {"Authorization": "QQBot %s" % token, "X-Union-Appid": app_id}
    try:
        gw = _request(GATEWAY_URL, headers=headers)
        ws_url = str((gw or {}).get("url") or "").strip()
    except Exception as e:                                       # noqa: BLE001
        print("[X] 取 WS 网关失败：%s" % e)
        return 4
    if not ws_url:
        print("[X] 网关返回里没有 url：%s" % json.dumps(gw, ensure_ascii=False)[:200])
        return 4
    print("[OK] 网关: %s" % ws_url[:60])

    deadline = time.time() + args.timeout
    print("-" * 58)
    print(">>> 现在到**群里** @ 一下机器人，随便发一句话 <<<")
    print(">>>   注意是群里 @，不是给机器人发私聊（私聊消息里没有群 openid）<<<")
    print(">>> 正在等待，最长 %d 秒（断了会自动重连，别管日志里的重连提示）" % args.timeout)
    print("-" * 58)

    attempt = 0
    while time.time() < deadline:
        attempt += 1
        if attempt > 1:
            print("[..] 第 %d 次尝试建立 WebSocket…" % attempt)
        openid = _listen_once(ws_url, token, app_id, deadline)
        if openid:
            print()
            print("[OK] GROUP_OPENID=%s" % openid)
            saved = save_openid(cfg_path, cfg, openid)
            print("[OK] 已回写 %s" % cfg_path)
            print("     现在 data/notify.json 里的 qq_bot 段：%s"
                  % json.dumps({k: (v[:6] + "…" if k == "app_secret" else v)
                                for k, v in saved.items()}, ensure_ascii=False))
            return 0
        left = deadline - time.time()
        if left <= 1:
            break
        # 抢线模式：间隔压到最低。这个 AppID 只要在别处在线（比如 WorkBuddy 的
        # QQ 通道），我们每次连上都会被它挤掉，单条连接的存活只有几秒 ——
        # 唯一能补的就是**尝试密度**：多 READY 一次就多一个窗口。
        time.sleep(0.5)

    print("[X] 等超时了，没收到群内 @消息。")
    print("    检查：① 机器人是否已加入群 ② 群里 @ 的是不是这个机器人"
          " ③ 开放平台是否开了「群聊消息」权限 ④ 机器人状态是否「已上线」")
    print("    ⑤ 群里「机器人主动在群聊内发言」有没有打开"
          " ⑥ 是不是发成了**私聊**（私聊里取不到群 openid）")
    return 5


if __name__ == "__main__":
    raise SystemExit(main())
