# 设计决策记录（Qoder Agent × WorkBuddy 协同）

本工具由 Qoder 侧开发 Agent 与协同设计者 "WorkBuddy" 讨论后确定。记录关键取舍，便于日后回溯。

> ⚠ **改配方/判定逻辑前，先读文末的「站点接口事实（活体实测）」一节。**
> 早期的侦察结论里已有多处被实测推翻（WorkBuddy 接口方法、Qoder 活动状态与"网页入口"），
> 下面正文保留原记录并已就地标注更正。

## 侦察阶段的事实
1. **WorkBuddy (codebuddy.cn)**：存在每日签到 HTTP 接口与状态查询接口；接口幂等；成功语义看响应体业务 `code`（`0`=成功、`10001`=今日已签），HTTP 状态码不可信（可能 400）。
   > **2026-09-24 更正**：接口确实存在，但**只接受 POST**——原先按 GET 调用会回 404，极易误判成"接口不存在"。
   > 另：`code=0` 只代表"请求成功"，不区分"刚领到"与"今天早领过了"；真正的状态在 `data.today_checked_in`。
   > 详见文末「站点接口事实」。
2. 本机桌面端凭证文件里的 `accessToken` 是**加密存储**（`$wbEncrypted`/envelope），无法直接读明文；`uid` 为明文。
3. **Qoder (QoderWork CN)**：签到**仅在桌面客户端浮窗按钮**，无网页入口；官方条款明令禁止脚本/群控，有设备指纹风控；活动曾标注 2026-07-30 截止（疑似已过期）。
   > **2026-09-24 更正（第一次）**：**这两条都不准确**。① 入口虽在客户端，但领取动作落在**外部网页** `openapi.qoder.com.cn/growth-page/activity-iframe`；
   > ② 活动**仍在滚动进行**（9 月容器跑到 2026-09-30 15:59），客户端日志里有 31 次 `CLAIMABLE` / 485 次 `CLAIMED`。
   > 但**结论（不做自动化）不变**：其接口由 Electron 主进程携带 `Cosy-MachineId/MachineToken` 设备指纹发出，直调 = 伪造设备身份，正踩风控红线。
   > **2026-09-24 更正（第二次，当天晚间实测）**：第一次更正里的"外部网页"**仍不准确** ——
   > 那不是一个能独立打开的网页，而是**只能被客户端内嵌的 iframe**（宿主用 postMessage 供数）。
   > 独立打开必然 401 `TOKEN_INVALID`，页面渲染"活动加载失败"。官方文档亦明写"领取渠道仅限 Qoder CN 桌面端"。
   > ⇒ **不仅"不做自动化"，而是"这条路本来就不通"**。详见文末「站点接口事实」。
4. 实测：在浏览器页面上下文对状态接口 `fetch` 可用，未登录返回 401 —— 证明"复用已登录浏览器会话"这条路成立，前提是用户在浏览器里登录一次。
   > **2026-09-24 补充**：登录后返回的是 404（不是 401）——因为方法用错了。**"404"在本站是"已登录但路由/方法不对"的信号，不是"未登录"。**

## 关键决策
- **A. WorkBuddy 用什么会话？** → 选 CDP 驱动**专用 profile 的真实 Chrome**，在页面上下文里 fetch 调接口（同源、自动带鉴权与真实指纹），而非纯 requests + 手贴 cookie（后者 TLS 指纹一眼假、token 短效需反复重抓，风险高）。
- **B. Qoder 要不要自动化？** → **不要**。~~无网页入口~~ 当时以为只能注 Electron/模拟点击，正撞设备指纹红线；
  收益（每日少量积分）与代价（主力开发账号被封）严重不对称。降级为 `manual`：桌面提醒 + 记"人工确认"。
  > **2026-09-24 晚间复验后修订**：结论不变，但**理由要换**。不是"有网页入口但风险高所以不做"，
  > 而是 **"网页入口根本不存在"**：那个领取页是只能被客户端内嵌的 iframe（见文末「站点接口事实」），
  > 独立打开必 401；官方文档亦明写"领取渠道仅限 Qoder CN 桌面端"。
  > 另：原写的"唤起客户端"**实际没做到** —— 实测 `qoder-cn:` 协议无效果，`open_target` 只能留空，
  > 目前它就是一条纯桌面提醒。别再照抄"唤起客户端"这个说法。
- **C. 降风险清单** → 单账号单设备；当日幂等；执行前随机延迟 + 站点间随机间隔；失败退避且 401 直接转人工不重试；连败熔断；日志与状态全程脱敏。
- **D. 架构** → 分层（core / browser / drivers）+ **配方驱动**：站点知识（URL、接口、判定规则、mode）全部写进 `recipes/*.yaml` 数据文件，代码里不出现产品名；停用/扩展只改配方。
- **E. 判定抽象** → `verdict.rules` 把业务 code 映射到统一 `Outcome` 语义（success/already/need_login/no_action），`unknown_result: no_action` 保证前端改版时不乱枪调用。
- **F. 随机时刻放在哪一层？** → **放在进程内**（"提前唤醒 → 程序自己在窗口内采样后等待"）。
  > **2026-10-08 曾在计划任务侧试过 `-RandomDelay`，2026-10-10 因真漏签一天而回退。**
  > 动机是好的：进程内等待要有一个进程驻留最长 2.4 小时（当时还是 `python.exe` 的黑框，
  > 09:55 起、11:28 才动手 —— 用户直接报障）。
  > 但 `-RandomDelay` 的"随机性由服务端每次重新选取"（MS-TSCH 3.2.5.4.2）在实践中**不可靠**：
  > 10-10 当天任务**一次都没跑**，但调度器已把该 occurrence 记为 `missed` 并跳到次日
  > （`NumberOfMissedRuns=1`；`NextRunTime` 一路显示次日；16 秒内连读 8 次得到 **8 个不同值**；
  > 当天日志文件根本不存在），最后靠 `StartWhenAvailable` 在 **12:22:43**（窗口 12:30 结束前 7 分钟）
  > 才补跑 —— 若那天它没补上，就是**静默漏签**。微软 KB2956042 的标题即
  > 「使用 RandomDelay 参数的计划任务不会运行」。
  > ⇒ 对"绝不能静默漏掉"的每日任务，**随机性宁可放进程内**：
  > 黑框问题已经由 `pythonw.exe` 独立解决（那条改动保留），代价只剩一个**不可见**的等待进程。
  手动 `run_checkin.bat` 与计划任务现在走**同一条**路径（都不带 `--now`），语义不再分裂；
  `--now` 退化为纯"人工立刻补跑"开关。
  > 保留下来的一个隐性缺口修复：`should_skip_today` 原本被关在 `if not now` 里，
  > 任何以 `--now` 调用的入口都会静默绕过 `skip_weekends` —— 已提到 `now` 判断之外并加回归测试锁住。

## 未决 / 观察点
- **客户端"自启动 Run 键"必须与快捷方式一起维护**（2026-10-10 实测缺口）：升级可能新增/重置
  `HKCU\...\Run` 项，开机自启的实例绕过快捷方式 ⇒ 无调试端口 ⇒ 驱动只能降级提醒（当天漏签）。
  工具已支持 `--app <name> --autostart`（含备份与 `--revert`）；**升级后要两条命令都跑**。
- **任务计划程序历史日志本机是关闭的**，且开启需要管理员权限 ⇒ 调度器"为什么跳过某次运行"
  **没有可回溯的证据**（10-10 只能靠 `NumberOfMissedRuns` / 日志文件缺失倒推）。
  建议用管理员终端跑一次 `wevtutil set-log Microsoft-Windows-TaskScheduler/Operational /enabled:true`
  （或在任务计划程序里点「启用所有任务历史」）。
- WorkBuddy 网页端登录后 token 的确切 localStorage 键名未知 → 用 `--probe` 首次运行时探测，必要时锁定到配方 `token_localstorage_key`。
- ~~Qoder 若未来开放网页签到入口，可平移到 `auto` 配方。~~ → **2026-09-24 撤回**：官方明确只做桌面端，短期内不会有网页入口。
  除非 Qoder 官方改变口径，否则 Qoder 保持 `manual`。
- **Qoder manual 模式的幂等语义 → 已定（2026-09-24 22:35，用户拍板"做吧"）**：
  新增独立的"今日已提醒"标记 `state.last_prompt_date`（`prompted_today()` / `record_prompt()`），
  **`done_today()` 一字未动** —— 它继续只认 `success`/`already`，WorkBuddy 的连续天数语义不受影响。
  - `record_prompt()` **不碰** `streak` / `failures` / `last_done`：manual 站点没有服务端证据，
    让它去刷连续天数等于把"我提醒过你"记成"你签到了"。
  - `--status` 的"今日"列现在会显示 `已提醒`，不再恒为 `-`。
  - 回写时机：仅当 `mode == manual` 且驱动返回 `NO_ACTION`（"我这一侧干完了，等你人工确认"）。
    驱动抛异常落到 `ERROR` 时**不记** —— 那种情况用户根本没看到提醒。
  - probe / dry-run 一律绕过该闸门：诊断必须随时可跑，不能被"今天提醒过了"挡住。

- **顺带修掉一个会让上面这条变坏的缺陷：执行窗口早于开窗时刻**（2026-09-24 新增 `reminder.not_before`）：
  `config.yaml` 的计划窗口是 `07:40–11:20`（为 WorkBuddy 的 `00:00` 刷新设的），而 Qoder 每日 **10:00** 才开窗
  ⇒ 约 1/3 的日子里那次唯一的执行落在开窗**之前**。没有时间闸门时它照样提醒（用户照做也领不到），
  而"当天只提醒一次"的幂等会把真正有效的那次吃掉 —— **等于当天白提醒**。
  处置：配方可声明 `reminder.not_before: "HH:MM"`（UTC+8）。早于该时刻 → `SKIPPED`，且
  **不消耗**当天的提醒额度（下次跑到开窗后仍会提醒）。格式写错时**放行**（fail-open）：
  "没提醒"是用户察觉不到的失败，"多提醒一次"只是烦。
  当时的代价：窗口前段命中的日子当天不会再自动提醒（可手动补跑）。

- **窗口迁移 → 已定（2026-09-24 22:40，用户拍板"挪到 10:05–12:30"）**：
  上一条留的尾巴"彻底消除要把 `window_start` 挪到 10:05 之后"已落地 —— `config.yaml` 的计划窗口
  改为 `10:05–12:30`，整段落在 Qoder 10:00 开窗之后，随机调度下**不再可能**与开窗错位。
  - 代价（已向用户明示、用户选择承担）：WorkBuddy 的签到时刻一并后移；随机窗宽 `3h40m → 2h25m`。
    随机性本身是反封堵资产，变窄是净损失，只是被"每次提醒都必然有效"抵过了。
  - `reminder.not_before` **保留**，但角色降级：随机调度下它恒为"已过"，现在守的是**手动跑** ——
    `--now` 绕过窗口采样，早上手动跑一次时靠它拦住，不白吃当天额度。
  - 同步点：`config.py` 的 dataclass 默认值、`scheduler.window_bounds()` 的兜底默认值，
    也已从 `07:40/11:20` 改成 `10:05/12:30`。**三处默认值失同步是最隐蔽的坑** ——
    它们只在"配置文件缺失或键缺失"时才生效，正常跑永远看不出差异。


一句话总纲：**WorkBuddy 借真浏览器的手做真浏览器的事；Qoder 只叫醒人、不冒充人；站点知识进配方，风险挡在判定与熔断之后。**

## 协同合并记录（重要）

WorkBuddy 协同 Agent 在讨论期间也独立写了一套并行脚手架并做了本机取证。经交叉核对后**统一收敛到本仓库当前这套**（低风险的网页接口 + 仅提醒），理由：

- **WorkBuddy 的头号侦察结论被证伪**：其 DESIGN.md 记的"后端域名 `copilot.tencent.com`"实为**自动更新 feed 的 URL**（asar 里的 `buildUpdateFeedUrl` 字符串），并非签到接口；asar 文本搜索里的 `checkin/sign_in` 命中也全是第三方库（semver/zod 的 "checking"、飞书 SDK 的 "sign_in"）的巧合，未定位到真实签到模块。
- **WorkBuddy 给 WorkBuddy 定的机制是"桌面客户端 头像→领取今日礼包" 的 UI 点击**（靠 CDP/UIAutomation 注 Electron 界面）——这与本项目对 Qoder 判定的"高风险、不自动化"是同一条路，且无法证伪风控，故不采纳。
- **本项目的 `codebuddy.cn/v2/billing/meter/daily-checkin` 是浏览器实测存在的 HTTP 接口**（未登录返回 401），配合"复用已登录浏览器"是更低风险、可验证的路径，作为 auto 配方的落地方案。

保留的 WorkBuddy 有效贡献（已吸收进设计）：随机执行窗口、失败熔断、日志脱敏、doctor/status 式诊断（这里体现为 `--probe`）等反封堵理念。

**遗留的待用户确认开放问题**：桌面端 WorkBuddy 的"今日礼包"与本工具打的 codebuddy.cn "daily-checkin" 是否同一套积分/同一账号体系，尚未验证（需要用户在浏览器登录 codebuddy.cn 后用 `--probe` 观察返回）。若两者不是同一权益，则需回到桌面端 UI 方案（并自行承担更高风险）。

---

## WorkBuddy(阿拾) 侧记录：收敛过程与已知踩坑

### 时间线（同一天，同一目录，两个 Agent 并行写）

| 时间 | 事件 |
|---|---|
| 11:03–11:08 | 首稿架构落地：`config.example.yaml`(`platforms:` schema) + `core/logging_setup.py` + `drivers/{cdp,http,winui,selector}.py` + `platforms/*`（三驱动降级、UI 兜底） |
| 11:10–11:16 | **首稿被放弃**，改写为配方驱动：`recipes/*.yaml` + `core/{models,state,jitter,log,notify,config}.py` + `browser/*` + `drivers/{browser_page,reminder}.py` |
| 11:17 | 入口约定落定：`run_checkin.bat` → `python -m checkin` |
| 11:19–11:25 | 两条并行实现被辨识（`Engine` vs `Runner`、`platforms/` vs `recipes/`），交叉验证后收敛 |

判定依据不是"谁写得晚"，而是**可达性 + 实测**：按 `__main__.py` 的 import 图做可达性分析，
`platforms/*`、`drivers/{cdp,http,winui,selector,browser}.py`、`core/{recipes,logging_setup}.py`
全部不可达且无法 import（引用不存在的 `PlatformConfig` / `CheckinStatus` / `request` 依赖缺失），
而配方驱动这条链 `python -m checkin --dry-run` 实测跑通。**结论：平台适配器是首稿，配方驱动是终稿。**

### 阿拾侧补充并修复的缺陷（均经实测复现）

| # | 缺陷 | 症状 | 处置 |
|---|---|---|---|
| 1 | `--dry-run` 语义错误 | `trigger=None` 时被拼成 `fetch("")`（即请求当前页），回一个**假的 `http=200 code=None`**，演练结果彻底失真，还把真实问题藏了起来 | dry-run 与 probe 共用只读路径；修完立刻暴露真相：**当前专用 profile 未登录，状态接口 401** |
| 2 | 注入 JS 未转义 | body 直接拼进 JS 双引号字符串 → `const TRIGGER_BODY = "{"a": 1}";` 语法错误。当前 `body: {}` 侥幸能过，**body 一旦非空签到必崩** | 所有注入值改为 `json.dumps` 编码成合法 JS 字面量；加了往返解码单测 |
| 3 | 无并发保护 | 幂等闸门是"读-判断-写"，非原子；计划任务与手动双击并发 → 同一站点重复打接口（最招风控的模式） | 新增 `core/lock.py` 系统级文件锁；只读模式不加锁；**实测第二实例被正确拒掉** |
| 4 | 随机窗口未接入主路径 | 计划任务在固定时刻触发，若程序立刻执行，则"每天同一秒上报"在系统层面可见 | 窗口采样下沉到 `Engine.run`（`now=True` 可跳过），任何入口都自动获得保护 |
| 5 | 无回归测试 | 改动无保护 | 新增 `tests/test_core.py`，50 项离线单测（判定语义/分布/幂等/熔断/退避/转义/锁/配置/闸门） |

### 协同机制的教训（重要，写给下一个人）

同一目录下两个 Agent 并行写造成了**三轮拉锯**：一方新建的模块（`scheduler.py`、`report.py`、
`cli.py`、`scripts/`），因另一方版本的入口文件里没有引用它，被"删除无引用模块"的清理判为死代码删掉，
而引用它的文件又存活 ⇒ 出现 `ModuleNotFoundError` 的中间态。

已采取的缓解手段：
- **入口收敛为单文件**：所有 CLI 语义只在 `src/checkin/__main__.py` 定义，不再有 `cli.py` / `run.py` / `runner.py`。
- **不依赖 `scripts/`**：`--print-task` 只输出一段可直接粘贴的 PowerShell
  （`Register-ScheduledTask`），直接用 venv 里的 python，少一个目录就少一个被删的面；
  唤醒时刻由 `__main__._wake_hint()` 自算（窗口开始前 10 分钟），不依赖 `scheduler`。
- **在易被误判的模块 docstring 里写明被引用关系**（见 `core/scheduler.py`）。
- **API 以文件现主为准，不做对抗**：`core/scheduler.py` 被重写后（去掉 `day`/`rng`/`startup_hint`），
  由 `engine.py`、`__main__.py`、`tests/` 三处适配到新签名，而不是把文件改回去。

### 已知欠账（留给后续，不在本轮修）

- `core/scheduler.py` 用**模块级可变全局 `_cfg`** 传递配置（`should_skip_today` / `plan_today` 里赋值，
  `window_bounds` 里读取）。单进程单配置下可用，但它使函数非重入、且 `plan_today` 的返回值可能
  已经过期（不 clamp 到"现在"）。更干净的做法是把窗口配置作为显式参数传入，并支持注入
  `now` / `rng` 以便确定性测试 —— 这也正是本文件的单测只能用统计断言的原因。
- `checkin/__main__.py` 目前把 CLI、状态报表、任务命令都放在一个文件里（约 200 行）。
  在"两个 Agent 会互相删文件"的环境下这是有意为之的取舍；如果协同改为串行，可再拆出 `cli/` 包。

**给用户的建议**：这类"两个 Agent 同目录协作"应**串行化**——一方完成后另一方再进场，
否则任何"清理死代码"的动作都会被对方的并发写入变成误删。原子提交 + 明确分工（谁拥有哪些文件）
比"商量着来"更可靠。

---

## 站点接口事实（2026-09-24 活体实测 · 改配方前必读）

### WorkBuddy / codebuddy.cn

取证方式：静态读 `%LOCALAPPDATA%\Programs\WorkBuddy\resources\app.asar`（317MB，未解包）
+ 在已登录的专用浏览器里注入 JS 实测 4 个候选组合（**只 POST 状态接口，未触碰 `daily-checkin`**）。

**① 方法必须是 POST。**

`app.asar` 中 `CloudAccountRepo` 原文：

```js
/**
* - Web（Cloud）：空串 → `/billing/meter/...`（走浏览器 cookie 认证）
* - Desktop：覆盖为 `/v2` → `/v2/billing/meter/...`（走 IDE 网关 Bearer token）
*/
get billingPrefix() { return ""; }

async postCheckin(path) {              // ← 两个接口都走这里，都是 POST + body {}
    const headers = await this.getCheckinRequestHeaders();
    if (headers) return this.deps.context.http.post(path, {}, { headers });
    return this.deps.context.http.post(path, {});
}
```

**② 路径与状态（实测矩阵）：**

| 路径 | 方法 | 返回 |
|---|---|---|
| `/billing/meter/checkin-activity-status` | **POST** | **`code:0`，`data.active=true, today_checked_in=true, streak_days=9, daily_credit=100`** ← **活的，用这条** |
| `/v2/billing/meter/checkin-activity-status` | POST | 同上（桌面端前缀，cookie 认证下也通） |
| `/billing/meter/checkin-status` | POST | `code:0` 但 `active=false, streak_days=0, daily_credit=0` ← **已废弃的僵尸接口** |
| 任一 | **GET** | **404**（登录前 401） |

**③ 判定语义**——`code` 不够，要读 `data`：

- `data.today_checked_in` 才是"今天到底领没领"。`code:0` 只说明请求成功。
- 写接口的业务结论在 `data.status`，取值抄自客户端自己的枚举 `CheckinClaimStatus`：
  `claimed` / `already_claimed` / `not_eligible` / `event_ended` / `unknown_biz_error`。
- **为什么必须按它的词表判**：活动结束后接口仍回 `code:0`，只看 `code` 会报**假成功**。
- 因此：已签时**不再发那一次 trigger 请求**（见 `page_script` 的 `skippedBecauseCheckedIn`），少一次写请求。

**④ 未验证项（不粉饰）**：`POST /billing/meter/daily-checkin` 的**真实响应**尚未观测过 ——
今天（2026-09-24）在本工具介入前用户已自行领取，按幂等策略收手了，没为"验证"而多发一次写请求。
配方已同时配好 `result_map`（按 `data.status`）与 `code` 规则（`0→success`、`10001→already`）双保险。

### Qoder / openapi.qoder.com.cn

**① 官方口径（权威，2026-09-24 取自 `docs.qoder.cn/events/100credits`）：**

- **"领取渠道：仅限 Qoder CN 桌面端。"** ← 那一行决定了这条路的上限
- "领取时间：每日 10:00 开放新一轮领取，领取窗口持续至次日 10:00 前"
- "开始时间：2026 年 9 月 18 日 10:00。结束时间：另行公告。"（**已作废**"7-30 截止"）
- 领取路径原文：**"点击客户端左下角的『用量面板 → 礼物图标』，进入活动入口。"**
  → 本仓库 `recipes/qoder.yaml` 原先写的"右上角浮窗的『签到』按钮"**不准确**，已按原文改正。

**② 领取落地页的真实形态（2026-09-24 CDP 实测 + 读其公开 JS）：**

`https://openapi.qoder.com.cn/growth-page/activity-iframe` 是一个**宿主驱动的 iframe**，不是独立网页：

| 观察 | 值 |
|---|---|
| 页面标题 | `Qoder Campaign`（能加载，不是 404/503） |
| 该域下的 cookie | 只有 `arms_uid` / `isg` / `tfstk`（风控基建），**无任何登录态** |
| 它自己发起的请求 | `GET /sash/api/v1/me/campaigns` → **401** |
| 401 原文 | `{"code":"TOKEN_INVALID","message":"missing authorization token"}` |
| 请求头 | 只有 `Referer`；**没有** `Cosy-MachineId` / `Cosy-MachineToken` |
| 页面渲染 | "专属活动权益 / **活动加载失败** / 请检查网络后重试" + 一个「重试」按钮 |
| 通信协议 | `postMessage`，`protocol="qoder.campaign"`, `version=1`，只认 `host:init` / `host:contextChanged` / `host:apiResponse` |
| 父窗口来源白名单 | 必须是 `qoder.cn` / `qoder.com` / `qoder.ai` 及其子域，或 `qoder-cn:` / `qoder-work:` 等自定义协议 |
| 独立打开时的行为 | 源码 `Ne = window.parent === window` → **直接忽略所有消息**，永远等不到数据 |

⇒ 结论：该页的数据**全部来自宿主（桌面端）**；它的 `Authorization token` 由宿主注入，
**不是浏览器 cookie**。所以"在浏览器里登录一下再打开"这条路不成立 —— 登录也换不来 `host:apiResponse`。

**③ 客户端侧接口（不变）：** `GET /sash/api/v1/me/campaigns` 由 **Electron 主进程**发出，
带 `Cosy-ClientType / MachineOS / MachineId / MachineToken`（`nativeRiskIdentityService`）。
→ **直调 = 伪造设备指纹，明确不做。**

**④ 实测状态与窗口：** 今日容器 `act-20260923-159` 窗口 `2026-09-24 10:00 → 09-25 09:59`
（与官方"每日 10:00 开窗"**完全吻合**；此前记的 `02:00 → 01:59` 是另一天容器的值）。
客户端日志：10:41 `CLAIMABLE` → 10:55 `CLAIMED`（用户已领）。
日志里**完全没有领取请求的痕迹**，与"客户端只展示与跳转、领取不经客户端接口"一致。
奖励 `{kind: CREDITS, amount: 100, validity: 相对 30 天}`。

**⑤ 已排除的岔路（别再试）：** `qoder-cn:` 协议"唤起客户端" —— 实测（客户端已在运行）
`os.startfile("qoder-cn:")` **无任何可观测效果**：不新起实例、不报错、也不把 `Qoder CN`
主窗口置前（Win32 `GetForegroundWindow` 前后同一 hwnd）。未运行时能否拉起**未验证**（要关掉
用户的客户端才能测，没做）。故 `recipes/qoder.yaml` 的 `open_target` 保持留空。

**⑥ 开窗时刻（10:00）与本项目执行窗口的错位 —— 历史问题，2026-09-24 已从根上消除：**
每日 10:00 开窗是官方口径（上 ①），而 `config.yaml` 当时的窗口起点是 07:40，错位约 2 小时 20 分，
按 Beta(2,2) 分布粗算约 1/3 的执行落在开窗前。**开窗前的提醒是负价值**：用户打开客户端也看不到
礼物图标，还会把"当天一次"的提醒额度用掉。
→ 当时的处置是配方声明 `reminder.not_before: "10:00"`；随后用户拍板把窗口整体迁到 `10:05–12:30`，
错位从根上消失（见「未决 / 观察点」末两条）。`not_before` 保留下来专门守"手动跑"这一路。

### 本轮被推翻的三个中间结论（如实留档）

排查过程中我（阿拾）先后给出三个**错误**结论，都被下一条证据推翻。留在这里是为了提醒后来者：
**别把"看起来合理"当"已验证"。**

| 错误结论 | 主张 | 被什么推翻 |
|---|---|---|
| A | "登录后 404 说明配方路径多了 `activity`，接口不存在" | 活体矩阵显示**该路径返回完整真实数据**；真正错的是**方法**（GET→POST） |
| B | "404 是假绿灯，不能据此判定登录成功" | 401→404 的跳变**确实**对应登录（cookie 里有 `KEYCLOAK_SESSION` 佐证）；404 在本站就是"已登录但路由/方法不对" |
| C | "Qoder 领取页可用浏览器会话自动化 —— 与 WorkBuddy 同一条 CDP 路径"（我上轮写进 `recipes/qoder.yaml` 的推断） | ①官方文档明写**"领取渠道仅限 Qoder CN 桌面端"**；②该页是宿主驱动的 iframe（`window.parent === window` 时忽略所有消息，父窗口白名单限定 `*.qoder.cn` 等）；③实测独立打开必 401 `TOKEN_INVALID`，请求头无指纹、无登录 cookie。**三重证据同向。** |

**教训**：判据要落到"可验证的信号"上再下结论 —— 比如登录该看 cookie，路径该看矩阵实测，
而不是从一个 404 往里推断因果。中间结论必须标明是**推断**，不能写成事实。
**补充教训（C 的由来）**：看到"客户端用 `openExternal` 打开了一个 https 网页"，我推断"那就是网页入口"。
但 `openExternal` 打开的不一定是**独立网页** —— 也可能是个**只能被宿主内嵌**的组件。
判断一个页面能不能独立自动化，最低成本的判据是：**直接打开它，看它自己发出的请求有没有鉴权、返回什么**。

