"""架构守卫 —— 把 AGENTS.md 里的契约变成可执行的断言。

为什么需要这个文件：本项目有**两个 Agent 在同一目录并行写**，且没有 `.git`（改动无回退点）。
历史上出过这样的事故：一方按「自己版本的入口文件没引用它」判定死代码，删掉了对方正在用的模块，
留下 `ModuleNotFoundError` 的中间态。**「入口没引用」不等于「死代码」** —— 这个断言就是那条铁律的机器版。

覆盖 4 类违约（对应 AGENTS.md 第五节）：
  1. 入口不可导入；
  2. 出现从入口不可达的模块（孤儿模块）；
  3. 冻结接口的签名被单方面改动；
  4. CLI 参数集被改动（或冒出第二个入口）。

运行：
    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

from __future__ import annotations

import ast
import contextlib
import importlib
import inspect
import io
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

ENTRY = "checkin.__main__"

# 有意保留但从入口不可达的模块：留空是有意的。
# 真要豁免某个模块，请在 AGENTS.md 与 HANDOFF.md 里说明理由后再加进来。
ORPHAN_ALLOWLIST: set[str] = set()


# ----------------------------------------------------------------------
# 从磁盘建立"模块名 -> 文件"映射，并做 AST 可达性分析
# ----------------------------------------------------------------------

def _disk_modules() -> dict[str, Path]:
    """{模块名: 文件路径}，覆盖 src/checkin 下全部 .py（跳过 __pycache__）。"""
    out: dict[str, Path] = {}
    for path in (SRC / "checkin").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        parts = list(path.relative_to(SRC).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()                       # checkin/core/__init__.py -> checkin.core
        out[".".join(parts)] = path
    return out


def _imports_of(modname: str, disk: dict[str, Path]) -> set[str]:
    """解析一个模块的 import，返回它可能引用到的 checkin.* 模块名（含 `pkg.attr` 形式）。"""
    tree = ast.parse(disk[modname].read_text(encoding="utf-8"))

    is_pkg = disk[modname].name == "__init__.py"
    pkg = modname if is_pkg else modname.rsplit(".", 1)[0]
    parts = pkg.split(".")

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("checkin"):
                    found.add(a.name)

        elif isinstance(node, ast.ImportFrom):
            if node.level:                    # 相对导入：按包层级回溯
                keep = len(parts) - (node.level - 1)
                if keep <= 0:
                    continue
                base = ".".join(parts[:keep])
                target = f"{base}.{node.module}" if node.module else base
            else:                             # 绝对导入
                if not (node.module or "").startswith("checkin"):
                    continue
                target = node.module or ""

            found.add(target)
            # `from x import y` 里 y 可能本身是子模块（x.y），也可能只是个名字 ——
            # 交给"是否存在于磁盘映射"来过滤，不必在这里区分。
            for a in node.names:
                found.add(f"{target}.{a.name}")

    return found


def _reachable_from_entry(disk: dict[str, Path]) -> set[str]:
    """从入口出发的 BFS 可达集合（只走到真实存在的模块）。"""
    entry_parts = ENTRY.split(".")
    seen = {".".join(entry_parts[:i]) for i in range(1, len(entry_parts) + 1)}  # 含父包
    stack = [ENTRY]
    while stack:
        cur = stack.pop()
        if cur not in disk:
            continue
        for nxt in _imports_of(cur, disk):
            if nxt in disk and nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


# ----------------------------------------------------------------------
# 1. 入口必须可导入（ImportError 会在这里炸，而不是在用户双击的时候）
# ----------------------------------------------------------------------

class TestEntrypoint(unittest.TestCase):
    def test_entry_imports_cleanly(self):
        mod = importlib.import_module(ENTRY)
        self.assertTrue(callable(getattr(mod, "main", None)),
                        f"{ENTRY} 必须导出 main()")

    def test_driver_table_resolves(self):
        """driver_for 惰性导入的各驱动必须都拿得到 —— 这是历史上最容易断的地方。"""
        from checkin.drivers import driver_for
        for mode in ("auto", "manual", "client"):
            drv = driver_for(mode)
            self.assertIsNotNone(drv, f"driver_for({mode!r}) 返回了 None")
            self.assertEqual(drv.mode, mode)
        self.assertIsNone(driver_for("no-such-mode"))

    def test_every_recipe_mode_has_a_driver(self):
        """配方里出现的每个 mode 都要有执行者，否则运行期才炸。"""
        from checkin.core.config import load_config
        from checkin.drivers import driver_for

        _, recipes = load_config(str(PROJECT_ROOT))
        self.assertTrue(recipes, "recipes/ 下一个可用配方都没有")
        for r in recipes:
            if r.mode == "disabled":
                continue
            self.assertIsNotNone(
                driver_for(r.mode),
                f"配方 {r.id!r} 的 mode={r.mode!r} 没有对应驱动")


# ----------------------------------------------------------------------
# 2. 不得出现孤儿模块（AGENTS.md 铁律 1 的机器版）
# ----------------------------------------------------------------------

class TestNoOrphanModules(unittest.TestCase):
    def test_no_module_is_unreachable_from_entry(self):
        disk = _disk_modules()
        self.assertGreater(len(disk), 5, "模块映射明显不完整，检查 _disk_modules()")

        reachable = _reachable_from_entry(disk)
        orphans = sorted(set(disk) - reachable - ORPHAN_ALLOWLIST)

        self.assertEqual(
            orphans, [],
            "发现从入口不可达的模块（孤儿/疑似死代码）：\n"
            + "\n".join(f"    - {m}  ({disk[m].relative_to(PROJECT_ROOT)})" for m in orphans)
            + "\n\n处置方式（禁止直接删除，见 AGENTS.md 铁律 1）：\n"
              "    a) 若它应该被使用 —— 从入口链上引用它，让它可达；\n"
              "    b) 若它确实该废弃 —— 先改名加 .deprecated 后缀观察一回合，"
              "并在 HANDOFF.md 登记「文件 + 理由 + 批准人」；\n"
              "    c) 若它是刻意的豁免 —— 加进本文件 ORPHAN_ALLOWLIST，"
              "并在 AGENTS.md / HANDOFF.md 说明理由。")


# ----------------------------------------------------------------------
# 3. 冻结接口：签名漂移必须被拦下
# ----------------------------------------------------------------------

class TestFrozenInterfaces(unittest.TestCase):
    """签名一旦要改，就先改 AGENTS.md 第三节，再由改动方同步更新这里的期望值。

    这不是"测试写死了实现"，而是**跨 Agent 的接缝锁**：接缝被单方面改掉时，
    对方的调用方会在运行期崩，而且很难定位。
    """

    def _params(self, fn) -> list[str]:
        return list(inspect.signature(fn).parameters)

    def test_engine_run_signature(self):
        from checkin.core.engine import Engine
        self.assertEqual(self._params(Engine.run),
                         ["self", "only", "dry_run", "probe", "now"])

    def test_driver_run_signature(self):
        from checkin.drivers import Driver
        self.assertEqual(self._params(Driver.run),
                         ["self", "recipe", "cfg", "dry_run", "probe"])

    def test_driver_for_signature(self):
        from checkin.drivers import driver_for
        self.assertEqual(self._params(driver_for), ["mode"])

    def test_recipe_from_dict_signature(self):
        from checkin.core.models import Recipe
        self.assertEqual(self._params(Recipe.from_dict), ["d"])

    def test_verdict_map_signature(self):
        from checkin.core.models import Verdict
        self.assertEqual(self._params(Verdict.map), ["self", "http_status", "code"])

    def test_outcome_values_are_stable(self):
        """Outcome 是状态库与通知层共享的词汇表，改动会让历史状态失去意义。"""
        from checkin.core.models import Outcome
        self.assertEqual({o.value for o in Outcome},
                         {"success", "already", "need_login", "circuit",
                          "no_action", "skipped", "error"})


# ----------------------------------------------------------------------
# 4. CLI 参数集冻结 + 不得出现第二个入口
# ----------------------------------------------------------------------

class TestCliContract(unittest.TestCase):
    FROZEN_FLAGS = [
        "--dry-run", "--probe", "--only-site", "--config-root",
        "--now", "--status", "--plan", "--print-task", "--json",
    ]

    def test_frozen_flags_all_present(self):
        entry = importlib.import_module(ENTRY)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                entry.main(["--help"])
        self.assertEqual(cm.exception.code, 0)

        help_text = buf.getvalue()
        missing = [f for f in self.FROZEN_FLAGS if f not in help_text]
        self.assertEqual(missing, [],
                         f"CLI 少了冻结参数：{missing}\n"
                         "要增删参数，先改 AGENTS.md 第三节 + 本文件的 FROZEN_FLAGS，"
                         "并在 HANDOFF.md 登记。")

    def test_no_competing_entrypoint(self):
        """唯一入口是 src/checkin/__main__.py —— 曾经有三套入口，语义分裂过。"""
        forbidden = [
            PROJECT_ROOT / "run.py",
            PROJECT_ROOT / "cli.py",
            PROJECT_ROOT / "runner.py",
            SRC / "checkin" / "cli.py",
            SRC / "checkin" / "runner.py",
        ]
        existing = [str(p.relative_to(PROJECT_ROOT)) for p in forbidden if p.exists()]
        self.assertEqual(existing, [],
                         f"出现了并行的 CLI 入口：{existing}\n"
                         "CLI 语义只能定义在 src/checkin/__main__.py（AGENTS.md 铁律 4）。")


# ----------------------------------------------------------------------
# 5. 协作基建自身也要在（防止契约文件被"清理"掉）
# ----------------------------------------------------------------------

class TestCollaborationScaffolding(unittest.TestCase):
    def test_contract_files_exist(self):
        for name in ("AGENTS.md", "HANDOFF.md"):
            self.assertTrue((PROJECT_ROOT / name).is_file(),
                            f"{name} 缺失 —— 协作契约文件不属于'无引用模块'，"
                            "删它需要走 AGENTS.md 铁律 1 的审批流程。")

    def test_agents_md_still_states_the_delete_rule(self):
        """契约里最关键的一条（禁止自动删除/审批删除）必须还在，否则这份契约形同虚设。"""
        text = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("禁止自动删除", text.replace("**", ""))


if __name__ == "__main__":
    unittest.main()
