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
2. 开发设置 → 权限设置：开启 **GROUP_AND_C2C_EVENT (1<<25)**；
   ⚠ 更隐蔽的是第二步：**开发设置 → 事件订阅里勾选「群@消息」**。
   只建应用、填凭据是不够的 —— 没勾这里，表现是"连接正常、心跳正常、
   READY 也收到了，但群里 @ 破天荒不来一个事件"，排查极易跑偏。
3. 建一个群（只有你一人即可），把机器人拉进群；
   群设置 → 机器人 → 打开「**机器人主动在群聊内发言**」。
4. 填好 `data/notify.json` 里的 `app_id` / `app_secret`（或命令行传）。

⚠ 传输层：别被"连上几秒就断"骗了
---------------------------------
本机实测三次，每次都是"0.2s 拿到 READY → 正好第 60.0 秒断，且没有关闭帧"。
看起来像被别处抢占，其实是：**WS 走了会话注入的代理**（`getpeername()` 返回
`127.0.0.1:<随机端口>` 而不是 QQ 的 IP），代理对空闲隧道有 60 秒超时。
修法是发**应用层心跳**（周期取 HELLO 里的 `heartbeat_interval`，实测 41250ms）。
加了心跳之后同一条连接跑满 180 秒稳在线。TCP keepalive 在这里没用 ——
它只到代理本地那一跳，代理到 QQ 那一跳照样空闲。

用法
----
    .venv\Scripts\python.exe tools\qq_bot_openid.py
    # 然后到群里 @机器人 发一句话，脚本会打印 GROUP_OPENID 并回写 data/notify.json
"""
import argparse
import json
import os
import socket
import sys
import time
import urllib.request

import websocket

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
GATEWAY_URL = "https://api.sgroup.qq.com/gateway"
ME_URL = "https://api.sgroup.qq.com/users/@me"
# 群聊 + 单聊事件（GROUP_AT_MESSAGE_CREATE 属于这一位）
INTENT_GROUP_AND_C2C = 1 << 25
OP_HELLO, OP_IDENTIFY, OP_HEARTBEAT, OP_DISPATCH = 10, 2, 1, 0
OP_HEARTBEAT_ACK = 11
# 这些事件会频繁出现且与我们无关，别刷屏；除此之外**一个都不能静默吞**。
_QUIET_EVENTS = ("READY", "RESUMED")


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


def heartbeat_period(hello_msg, default_sec=41.25):
    """从 HELLO 帧里取心跳周期（秒）；拿不到就用兜底值。

    为什么必须在 HELLO 里取而不是硬编码：官方的 `heartbeat_interval` 是服务端下发的
    （实测 41250ms），硬编码一旦平台改值就会踩到"发得太晚被踢"。
    """
    d = (hello_msg or {}).get("d") or {}
    try:
        ms = float(d.get("heartbeat_interval"))
    except (TypeError, ValueError):
        return default_sec
    return ms / 1000.0 if ms > 0 else default_sec


def heartbeat_due(last_hb, now, period_sec):
    """到点了吗 —— 提前到周期的 80% 就发，别卡着边界，留出网络抖动余量。"""
    if period_sec <= 0:
        return False
    return (now - last_hb) >= period_sec * 0.8


def enable_keepalive(ws):
    """TCP keepalive —— 20s 空闲后每 5s 探活一次。

    ⚠ 这条**救不了代理掐隧道**（见本文件顶部 §传输层）：代理掐的是它自己那一跳，
    本地 keepalive 探活只到代理就停了。真正管用的是**应用层心跳**。这里保留它，
    仅为直连场景下的兜底。
    """
    try:
        sock = ws.sock
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.ioctl(0x98000004, (1, 20000, 5000))   # SIO_KEEPALIVE_VALS
        return True
    except Exception:                                          # noqa: BLE001
        return False


def _listen_once(ws_url, token, app_id, deadline):
    """开一次 WS 会话、等群里的 @ 事件。

    返回 `group_openid`；到点或连接被断开都返回 None。

    ⚠ **必须发心跳，否则 60 秒必断**（2026-10-19 取证结论，三次复现一模一样）：
    本机的 WS 会走会话注入的代理（实测 `getpeername()` 返回的是 `127.0.0.1:62046`
    而不是 QQ 的 IP），代理对**空闲隧道**有 60 秒超时 —— 没有应用层心跳时，
    连接拿到 READY 后正好在第 60.0 秒夭折，且**没有关闭帧**（`close_status_code=N/A`），
    看起来就像"被别人抢走"，很容易误判成抢占或机器人离线。
    加上按 HELLO 周期的应用层心跳后，实测 180 秒稳定在线（40/80/120/160s 四次 ACK）。
    TCP keepalive 在这里**没用**：它只 OpenSSL 到代理本地那一跳。

    **为什么还要能重连**：确实存在抢线（同一 AppID 只允许一条 WS），只是它不是
    这次的主因。断了就重连，由上层负责。
    """
    ws = websocket.create_connection(ws_url, timeout=5)
    ws.settimeout(5)
    enable_keepalive(ws)
    seq = None
    hb_period = 41.25                  # 收到 HELLO 之前先兜个底
    last_hb = time.time()
    identified = False
    ready_n = 0
    try:
        while time.time() < deadline:
            now = time.time()
            # 心跳放在 recv 之前检查：recv 超时不能成为唯一的心跳驱动，
            # 否则一旦超时粒度大于周期就永远发不出去（第一次就是这么漏的）。
            if identified and heartbeat_due(last_hb, now, hb_period):
                ws.send(json.dumps({"op": OP_HEARTBEAT, "d": seq}))
                last_hb = now
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not raw:
                break
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            op, t = msg.get("op"), msg.get("t")
            if msg.get("s") is not None:
                seq = msg.get("s")
            if op == OP_HELLO:
                hb_period = heartbeat_period(msg)
                ws.send(json.dumps({"op": OP_IDENTIFY, "d": {
                    "token": "QQBot %s" % token,
                    "intents": INTENT_GROUP_AND_C2C,
                    "shard": [0, 1],
                }}))
            elif op == OP_DISPATCH and t == "READY":
                identified = True
                ready_n += 1
                peer = "(未知)"
                try:
                    peer = str(ws.sock.getpeername())
                except Exception:                              # noqa: BLE001
                    pass
                print("[OK] 已就绪（第 %d 次）—— 现在到群里 @ 机器人" % ready_n)
                print("     心跳周期 %.2fs（服务端下发）；对端 %s"
                      % (hb_period, peer))
                if peer.startswith("('127.0.0.1'"):
                    print("     ⚠ 走了本地代理 ⇒ 没有心跳会在 60s 后被掐，已自动兜住")
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
            elif op == OP_DISPATCH and t not in _QUIET_EVENTS:
                # 未知事件必须**打出来**：曾经只认识两种事件名，其余全部静默忽略，
                # 于是"机器人到底收到没""平台用的是不是别的事件名"这类信息被吞掉，
                # 排查只能靠猜。⚠ SILENT FAILURE 一律不许再有。
                print("[..] 事件 %s（未处理，s=%s）keys=%s"
                      % (t, msg.get("s"), list((msg.get("d") or {}).keys())[:6]))
            elif op not in (OP_HELLO, OP_HEARTBEAT_ACK, OP_DISPATCH):
                print("[..] op=%s t=%s %s"
                      % (op, t, json.dumps(msg, ensure_ascii=False)[:160]))
    except websocket.WebSocketConnectionClosedException:
        peer = "(未知)"
        try:
            peer = str(ws.sock.getpeername())
        except Exception:                                      # noqa: BLE001
            pass
        print("[!] 连接断开了（对端 %s，close=%s/%s）—— 正在重连，不影响"
              % (peer, getattr(ws, "close_status_code", "N/A"),
                 getattr(ws, "close_reason", "N/A")))
        print("    可能是：①同一 AppID 在别处在线（抢线）②代理掐了空闲隧道 "
              "③服务端要求重连。心跳已按 %.1fs 周期在发，①②都兜住了。" % hb_period)
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
        me = _request(ME_URL, headers=headers)
        print("[OK] 机器人身份：%s（id=%s）"
              % (str(me.get("username") or "?"), str(me.get("id") or "?")))
    except Exception as e:                                       # noqa: BLE001
        # 拿不到身份不影响抓 openid，但少了这条证据排查会多绕一圈
        print("[!] 查机器人身份失败：%s" % e)
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
    print("    ⚠ 连接和心跳都正常却收不到事件，**第一嫌疑永远是「事件订阅没勾」**：")
    print("     开发设置 → 事件订阅/权限：勾选「群@消息(GROUP_AT_MESSAGE_CREATE)」")
    print("     —— 只建应用、填凭据是不够的，没勾这里，连接正常、心跳正常、就是没事件。")
    print("   其次检查：①机器人是否已加入群 ②群里 @ 的是不是这个机器人"
          " ③机器人是否「已上线」")
    print("   ⑤群里「机器人主动在群聊内发言」有没有打开"
          " ⑥是不是发成了**私聊**（私聊里取不到群 openid）")
    return 5


if __name__ == "__main__":
    raise SystemExit(main())
