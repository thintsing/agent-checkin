# 智能体签到 · Agent Check-In

> 让 AI 编程助手的每日签到自动化 —— **站点知识全部外置为数据，凭证永不离开浏览器。**
>
> *Automate daily check-ins on AI coding-assistant platforms. Site knowledge lives in YAML data; credentials never leave the browser.*

[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-129%20passing-brightgreen)](#测试)
[![License](https://img.shields.io/badge/license-MIT-blue)](#许可)
[![Platform](https://img.shields.io/badge/platform-Windows-0078D4?logo=windows&logoColor=white)](#快速开始)

---

## ⚠️ 先读这段：风险声明

**本项目会自动化执行两个平台的每日签到，这可能违反它们的服务条款。请你自己权衡用哪个账号。**

- 自动签到**违反 Qoder / WorkBuddy (CodeBuddy) 的服务条款**。无论做得多"像人"，都存在账号被限制或封禁的风险，**本工具无法保证不封号**。
- 项目内置的降风险设计（见下文）只是把风险**压低**，不是消除。
- **只用你自己的单一账号、单一设备、单一出口网络**；绝不批量、多号、代理轮换 —— 那才是真正触发风控的模式。
- 项目**明确不做**的一件事：不读取、不复刻客户端加密的 `auth.machine-id` / `auth.v1.dat` —— 那等于**伪造设备身份**，正踩风控红线。Qoder 走的是"CDP 驱动客户端自己点自己的按钮"，不碰任何接口。

> 这个仓库的主要价值在于**工程实现本身**（配方驱动架构、CDP 自动化、多 Agent 协同开发），而不是"帮你多领 100 积分"。请把它当技术参考，自己判断是否值得承担风险。

---

## 这是什么

一个跑在本机的**每日签到调度器**：

- 每天在你设定的时间窗口内，**随机**挑一个时刻执行（不是固定整点 —— 固定时刻在系统层面一眼可辨）；
- 用 **Chrome DevTools Protocol (CDP)** 接管一个**已登录的专用浏览器**（`auto` 站点）或**已运行的桌面客户端**（`client` 站点），完成签到；
- 全程**不碰账号密码、不逆向 token、不伪造设备指纹** —— 鉴权由浏览器 / 客户端自己携带；
- 结果写入本地状态文件（**不含任何凭证**），并给出桌面通知与连续天数统计。

真实运行输出：

```text
  站点            模式      连续    今日失败    今日
  --------------------------------------------------------
  qoder           client    1       0          已完成
  workbuddy       auto      2       0          已完成
```

---

## 三个技术硬骨头

这个项目里真正的难点不是"发个 HTTP 请求"，而是下面三件事。它们的排查过程和结论都写进了 `DESIGN_NOTES.md`。

### ① Qoder 的领取页不是网页，是"宿主驱动的 iframe"

官方口径是"领取渠道**仅限 Qoder CN 桌面端**"。那个 `openapi.qoder.com.cn/growth-page/activity-iframe` 看着像个网页，实测：

| 观察 | 结果 |
|---|---|
| 独立打开 | 渲染"**活动加载失败**"，请求 `GET /sash/api/v1/me/campaigns` → **401 `TOKEN_INVALID`** |
| 该域下的 cookie | 只有 `arms_uid` / `isg` / `tfstk`（风控基建），**无任何登录态** |
| 通信方式 | `postMessage`，且父窗口白名单限定 `*.qoder.cn` / `qoder-cn:` 等 |
| 源码行为 | `window.parent === window` 时**忽略所有消息**，永远等不到数据 |

⇒ **浏览器自动化这条路根本不成立**（登录也换不来数据）。

**解法**：既然只能在客户端里点，那就**驱动客户端自己点**。该 iframe 在 CDP 的 `/json` 里是一个独立的 OOPIF target，附着上去、派发一次**完整 pointer 事件序列**即可 —— 即 `mode: client`。

> 顺带一个坑：这种 UI 绑的是 `pointerdown`，`el.click()` **静默无效**，必须派发完整事件序列。

### ② "客户端一直开着"时，自动化无从下手

调试端口**只能在进程启动时绑定**（Chromium 架构约束），而 Qoder 的单实例锁是**全局**的 —— 实测用隔离 `--user-data-dir` 起第二个实例，那个临时目录**连一个文件都没写出来**进程就没了。

**解法**：**改造日常启动入口**，让客户端启动时就带上 `--remote-debugging-port`。实测 Qoder 的 Launcher 会**透传未知开关**，于是把三个快捷方式入口都加上参数（工具：`tools/win_shortcut_args.py`，含备份与 `--revert`）。

端口值**不写死在工具里** —— 以 `recipes/qoder.yaml` 的 `client.debug_port` 为**单一事实源**（换端口 = 改配方 → 重跑 `--qoder`）。原始 `.lnk` 的备份**只取最早那一份**（不会被二次改造覆盖），所以任何一次改造都能 `--revert` 回到真正干净的状态。

驱动随之采用**四分支决策**，核心原则是 **绝不杀掉用户正在用的客户端**：

| 分支 | 条件 | 动作 |
|---|---|---|
| ① | 读客户端日志，本窗口已领 | **连客户端都不启动** |
| ② | 调试端口已就绪 | 直接接管；**入口没自动出现就按配方把入口点出来**，领完**不关闭**客户端 |
| ③ | 客户端在跑但没开端口 | **绝不杀** → 降级为桌面提醒 |
| ④ | 客户端没在跑 | 才由我们拉起 → 领取 → 关掉自己起的那个 |

② 这一行藏着一个**反直觉的坑**（2026-09-26 实测踩到，当天没能自动领到）：
业务入口**是否自动打开**由「应用**启动那一刻**」的服务端状态决定 —— 客户端整天开着时，
次日状态翻成"可领"后入口**不会自己出现**，"端口已开"于是反而成了唯一拿到**空窗口**的路径。
所以②分支在等待入口失败后，会按配方声明的一串入口**逐级点击把它调出来**
（`client.open_entry_labels`，代码里不出现产品名），再等一轮；
点不到就静默退回原来的判定，行为不劣化。

⚠️ **代价（知情后接受）**：端口常开意味着**本机任何进程**都能连上接管该客户端的渲染进程。

### ③ Agent 的运行环境会"毒化" GUI 进程

在 AI Agent（或它派生的 shell）里启动 Electron 应用，环境里往往带着 `ELECTRON_RUN_AS_NODE=1` —— 应用会**退化成 Node 进程**：不初始化 Chromium、不建窗口、不绑调试端口，跑完脚本就退出。

症状极具误导性：只剩一个 native-messaging 宿主进程、日志目录不产生新 session、报错来自 **Node** 的参数解析器。

**解法**：`core/procenv.py` 在派生子进程前清洗环境（剥离 `ELECTRON_RUN_AS_NODE` / `NODE_OPTIONS` / 各类 Agent 命名空间变量）。

> **A/B 实测对照**：不清 → 40 秒端口未就绪；清了 → **2 秒就绪**。

---

## 支持的平台

| 平台 | 模式 | 做法 | 为什么 |
|---|---|---|---|
| **WorkBuddy** (`codebuddy.cn`) | `auto` | 在**已登录的真实浏览器**页面上下文里 `fetch` 调接口 | 接口幂等、单账号、走真实浏览器 TLS/HTTP2 指纹 |
| **Qoder CN** | `client` | **CDP 驱动桌面客户端**点它自己的按钮 | 网页入口不存在（见上文 ①）；且不伪造设备指纹 |

新增站点**不需要改一行代码** —— 在 `recipes/` 放一个 YAML 即可。

---

## 快速开始

> 环境：**Windows** + Python 3.9+（脚本会自动建 venv 装依赖）。用 Edge 或 Chrome 均可。

```bat
REM 1) 首次：打开"签到专用浏览器"，在里面人工登录一次（登录态长期复用）
run_checkin.bat --login

REM 2) 日常：执行签到（会先等到窗口内的随机时刻）
run_checkin.bat

REM 3) 注册每日计划任务（推荐，自动处理引号 / 中文路径 / 编码）
run_checkin.bat --install-task
```

- 专用浏览器使用**独立 profile**（`%LOCALAPPDATA%\AgentCheckIn\chrome-profile`），与你日常浏览器完全隔离。
- 本机没装 Chrome 时会自动退回 Edge —— 属正常降级。
- 登录成功的硬证据在 cookie 里（`codebuddy.cn` 域下会出现 `KEYCLOUD_SESSION` / `session`）。

### 排查用（全部只读，绝不触发签到）

```bat
python -m checkin --status      REM 各站点状态、连续天数、今日失败次数
python -m checkin --probe       REM 探针：报登录态与真实签到状态
python -m checkin --plan        REM 今天计划几点执行
python -m checkin --dry-run     REM 演练：走完整流程但不触发、不记状态
```

---

## 命令

`run_checkin.bat` 与 `python -m checkin` 是**同一套参数**（前者是后者的包装，会自动建 venv 装依赖）。

```bash
python -m checkin --login              # 打开专用浏览器，人工登录一次
python -m checkin                      # 执行签到（先等到窗口内的随机时刻）
python -m checkin --now                # 立刻执行，不等窗口（手动补跑）
python -m checkin --only-site workbuddy
python -m checkin --status [--json]    # 状态报表
python -m checkin --install-task       # 注册每日计划任务
python -m checkin --uninstall-task
python -m checkin --print-task         # 打印等价的手写注册命令
```

> **注意**：`run_checkin.bat` 结尾带 `pause`，**不要**直接放进计划任务。用 `--install-task`。

> **必须用 `--install-task` 注册**。计划任务的工作目录是 `C:\Windows\System32`，用 `python -m checkin` 会以 `No module named checkin` **每天准时失败且不弹窗**。现在改为直接执行 `src/checkin/__main__.py`（它自己会挂 `sys.path`），与工作目录无关。

---

## 工作方式

### 配方驱动：站点知识是数据，不是代码

`recipes/*.yaml` 描述一个站点的全部知识（URL、接口、判定规则、模式），代码里**不出现任何产品名**。停用站点把 `enabled` 改成 `false` 即可。

```yaml
id: workbuddy
mode: auto              # auto | client | manual | disabled
session: { start_url: ..., tab_match: ... }
actions: { status: {method: POST, path: ...}, trigger: {...} }
verdict:                # 把业务码映射到统一语义，而不是看 HTTP 状态码
  rules: { ... }
  unknown_result: no_action     # 前端改版时不乱枪调用
```

### 判定哲学：宁可"什么都不做"，也不猜成功

- **不信 HTTP 状态码**。签到成功时接口可能返回 HTTP 400，同时业务 `code = 0`。
- 只看响应体业务码，并且**再看 `data.today_checked_in`** 确认"今天到底领没领"。
- 读 `data.status` 的业务词表（`claimed` / `already_claimed` / `event_ended`…），**未知取值一律 `no_action`**。
- **已签就收手** —— 状态接口声明今日已签时，不再发那一次写请求。少一次交互就少一分被注意的理由。

### 降风险设计（对应实现位置）

| 措施 | 实现 |
|---|---|
| **凭证不出浏览器** | 不逆向加密 token、不存账号密码；请求在已登录页面上下文里 `fetch`，浏览器自动带鉴权与真实 TLS/HTTP2 指纹（`browser/page_script.py`） |
| **随机执行时刻** | 计划任务只在窗口前唤醒；真正时刻由 `core/scheduler.py` 在窗口内按 **Beta(2,2)** 采样后等待，**不是固定整点** |
| **单实例运行锁** | `core/lock.py`：系统级文件锁，防"计划任务 + 手动双击"并发导致重复打接口；进程退出自动释放 |
| **当日幂等** | `core/state.py` + `engine.py`：已成功/已签的站点当天不再打接口 |
| **拟人节奏** | 触发前随机延迟 1.5–6s、站点间随机间隔 25–90s（`core/jitter.py`） |
| **失败即停不轰炸** | 仅未知错误指数退避重试（默认 2 次）；`401 / 需登录` 直接转人工、**绝不硬刚**；连败 3 次当日熔断 |
| **日志脱敏** | `core/log.py` 的 `RedactFilter`：token / cookie / Authorization 打码；状态文件不含任何凭证 |
| **进程环境清洗** | `core/procenv.py`：剥离 Agent 注入的 `ELECTRON_RUN_AS_NODE` 等变量 |

---

## 目录结构

```
.
├─ run_checkin.bat            # 日常入口（自动建 venv、装依赖、跑主程序）
├─ config.yaml                # 全局运行参数 + 安全策略（端口、随机窗口、延迟、熔断阈值…）
├─ recipes/                   # ★ 站点配方：站点知识全部是数据，不是代码
│   ├─ workbuddy.yaml         #   mode: auto（接口 + verdict.rules 业务码判定）
│   └─ qoder.yaml             #   mode: client（CDP 驱动桌面客户端自己领）
├─ tools/
│   ├─ qoder_cdp_probe.py     # 只读侦察（定位活动入口，不点击）
│   ├─ qoder_cdp_claim.py     # client 驱动的薄封装（launch / status / claim / close）
│   └─ win_shortcut_args.py   # 读写 Windows 快捷方式参数：让客户端启动即带调试端口（含备份/回滚）
├─ src/checkin/
│   ├─ __main__.py            # 唯一 CLI 入口
│   ├─ core/                  # models / config / engine / scheduler / lock / state
│   │                         #   / jitter / procenv / log / notify
│   ├─ browser/               # launcher（拉起带调试端口的浏览器）/ cdp（极简 CDP 客户端）
│   │                         #   / page_script（注入页面的 JS）
│   └─ drivers/               # browser_page（auto）/ client_claim（client）/ reminder（manual）
├─ tests/
│   ├─ test_core.py           # 离线单元测试（不联网、不开浏览器）
│   └─ test_architecture.py   # 架构守卫：孤儿模块 / 签名漂移 / 入口唯一性
├─ AGENTS.md                  # ★ 多 Agent 协作契约
├─ HANDOFF.md                 # ★ 交接日志
├─ DESIGN_NOTES.md            # 设计决策与实测记录
└─ README.md
```

---

## 多 Agent 协同：这个项目本身的另一条主线

**这个项目是两个 AI Agent 在同一个目录里并行写出来的**（Qoder 侧开发 Agent + WorkBuddy 侧 Agent）。而这件事**一开始翻车了**：

> 一方按"自己版本的入口文件没有引用它"判定某个模块是死代码，把它删掉 —— 而另一方正在用它。
> 结果是 `ModuleNotFoundError` 的中间态。

于是固化了三个东西，把"靠自觉"变成"靠机制"：

| 文件 / 机制 | 作用 |
|---|---|
| `AGENTS.md` | **协作契约**。6 条铁律、冻结接口清单、文件所有权表。最关键的一条：**「入口没引用」≠「死代码」**，删除必须走审批（grep 全仓 → 登记 → 改名观察一回合） |
| `HANDOFF.md` | **交接日志**。每轮收工追加一条，最新在最上。开工先读第一条 |
| `tests/test_architecture.py` | **机械守卫**。把契约变成断言 —— 违约时测试直接红，不靠自觉 |

如果你也在做多 Agent 同目录协作，这部分可能比签到功能本身更有参考价值。

---

## 测试

```bash
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

**129 项，全部离线**（不联网、不开浏览器），约 3 秒跑完：

- **单元测试**：业务码与状态位判定、随机时刻分布、幂等 / 熔断 / 连续天数、退避抖动、
  注入 JS 的转义与脱敏、运行锁互斥、配置加载、引擎各闸门分支。
- **架构守卫（14 项）**：入口可导入、无孤儿模块、冻结接口签名不漂移、CLI 参数集不变。
- 另有多项**针对实测接口的回归断言**（例如"status 必须 POST、不得使用已废弃的僵尸接口"），
  它们的作用是**防止有人把踩过的坑改回去**。

---

## 已知限制（诚实记录）

这个项目在文档里保留了**被推翻的中间结论**和**未验证项** —— 我认为这比粉饰更有价值。

1. **`daily-checkin` 的真实响应尚未观测过**：首次运行时当日积分已通过其它途径领到，
   程序按幂等策略收手，**没有为了"验证"而额外发一次写请求**（多发一次就多一分风险）。
   配方已做双保险（业务词表 + code 规则）。
2. **Qoder 客户端升级可能改版**：若按钮文案 / DOM 变化，驱动返回 `no_action` 并提示人工查看，
   **不会静默假成功**。`0.4.2` 之后的版本需要重新验证。
3. **客户端升级会同时动两处**（2026-09-27 实测 `0.4.2 → 0.4.3`，当天因此没领到）：
   - **快捷方式参数**可能被重置：`开始菜单` 那个入口会被应用**回写成无参数版**
     （已加只读保护挡住；升级时它若重建失败属正常，`data/shortcut_backup/` 有原始备份）。
   - **更难查的一种**：升级时启动器会**自己把应用重启一遍**（`state.ini` 的 `updatedAt`
     与应用的启动时刻重合，且同一刻 `targetVersion` 发生切换），这一次重启**不带**
     透传的调试开关 ⇒ 端口没开，驱动只能走 ③ 降级提醒。
   ⇒ 升级后：重跑 `python tools/win_shortcut_args.py --qoder`，**再从快捷方式重新启动客户端**。
4. **调试端口是一次性资源**：被僵尸句柄占住后（症状：`netstat` 显示 `LISTENING`，
   但**连不上也绑不了**，`netstat` 里记的 PID 已不存在）**只能换端口或重启机器**，
   重启客户端也拿不回。所以端口值以 `recipes/qoder.yaml` 为单一事实源，换端口是改一行的事。
5. **计划任务只在有登录会话时运行**（锁屏算、未登录不算）—— 开浏览器需要桌面会话。

---

## 许可

[MIT](LICENSE) —— 随便用，但**风险自担**。

> 再次提醒：使用本工具产生的任何后果（包括账号被封禁）由使用者自行承担。
