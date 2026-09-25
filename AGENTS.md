# AGENTS.md —— 本目录的协作契约

> **这份文件是给「所有在本目录里干活的 Agent」看的**，不是写给某一个人的。
> 你（无论你是 Qoder、WorkBuddy 还是别的 Agent）**开工前必须先读完本文件 + `HANDOFF.md` 最新一条**。
>
> Qoder 侧：本文件即 `${project}/AGENTS.md`，你启动时会自动加载；拆分规则也可放 `.qoder/rules/*.md`，
> 但**契约本身只维护在本文件里**，避免两处不一致。
>
> 本项目的 HEAD 是 `src/checkin/__main__.py`。**2026-09-25 起本目录已纳入 Git**（可回溯），
> 但铁律之所以是铁律，是因为在引入 Git 之前，每一次误删都真的无法挽回 —— 所以下面这些不是建议。

---

## 一、这个项目是干什么的

两个平台（WorkBuddy / Qoder CN）的每日签到：用 CDP 接管一个**已登录的专用 Chrome**，在页面上下文里
调接口领取积分。站点知识全部外置在 `recipes/*.yaml`，代码里不出现产品名。

---

## 二、铁律（违反即回滚）

| # | 规则 | 为什么 |
|---|---|---|
| **1** | **禁止自动删除文件。** 删任何文件前必须：① `grep` 全仓确认无引用；② 在 `HANDOFF.md` 登记「文件 + 理由 + 谁批准」；③ 先改名加 `.deprecated` 后缀，**观察一个回合**再删。 | 这是本项目翻车的**直接根因**：一方按「自己版本的入口文件没引用」判定死代码，删掉了对方正在用的模块，留下 `ModuleNotFoundError` 的中间态。**「入口没引用」≠「死代码」**——对方的入口可能正在改，或者引用它的文件你还没看到。 |
| **2** | **改文件前必须重新 `Read` 一次。** 禁止凭记忆或凭上文里的内容直接编辑。 | 两个 Agent 并行写时，你手上的内容随时可能是过期的。基于过期内容写入 = 静默覆盖对方的改动。 |
| **3** | **收工必须写 `HANDOFF.md`。** 没写 = 本轮视为未完成。 | 对方无从得知你动了什么。交接靠文件，不靠运气。 |
| **4** | **唯一 CLI 入口：`src/checkin/__main__.py`。** 不新增 `cli.py` / `run.py` / `runner.py` / 第二个 `argparse`。 | 曾经有三套入口（`__main__` / `cli.py` / `run.py`）+ 两个执行核（`Engine` / `Runner`），语义分裂。 |
| **5** | **改「冻结接口」必须先改本文件。** 见第三节。 | 接口是两份实现的接缝。单方面改签名 = 直接让对方崩。 |
| **6** | **收工前必须跑验证门，全绿才算完。** 见第五节。 | 没有回归保护时，两个 Agent 会把对方的 bug 当自己的。 |

---

## 三、冻结接口（改之前先在 `HANDOFF.md` 提案）

这些是跨模块的接缝，**任何签名变动都必须双方确认**，并由改动方同步更新本节。

```python
# src/checkin/core/models.py —— 语义与数据模型
class Outcome(str, Enum):        # success | already | need_login | circuit | no_action | skipped | error
Recipe.from_dict(d: dict) -> Recipe
Verdict.map(http_status, code) -> Outcome

# ↑ 上面三个是冻结的接缝，签名不得单方面改。
# ↓ 下面两个是 2026-09-24 新增的**非冻结**辅助方法（只增不改，老配方不受影响）：
Verdict.already_checked_in(flags: dict|None) -> bool          # 读 data.<checked_in_flag>
Verdict.map_flags(flags: dict|None) -> Outcome|None           # 读 data.<result_flag> 的业务词表

# src/checkin/core/state.py —— 状态库（2026-09-24 新增，非冻结；done_today 的语义一字未动）
StateStore.prompted_today(site_id) -> bool     # manual：今天是否已提醒过（≠ 已签到，不进 streak）
StateStore.record_prompt(site_id) -> None      # 只写 last_prompt_date；不碰 streak/failures/last_done

# src/checkin/drivers/__init__.py —— 驱动协议（新增 mode 只加一行表项）
Driver.run(recipe, cfg, dry_run=False, probe=False) -> CheckinResult
driver_for(mode: str) -> Optional[Driver]      # 表：auto -> BrowserPageDriver, manual -> ReminderDriver

# src/checkin/core/engine.py —— 执行核心
Engine.run(only=None, dry_run=False, probe=False, now=False) -> List[CheckinResult]
```

**CLI 参数集（`__main__.py`）：**
`--login` `--dry-run` `--probe` `--only-site` `--config-root` `--now` `--status` `--plan` `--print-task` `--install-task` `--uninstall-task` `--json`

**配方 schema（`recipes/*.yaml`）：** `id / name / enabled / mode` + `session{kind,start_url,tab_match,token_localstorage_key}`
+ `actions{status,trigger}` + `verdict{transport,rules,http_401_result,unknown_result,checked_in_flag,result_flag,result_map}`
+ `reminder`
（`mode: auto|manual|disabled`；`manual` 只发提醒，**不做自动化**——理由见 `DESIGN_NOTES.md` 决策 B）。

`reminder` 是**自由字典**（引擎只按 key 取值，不认识就忽略），当前认这三个：

| key | 语义 | 谁在用 |
|---|---|---|
| `toast_title` / `toast_body` | 桌面提醒文案 | 所有 `manual` 配方 |
| `open_target` | 提醒时顺手唤起的目标（exe 路径 / URL scheme / 已注册协议） | 目前**都留空**（实测 `qoder-cn:` 无效） |
| `not_before` | `"HH:MM"`（UTC+8）。**早于该时刻不提醒，且不消耗当天的提醒额度**。格式写错时放行（fail-open）。用途：站点开窗晚于计划窗口起点时，避免发出"用户照做也领不到"的无效提醒 | `qoder` = `"10:00"` |

> **`manual` 配方的幂等**：`StateStore.done_today()` 只认 `success`/`already`，manual 站点永远拿不到
> 服务端证据 ⇒ 它有独立的 `prompted_today()` / `record_prompt()`（`last_prompt_date`），
> 只管"今天别重复弹窗"，**不参与连续天数**。两者刻意不合并，别顺手"统一"掉。

> **改配方前先读 `DESIGN_NOTES.md` 的「站点接口事实」一节**：那里记着实测出来的方法/路径/业务码语义。
> 例如 workbuddy 的 status 接口**必须 POST**（GET 会回 404，看着像"接口不存在"），且
> `/billing/meter/checkin-status` 是恒返回全 0 的**废弃接口**——别被它骗成"永远成功"。

---

## 四、文件所有权

所有权不是领土，是**「改之前要不要打招呼」的判据**。改别人的地盘 → 先在 `HANDOFF.md` 登记意图。

| 范围 | 负责方 | 说明 |
|---|---|---|
| `src/checkin/__main__.py` | **共管** | 唯一入口。改前必须登记。 |
| `src/checkin/core/{models,config}.py`、`drivers/__init__.py`、`recipes/*.yaml`、`config.yaml` | **共管** | 契约层与站点知识。改前必须登记。 |
| `src/checkin/core/{engine,lock,scheduler,state,jitter,log,notify}.py`、`tests/` | WorkBuddy | 执行核心与回归保护 |
| `src/checkin/browser/*`（`cdp` / `launcher` / `page_script`） | Qoder | CDP 这层是 Qoder 建的，保持它的风格 |
| `AGENTS.md`、`HANDOFF.md` | **共管** | 契约本身 |

---

## 五、验证门（收工前必跑）

```bat
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

必须**全绿**。其中 `tests/test_architecture.py` 是机械守卫，它会拦下四类问题：

1. 入口不可导入（`checkin.__main__`）；
2. **出现了从入口不可达的模块**（即上面那条铁律要防的「孤儿模块」）；
3. `Driver` 协议 / `Engine.run` / `driver_for` 的签名被单方面改动；
4. CLI 参数集被改动。

跑不过就**修到过**，不要跳过、不要删测试（删测试属于铁律 1 管辖范围，要登记）。

---

## 六、沟通约定

- **谁发现谁记录**：所有取舍写进 `DESIGN_NOTES.md`（决策与理由）；所有交接写进 `HANDOFF.md`（时间、动了什么、状态）。
- **以文件现主为准，不做对抗**：发现对方重写了你写的模块且对方版本能跑 —— **适配它**，不要改回去。
  API 变了就改调用方。改成你自己的版本会让下一个人分不清哪个是活的。
- **不许静默猜**：不确定就写进 `HANDOFF.md` 的「待确认」，别自己编一个事实填上。

**一句总纲：交接靠文件，不靠记忆；删除要审批，改动要登记。**
