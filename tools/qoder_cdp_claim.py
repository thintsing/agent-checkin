#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Qoder CN 每日 Credits —— 手动跑一次领取。

**这个脚本不自己实现任何自动化逻辑**：启动/点击/判定全在
`src/checkin/drivers/client_claim.py` 里。各自实现一份必然漂移
（本项目已经因为"同一结论两处写法"踩过坑），所以这里只做两件事：
加载配置 + 把结果打印成人看得懂的样子。

用法
----
    python tools/qoder_cdp_claim.py --status   # 只读诊断：不启动客户端、不点击
    python tools/qoder_cdp_claim.py --claim    # 完整跑一次：拉起 → 点领取 → 关掉

什么时候用
----------
日常**不需要**手动跑 —— 计划任务（`AgentCheckin`）每天在窗口内自动执行。
这个脚本是给这两种场景用的：
  * 排查（`--status` 把端口/进程/日志/可执行文件四个信号一次列清）
  * 手动补领（`--claim`，比如当天自动跑时客户端正开着、走了降级提醒）

只读侦察活动入口结构（**不点击**）请用 `tools/qoder_cdp_probe.py`。

背景与全部踩坑记录
------------------
见 `recipes/qoder.yaml` 头注释、`src/checkin/core/procenv.py`、
`src/checkin/drivers/client_claim.py`，以及 `HANDOFF.md` 2026-09-25 条目。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "src"))

from checkin.core.config import load_config                    # noqa: E402
from checkin.drivers.client_claim import ClientDriver          # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Qoder CN 每日 Credits 领取（手动跑一次）")
    ap.add_argument("--status", action="store_true",
                    help="只读诊断：不启动客户端、不点击")
    ap.add_argument("--claim", action="store_true",
                    help="完整跑一次：必要时拉起客户端并点击领取")
    ap.add_argument("--site", default="qoder", help="配方 id（默认 qoder）")
    args = ap.parse_args()

    if not (args.status or args.claim):
        ap.print_help()
        return 1

    cfg, recipes = load_config(ROOT)
    recipe = next((r for r in recipes if r.id == args.site), None)
    if recipe is None:
        print(f"找不到配方 {args.site!r}（recipes/ 下没有这个 id）")
        return 2
    if recipe.mode != "client":
        print(f"配方 {args.site!r} 的 mode 是 {recipe.mode!r}，本工具只处理 mode=client")
        return 2

    driver = ClientDriver()
    # --status 走 probe：驱动内部会跳过一切写操作（不启动、不点击）
    result = driver.run(recipe, cfg, probe=args.status)

    print()
    print("=" * 60)
    print(f"  {'诊断' if args.status else '执行'}结果：{recipe.name}")
    print("=" * 60)
    print(f"  outcome : {result.outcome.value}")
    print(f"  message : {result.message}")
    if result.detail:
        print("  detail  :")
        for k, v in result.detail.items():
            text = str(v)
            if len(text) > 300:
                text = text[:300] + "…"
            print(f"      {k} = {text}")
    print()

    if args.claim:
        print(json.dumps({"site": recipe.id, "outcome": result.outcome.value,
                          "message": result.message}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
