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

三个平台（WorkBuddy / Qoder CN / Trae CN）的每日签到：用 CDP 接管一个**已登录的专用 Chrome**
（`auto`）或**已运行的桌面客户端**（`client`），驱动它们**自己**完成签到。
站点知识全部外置在 `recipes/*.yaml`，代码里不出现产品名。

`client` 站点内部有两条路，由配方 `client.bridge` 是否存在决定：
**有** bridge 段 ⇒ 在客户端页面上下文里调它自己的接口/通道（WorkBuddy 的 http 通道、Trae 的 IPC 通道），
**不点 DOM**；**无** bridge 段 ⇒ 点它自己的按钮（Qoder 走这条）。

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
| **7** | **签到失败要自行处置到底，不许"摆着"。** 用户 **2026-10-14 明确授权**：可以**自行重启目标客户端**（含用户正在使用的实例）、可以改代码。处置链 = **自愈 → 验证成功 → 推送 GitHub**。 | 用户要的是"**结果到手**"，不是"看到报错"。原先"绝不碰用户进程"的保守做法，会让**已知可解**的问题白白漏签一整天（2026-10-14 Trae 就是这么漏的）。⚠ 这是**窄口授权**：只在"已知不会自愈"的场景才动手，且每一条都必须由配方**显式开启**（见下表），不做无谓打断。 |

**铁律 7 的三道窄口（全部默认关闭，配方显式开启才生效）：**

| 配方字段 | 触发场景 | 动作 | 附加前提 |
|---|---|---|---|
| `restart_on_bind_failure` | 命令行**带了**端口参数、端口却连不上（`listen()` 失败，不会自愈） | 重启客户端、当场补领；若判为**僵尸 socket** 则先换端口再重启 | 必须过**忙闲关** |
| `restart_when_portless` | 客户端在跑但命令行**完全没有**端口参数 | 重启客户端、当场补领 | 必须过**忙闲关**；**WorkBuddy 一律不得开启**（那是本 Agent 自己的宿主进程，杀了就掐断会话） |
| `repair_launch_entry` / `repair_autostart` | 同上（不重启客户端的替代方案） | 把**启动入口**（快捷方式 / 注册表自启项）改回带端口 —— 只影响**下一次**启动 | 一天一次；走 `tools/win_shortcut_args.py`（带备份、可 `--revert`） |

**忙闲关的判据链**：`busy_probe.busy_js`（在页面里判）→ 取不到结论时退到**进程 CPU 旁证**
（`cpu_window_sec` / `idle_cpu_sec`）。两条都是"看不清" ⇒ **一票否决，不重启**。
⚠ 2026-10-17 修了一处**名存实亡**：`busy_js` 必须连着调试端口才能跑，而"端口不通"正是
唯一想重启的场景 ⇒ 它此前永远返回 None ⇒ 授权过的自愈**一次都没真正执行过**。
新增 CPU 旁证不是放宽判定，而是补上一条**本来就该有的证据**。

⚠ **同一天又抓到两次同类（2026-10-17）**，写自愈/播报路径时**逐条自查**：

| 机制 | 名存实亡的原因 | 修法 |
|---|---|---|
| `RunLock` 判活 | `OpenProcess` 对**已退出但句柄还被父进程握着**的僵尸进程照样返回句柄（实测 6/6）⇒ `pid_alive` 恒 True ⇒ 锁永远不判陈旧 | 再调 `GetExitCodeProcess`，只有退出码仍是 `259`(STILL_ACTIVE) 才算活着 |
| 昨日缺口补报 | 口径写成"只认 `mode == auto`"，而**真实配方三个站点全是 `client`** ⇒ 生产环境永不触发；单测用手搓的 `auto` 配方 ⇒ 全绿 | 改成"排除 `manual`/`disabled`"，并加一条**直接读真实配方**的守卫 |

> ⇒ **判据要按真实配方 / 真实数据写，不能按测试里的便利写。**
> 写完一条路径就问："它依赖的字段，在真实配方里是什么值？在它该生效的场景里拿得到吗？"

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
# ⚠ 本轮还能等多久（`core.models.RunBudget`）**不走参数**，由引擎挂在 `cfg.budget` 上
#   带下去 —— 加参数会破坏这条冻结签名。"等待多久"本来就是调度层才知道的信息。
driver_for(mode: str) -> Optional[Driver]      # 表：auto -> BrowserPageDriver, manual -> ReminderDriver,
                                               #     client -> ClientDriver

# src/checkin/core/engine.py —— 执行核心
Engine.run(only=None, dry_run=False, probe=False, now=False) -> List[CheckinResult]
```

**CLI 参数集（`__main__.py`）：**
`--login` `--dry-run` `--probe` `--only-site` `--config-root` `--now` `--status` `--plan` `--print-task` `--install-task` `--uninstall-task` `--json`

**配方 schema（`recipes/*.yaml`）：** `id / name / enabled / mode` + `session{kind,start_url,tab_match,token_localstorage_key}`
+ `actions{status,trigger}` + `verdict{transport,rules,http_401_result,unknown_result,checked_in_flag,result_flag,result_map}`
+ `reminder` + `client{…}`（仅 `mode: client` 用到）
（`mode: auto|manual|client|disabled`；`manual` 只发提醒，**不做自动化**——理由见 `DESIGN_NOTES.md` 决策 B。
`client` = **驱动桌面客户端自己完成签到**：配方**有** `bridge` 段就在客户端页面里调它自己的
http 通道 / IPC 通道（WorkBuddy 的 `window.wb.http`、Trae 的 `window.vscode.ipcRenderer` 走这两条，
免维护登录态），**无** `bridge` 段就点它自己的按钮（Qoder 走这条）。
bridge 两段 JS（`status_js` / `claim_js`）各返回一段 **JSON 字符串**，驱动**只认顶层 `code`**
（业务码需在 JS 里从 `data.code` 提到顶层），`data` 原样带出供 `verdict.checked_in_flag` 判定；
判定三段：status 预检 → claim → **再读 status 复核**（防"假成功"降级 `no_action`）。

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
