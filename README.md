# token-monitor

本地 code agent 监控器。它读取 Codex / Grok / Kimi / Command Code 账号额度、
扫描正在运行的 Codex session JSONL，并在网页中展示账号、额度窗口、活动会话、
用量估算，以及 Codex CLI、Grok CLI、Kimi Code、DeepSeek Harness、
Command Code、Claude Code、OpenCode 等进程的异常流量。超出阈值的异常大上传
会在 Dashboard 顶部告警，并落盘为可检索、可标记已读的历史告警。

当前版本只负责观察和统计，不会因为额度状态启动新的 Codex 任务，也不提供
额度中断后的自动处理入口。

## 从 codex-reset-monitor 改名

项目原名 `codex-reset-monitor`，现改名为 `token-monitor`：仓库目录、Python 包
（`token_monitor`）、命令行程序、conda 环境和 systemd 单元都使用新名字。升级时：

- 后台服务需要重新安装一次，单元名才会换成 `token-monitor.service`：
  `token-monitor --state-dir "$HOME/.token-monitor" service install ...`。
  在重新安装之前，`service status` / `service logs` / `service uninstall`
  仍会自动识别并操作旧的 `codex-reset-monitor.service`。
- 状态目录默认使用 `~/.token-monitor`；如果该目录还不存在而旧的
  `~/.codex-reset-monitor` 存在，会继续使用旧目录，额度快照、会话记录和用量
  索引都不会丢失。想迁移时把旧目录改名或复制为 `~/.token-monitor` 即可。
- 旧的可执行文件 `codex-reset-monitor` 会被移除，改用 `token-monitor`。

## 安装

需要已经安装并登录 Codex CLI。项目只使用 Python 标准库，依赖环境按 conda 管理：

```bash
conda env create -f environment.yml
conda activate token-monitor
```

也可以直接使用当前 Python：

```bash
conda run -n token-monitor python -m pip install -e .
```

## 使用

全局参数放在子命令之前。默认状态目录是 `~/.token-monitor`，默认账号是
`~/.codex`。`--codex-home` 可以重复传入多个账号。

```bash
# 查看当前账号额度
token-monitor \
  --state-dir "$HOME/.token-monitor" \
  quota --json

# 单次发现活动会话
token-monitor \
  --state-dir "$HOME/.token-monitor" \
  sessions --json

# 扫描本机 code agent 异常流量（两次采样之间默认隔 1 秒）
token-monitor traffic --json

# 查询已落盘的历史异常流量告警（默认最近 7 天、未读、最多 50 条）
token-monitor --state-dir "$HOME/.token-monitor" alerts --days 7 --unread

# 历史告警的已读与清理
token-monitor --state-dir "$HOME/.token-monitor" alerts --ack-all
token-monitor --state-dir "$HOME/.token-monitor" alerts --clear-before 30 --dry-run
token-monitor --state-dir "$HOME/.token-monitor" alerts --prune

# 检索 token 用量历史（按日期、模型、会话）
token-monitor --state-dir "$HOME/.token-monitor" usage --days 30 --limit 20
token-monitor --state-dir "$HOME/.token-monitor" usage --days 0 --session 01a0c7ed --json
token-monitor --state-dir "$HOME/.token-monitor" usage --days 90 --group model --sort tokens
token-monitor --state-dir "$HOME/.token-monitor" usage --days 30 --group account --sort cost
token-monitor --state-dir "$HOME/.token-monitor" usage --account account-work --group model

# 查看 agent 数据目录占用、磁盘提醒和可归档会话
token-monitor --state-dir "$HOME/.token-monitor" disk --days 30

# 会话归档（先预览，加 --yes 才执行）与恢复
token-monitor --state-dir "$HOME/.token-monitor" --codex-home "$HOME/.codex" \
  sessions --archive --older-than 30
token-monitor --state-dir "$HOME/.token-monitor" --codex-home "$HOME/.codex" \
  sessions --archive --older-than 30 --yes
token-monitor --state-dir "$HOME/.token-monitor" \
  sessions --restore "$HOME/.token-monitor/archives/codex-sessions-20260922-170000.tar.gz"

# 持续监控两个账号并启动网页 Dashboard
token-monitor \
  --state-dir "$HOME/.token-monitor" \
  --codex-home "$HOME/.codex" \
  --codex-home "$HOME/.codex-work" \
  --commandcode-home "$HOME/.commandcode" \
  daemon \
  --dashboard \
  --dashboard-host 0.0.0.0 \
  --dashboard-port 8765
```

Dashboard 默认地址是 `http://127.0.0.1:8765/`。需要从 Windows 或局域网访问
WSL 时，可以使用 `--dashboard-host 0.0.0.0`；这会让网页可被网络中的其他设备
访问，应根据实际网络环境配置。

Dashboard 是 Python 服务内嵌的 HTML、CSS 和 JavaScript，不需要单独启动前端
开发服务器。网页端口就是 `--dashboard-port` 指定的端口。页面自带浏览器标签图标：
`/favicon.svg`（矢量）和 `/favicon.ico`（内置 16/32 像素 PNG 回退）。侧栏品牌图形与
标签页图标出自**同一份几何**，不写额外文件、不请求外部资源。
图标由一组几何原语描述（圆角矩形 / 圆 / 圆环 / 圆头线段 / 圆弧 / 折线 / 多边形，
外加从图形里挖洞的 cut 模式）：同一份原语生成矢量 SVG，也用 zlib + struct 光栅化成
PNG / ICO（4×4 超采样抗锯齿），所以两处渲染结果一致。徽章渐变直接取页面自己的调色板
（左上 violet 主色 #8b5cf6 → 右下 cyan 状态色 #06b6d4），标签页图标、侧栏 logo 与页面
共用一套颜色。候选方案来自 Stitch MCP 的设计稿
（用量柱 / 代币脉搏 / 额度表盘 / 成本盾牌 / 鲸鱼剪影），当前上线的是**成本盾牌 + 脉搏线**；
换方案只改 `_FAVICON_STYLE` 一个常量，URL 指纹随之变化，浏览器会自动重新拉取。图标 URL 带几何指纹（`?v=…`）并按内容长期
缓存，改图标即换 URL，浏览器不会一直沿用旧的空图标；页面与监控状态接口仍是 `no-store`。

「告警历史」「用量检索」「磁盘与会话管理」属于按需查看的功能：默认折叠并排在页面
最后，折叠时只显示一行关键结论（未读数、索引规模、目录占用与可归档数量），展开或
点击侧边栏「按需查看」里的入口时才会拉取明细，展开状态记在浏览器本地。

「账号与额度」区以看板卡片并列展示各账号的额度窗口，卡片标题是**订阅类型**
（`产品 · 套餐`，例如 `Codex · Plus`、`Codex · Pro Lite`、`Grok · SuperGrok`、
`Command Code · GOAT`），账号 ID、profile 和来源退到标题下方的次要信息行；
没有套餐概念的 provider（Kimi、DeepSeek Harness、Claude Code）只显示产品名，
不会拿模型名冒充套餐。订阅类型来源：Codex 读本地 `auth.json` 里 `id_token` 的
`chatgpt_plan_type` claim（按文件 mtime 缓存，不解析或记录任何令牌内容），
Grok / Command Code 用各自额度接口返回的套餐名，扫描目录元数据里已写明的值优先；
未收录的取值只做首字母大写。卡片内的活动会话默认折叠为摘要按钮，点击
「展开 N 个活动会话」再展开完整会话表，「收起会话列表」恢复折叠，
折叠状态在 5 秒自动刷新之间保持。

## Web 端扫描目录管理

Dashboard 的「扫描目录」面板可以查看、添加和移除各 provider（Codex / Grok /
Kimi Code / DeepSeek Harness / Command Code / Claude Code）的数据目录，
修改立即热重载生效，无需重启 daemon，也不丢失用量索引检查点。

目录来源的优先级为：Web 配置 > 命令行参数（`--codex-home` 等）> 自动探测默认目录。
Web 配置持久化在状态目录的 `scan-dirs.json`（默认 `~/.token-monitor/scan-dirs.json`）；
把某个 provider 的目录清空表示显式禁用该 provider，在面板中「重置」则回退到
命令行参数或自动探测。

为保证安全，只允许配置当前用户主目录之内、确实存在且可读的目录；`~/.ssh`
等敏感目录和监控状态目录本身不可作为扫描目录，网页也不提供任意路径浏览。

## 健康检查

Dashboard 端口同时提供两个探测端点，便于 systemd、DevDeck、容器和反向代理
监控 daemon：

- `GET /healthz`：存活检查。主循环在阈值内（`max(2×扫描间隔, 120s)`）有心跳
  返回 200 `{"status": "ok"}`，主循环卡死或从未完成首轮返回 503
  `{"status": "stuck"}`。
- `GET /readyz`：就绪检查。所有关键组件（主循环）正常返回 200
  `{"status": "ready"}`；关键组件失败或尚未完成启动返回 503
  `{"status": "not_ready"}`。单个非关键组件（某 provider、流量采集、
  用量索引器、housekeeping）故障只会让整体状态显示为降级，不影响就绪状态码。

`/readyz` 的响应体携带逐组件状态：最后成功时间、最近错误（主目录路径脱敏为
`~`，不暴露凭据）和是否过期；`/api/state` 的 `health` 字段提供同样的摘要，
Dashboard 顶栏据此显示「正常 / 部分降级 / 启动中 / 异常」指示，点击可查看
异常组件和最近错误。daemon 重启后组件状态从「启动中」开始，首轮成功后转为正常。

## 历史数据管理

用量索引、会话历史和异常流量告警三类历史数据分别配置保留天数：

- 用量索引明细：默认 90 天（`--usage-retention-days`，daemon 和 service install
  均支持）。
- 已结束会话历史：默认 30 天（`--session-retention-days`）。
- 异常流量告警：默认 30 天（`--alert-retention-days`）。

Dashboard 设置页的「历史数据」子块展示状态目录及各类索引的磁盘占用，并可在线
修改用量与会话的保留天数（Web 配置优先于命令行参数，持久化在状态目录的
`settings.json`，修改立即生效无需重启）。子块内提供清理预览——明确将删除的
数据范围和预计释放空间——确认后立即清理；daemon 也会每天按生效保留期自动清理
过期历史并在删除后压缩（VACUUM）数据库。清理只删除过期的历史行：绝不影响仍在
活动的会话和用量索引的增量读取检查点；自动清理失败时记录原因，并通过健康组件
徽标在 Dashboard 顶栏提示。

## 监控内容

- **Dashboard 与设置页支持白天 / 夜间主题**：右上角按钮在「跟随系统 → 白天 → 夜间」
  之间循环，选择保存在浏览器 `localStorage`（两个页面共用同一份偏好），默认跟随系统
  `prefers-color-scheme`；首屏脚本在样式解析前写入 `data-theme`，切换主题与刷新页面都
  不会闪屏。所有配色收敛成一套语义变量（状态色、边框、遮罩、图表轨道、阴影），浅色模式
  下表格、徽标、告警等级与图表一起变色。
- **Codex 账号是可选的**：没有 Codex CLI、`CODEX_HOME` 或有效登录时 daemon 仍可启动，
  只监控已启用的其他 provider（Grok / Kimi / DeepSeek Harness / Claude Code /
  Command Code）或仅监控流量与磁盘占用。每个 provider 目录独立初始化：目录不存在直接跳过，
  读取失败只记一条日志并跳过该目录，不影响其他 provider 和 Dashboard；`service install`
  同样允许 `codex_homes: []`，此时不再解析 Codex 可执行文件。`token-monitor quota`
  在没有任何账号时给出可读提示并返回退出码 2。
- 额度查询使用 Codex App Server 的 `account/rateLimits/read`，不会发送模型提示词。
- 每个 `CODEX_HOME` 使用独立的登录状态、session 范围和额度快照。
- 活动会话以实际打开 JSONL 的进程和 App Server 会话状态为依据。
- JSONL 按字节偏移增量读取，避免重复扫描大型历史文件。
- 用量页按需汇总 Codex session JSONL、Grok unified 日志、Kimi wire 日志和
  DeepSeek Harness projcache 合计。
- **统一会话模型**：Codex、Grok、Claude Code、Kimi、DeepSeek Harness、Command Code
  的活动会话都由各自的发现适配器产出同一个 `TrackedSession` 结构，账号、产品、项目、
  模型、状态、token、开始时间与最后活动时间字段口径一致；公共聚合与 Dashboard 只读这组
  字段，不再依赖 provider 私有结构。discovery 阶段 token 为 0，由用量索引按
  `jsonl_path` 统一回填 token / 上下文 / 轮数（长会话提醒也走同一入口）。
- Claude Code 活动会话以进程实际打开的会话 JSONL 为准（`/proc/<pid>/fd`），
  项目、模型和开始时间只读文件头部，不读取提示词或工具输出；同一会话被多个进程
  打开时合并 pids，与 Kimi / DSH / Grok 的识别口径一致。
- Grok 活动会话以 Grok CLI 进程实际打开的会话文件为准（`/proc/<pid>/fd`），
  进程不持有句柄时退回按工作目录匹配；会话目录名里的 URL 编码项目路径会还原成
  真实目录，模型与创建时间来自 `summary.json`。同一会话被多个进程打开时合并 pids，
  进程退出后自动消失，会话目录被轮转或移动后仍按原路径报告。
- Kimi 活动会话以进程打开的 `state.json` / `wire.jsonl` 为准。
- DeepSeek Harness 活动会话以进程打开的 `session.lock` 为准；身份来自
  `~/.dsh/.anonymous-user-id`，不读取 API Key。DSH 本地没有订阅额度窗口。
- 用量索引保存于状态目录，后台重启后从检查点继续。
- 用量区展示近 30 天按天成本趋势图（悬停查看当天金额与 token，当天高亮）、
  缓存节省金额（缓存命中相对全价输入节省的等价美元）和项目成本排行 Top 5。
- 「用量与成本估算」区支持三种统计维度：按账号（默认）、按模型、按项目。
  三种维度由同一份模型级叶子数据聚合，切换维度不改变合计口径：表尾给出合计行，
  每行给出在筛选结果内的金额占比，并且始终显示账号成本排行 Top 5（金额 + 占比）
  与项目成本排行 Top 5。筛选条件为账号、模型、项目三者可叠加；
  账号口径沿用真实账号 ID 归并（`account_id` 优先，没有时退回 `profile:<名字>`），
  同一账号在多个产品下产生用量也只算一行，行内用 Codex / Grok / Kimi Code /
  Claude Code / DeepSeek Harness 等产品标签标注来源。
- `--budget-usd <美元>`（daemon 和 service install 均支持）设置每月 API
  等价金额预算：用量区显示本月预算进度条；本月金额达到预算 80% 时页面顶部
  出现黄色告警、达到 100% 时变为红色告警。配额窗口耗尽或用量超过 90% 也
  会在同一告警区提示。
- Kimi 账号配额接口返回 booster 钱包时，用量区展示本月真实扣费、月限额和
  余额（按接口币种显示 ¥/$），并与本地 API 等价估算并列，方便核对订阅扣费。
- 「习惯分析」区对已索引的对话做本地统计分析，可按全部 / 近 7 天 /
  近 30 天切换统计窗口：习惯画像（平均每对话规模、最活跃时段、主力模型、
  用量最高的一天、周末占比、缓存累计节省）、
  活跃时段图、模型成本分布、对话规模分布和最贵对话 Top 5，并按规则生成
  省 token 建议（缓存命中率、短对话合并、缓存写入开销、超长对话拆分、
  输出压缩、模型与单对话成本集中度、项目间缓存差异等），能量化的建议会
  给出按当前用量规模折算的预计节省金额。分析只读取 token 元数据，不读取
  提示词或工具输出等对话内容。
- `--kimi-home` 可重复传入多个 Kimi Code 数据目录（默认在存在时使用
  `~/.kimi-code` 或 `KIMI_CODE_HOME`）；Kimi 配额通过与官方 CLI 相同的
  `GET {base}/usages` 接口读取（按 `KIMI_CODE_BASE_URL` 或 `<home>/region`
  选择域名），access token 过期时按官方相同的目录锁协议
  （`~/.kimi-code/oauth/kimi-code.lock`）刷新并原子写回凭据，读取失败时
  只展示账号身份与登录状态，不影响本地用量统计。配额结果带缓存
  （成功 60 秒、失败 15 秒），避免 Dashboard 轮询反复请求接口。
- `--commandcode-home` 可重复传入多个 Command Code 数据目录（默认在存在时
  使用 `~/.commandcode` 或 `COMMANDCODE_HOME`）；订阅额度通过与官方 CLI 相同
  的后台接口读取：`/alpha/whoami`（身份与组织）、`/alpha/billing/credits`
  （套餐名额余额、5 小时与每周窗口）、`/alpha/billing/subscriptions`
  （套餐、订阅状态与账期）和 `/alpha/usage/summary`（本账期请求数与扣费），
  请求带官方 CLI 相同的 `User-Agent` 与版本头，缺少时服务端会返回 403。
  `auth.json` 中的 API Key 只用于鉴权请求，不写入返回值、日志或 Dashboard；
  读取失败时只展示账号身份与登录状态。配额结果带缓存（成功 60 秒、
  失败 15 秒），避免 Dashboard 轮询反复请求接口。
- Command Code 账号卡片展示账号 ID、Profile、套餐（如 GOAT / Pro / Teams Pro）、
  登录状态，以及 5-hour / Weekly / monthly 三个额度窗口；用量区额外给出本月
  订阅真实扣费、剩余名额、请求数和账期重置天数，与本地 API 等价估算并排对账。
- Command Code 活动会话以进程实际打开的 `projects/<项目>/<session>.jsonl` 为准；
  工作目录取会话头 `type: "session"` 记录的 `cwd`，模型取同目录
  `<session>.meta.json`，不读取对话正文或工具输出。
- 异常流量监控扫描本机 `codex` / `grok` / `kimi` / `dsh` / `command-code` /
  `claude` / `opencode` 等进程及其子进程的已建立 TCP 连接，使用内核 `tcp_info` 的
  `bytes_sent` 计算外发增量。回环地址（本机 Web UI）不计入外发告警；
  新连接第一次只记基线，避免把监控启动前的历史流量当成突发上传。
  DeepSeek Harness 的 Web UI（默认 `:3080`）把会话推给浏览器也不计入外发告警。
  默认 15 秒内外发 8 MiB 黄色告警、32 MiB 红色告警；5 分钟累计 64 MiB /
  256 MiB 同样分级。可用 `--upload-warn-mb`、`--upload-alert-mb`、
  `--upload-window-warn-mb`、`--upload-window-alert-mb` 调整。只记录
  进程、工作目录、对端地址和字节数，不读取连接内容。
- 异常流量告警会落盘到状态目录的 `traffic-alerts.sqlite3`，daemon 重启后仍可
  查询。同一进程、同一触发规则、同一级别的重复告警在 5 分钟合并窗口内合并为
  一条记录（`count` 累计、`peak_bytes` 取峰值、重新变为未读）；`--alert-retention-days`
  （daemon 和 service install 均支持，默认 30 天）控制保留天数，daemon 按小时
  自动清理过期记录。
- Dashboard 的「异常流量监控」区展示实时进程表，「告警历史」区按时间段、
  级别、触发规则、已读状态和关键词检索历史告警，可逐条或一键标记已读，
  并按当前时间范围删除历史告警（删除前会提示影响条数）。「近 15 秒外发」卡片
  显示未读告警数。历史页面只展示进程、目录、对端和字节数等元数据。
- `token-monitor alerts` 在命令行查询同一份历史：`--days` / `--since` /
  `--until` 控制时间范围，`--level`、`--kind`、`--product`、`--query` 筛选，
  `--unread` / `--read` 过滤已读状态，`--json` 输出机器可读结果，`--stats`
  只看统计，`--quiet` 只用一个退出码表示「是否存在未读告警」（有未读时为 1）。
  `--ack`、`--ack-all` 标记已读，`--clear`、`--clear-before`、`--clear-all`
  删除历史（`--clear-all` 必须同时加 `--yes`），`--dry-run` 预览清理条数。
- 「用量检索」区直接检索用量索引里的 token 历史记录：时间范围（近 7 / 30 / 90 天、
  全部历史或自定义起止日期）、模型、账号、关键词（会话 ID、JSONL 文件名、项目路径、
  模型）四个条件筛选，可切换「会话明细 / 按日期汇总 / 按模型汇总 / 按账号汇总」
  四种视图，并按最近活动、token 用量或估算金额排序，支持翻页和点击会话 ID 下钻；
  账号下拉来自索引里的文件级账号表，会话明细也会标注该会话属于哪个账号。
  `GET /api/usage/search` 提供同样的能力，`days=0` 表示不限时间；
  单次检索最多扫描 20 万条原始记录，命中上限时返回 `truncated`。
- 活动会话的轮数和最新上下文会与用量索引对照：轮数达到 `--session-turn-warn`
  （默认 100，与习惯分析的「超长对话」口径一致）或最近一次上下文达到
  `--session-context-warn-tokens`（默认 200000）时，Dashboard 顶部提示、
  「账号与额度」的会话表标出「建议开新会话」，daemon 日志也会提醒。
  超长会话每一轮都按全量上下文重新计费，任务收尾后让模型总结再开新会话更省 token。
- `token-monitor disk` 统计 Codex / Grok / Kimi / DeepSeek Harness / Command Code
  数据目录和监控状态目录的占用（含一级子目录排行与会话文件占比）：单个目录超过
  `--disk-warn-gb`（默认 5 GiB）或合计超过 `--disk-total-warn-gb`（默认 10 GiB）
  时提醒；daemon 每 60 秒复查一次，并按 30 分钟冷却写日志，Dashboard 顶部同步提示。
- 会话归档与清理只针对 Codex `<CODEX_HOME>/sessions/**/rollout-*.jsonl`；
  其他 agent 目录只统计和提醒。`sessions --archive --older-than N --yes` 会先把
  选中的会话打包成 tar.gz 并写入 manifest（含文件清单、大小、sha256 和会话用量摘要），
  校验通过后才删除原文件；`sessions --clean` 直接删除；两者缺省都只预览，
  并始终跳过仍在运行的会话、10 分钟内修改过的文件和小于 `--min-size-mb` 的文件。
  活动会话表里每个已结束且有 JSONL 的会话都带「归档此会话」按钮，可以只归档某一个
  会话而不必按天数批量清理（运行中的会话、最近 10 分钟仍在写入的文件不可归档，
  按钮位置会说明原因）；会话表会保留最近 24 小时内结束的会话，方便刚收尾就归档。
  命令行等价写法是 `sessions --archive --session <会话 ID 或路径> [--yes]`。
  Dashboard 的「磁盘与会话管理」区提供同样的预览、归档、清理按钮，并可从归档
  恢复到原路径；网页上的归档和清理在后台执行并按文件上报进度（压缩 → 校验 →
  删除），提交后立即返回，不会让页面等待整包压缩。归档默认放在状态目录的
  `archives/` 下。
- 用量检索在 SQL 里按「日期 + 会话 + 模型 + 长上下文」聚合后再算金额，账号身份来自
  `usage_file_account` 文件级账号表（索引轮次里按 profile / auth.json 覆盖写入，
  不影响 token 明细），账号分组和筛选同样是 SQL 层的 JOIN / 子查询；相同筛选
  条件带 30 秒缓存：8 万条记录的索引上，近 30 天查询约 0.13 秒、全部历史约 0.21 秒，
  相同条件重复查询约 1 毫秒。旧索引第一次打开会自动建表，下一轮索引补齐账号标签，
  在补齐之前这些记录按「未知账号」归组而不是被丢弃。
  索引为每条记录保存长上下文标记，旧索引第一次打开时就地回填一次（不重读 JSONL），
  回填中断后再次打开会继续补齐。
- Claude Code 用量来自本地会话 JSONL（`~/.claude/projects/<项目>/<会话>.jsonl`）：
  只读取 assistant 记录的 `message.usage`、模型、时间戳与 `cwd`，不读取提示词或工具输出。
  同一个 `message.id` 的重复写入只计一次；`subagents/**` 目录在主会话已经内联写入
  侧链消息时会被跳过，避免重复计数（该判断带 10 分钟缓存）。
  用量并入按日、模型、项目和会话的统计与 API 等价成本，趋势图和习惯分析同样覆盖；
  支持断点续扫与日志轮转（文件被替换或截断时自动从零重扫）。
  可用 `--claude-home` 指定目录，默认在存在时使用 `~/.claude` 或 `CLAUDE_CONFIG_DIR`。
  金额按 Anthropic 公开 API 单价换算，缓存写统一按 1.25× 输入价估算。
- `token-monitor usage` 在命令行检索同一份索引：`--days`（0 表示全部历史）或
  `--from` / `--to` 指定日期，`--model`（可重复）、`--session`、`--project`、
  `--account`（匹配账号 ID、profile 名或产品，例如 `codex` / `grok`）、
  `--query` 筛选，`--group` 选择分组（`session` / `date` / `model` / `account`），
  `--sort` 选择排序，`--limit` / `--offset` 翻页，`--json` 输出机器可读结果。
  检索只读状态目录里的用量索引，不会触发重新扫描 JSONL，也不会读取对话内容。

金额是 OpenAI / Anthropic 等官方 API 等价估算，不代表 Plus、Claude 订阅或其他
订阅的实际账单。模型没有已知 API 单价时仍展示 token，但不会计入金额合计。

## 部署与后台服务

### 运行环境

- **Python ≥ 3.10**，只使用标准库，没有第三方运行时依赖，也不需要编译工具链。
  macOS 自带的 `python3` 通常是 3.9，需要 Homebrew、python.org 或 conda 提供的
  3.10+ 解释器；Windows 用 python.org 安装包、Microsoft Store 或 conda 均可
  （`environment.yml` 使用 conda-forge 的 3.11，三个平台都可用）。
- **SQLite** 需要 JSON1 扩展来加速聚合；缺失时（例如很旧的系统 Python）会自动退回
  Python 侧聚合，功能不受影响，只是检索慢一些。
- **Codex CLI 是可选的**：没有 Codex、`CODEX_HOME` 或有效登录时 daemon 仍可启动，
  只监控其他 provider 或仅监控流量与磁盘占用。
- 状态目录由程序自己创建为 `0700`，锁文件与配置文件为 `0600`；daemon 通过
  `flock` 保证同一状态目录只有一个实例。
- Dashboard 默认只监听 `127.0.0.1`；需要局域网或容器外访问时加
  `--dashboard-host 0.0.0.0`，并自行确认网络可信（页面默认没有鉴权）。

### 平台能力对照

| 能力 | Linux | macOS | Windows |
| --- | --- | --- | --- |
| 额度查询、用量索引与检索、告警、磁盘与会话管理、Dashboard、健康检查 | ✅ | ✅ | ✅ |
| 活动会话与进程证据 | ✅ `/proc` | ✅ `ps` + `lsof` | ✅ Toolhelp32 + Restart Manager |
| 异常流量字节统计（`traffic`、流量告警） | ✅ netlink `INET_DIAG` | ⚠️ 只列进程与远端连接，不统计字节 | ⚠️ 同 macOS（`GetExtendedTcpTable` 无字节数） |
| 后台服务 | systemd 用户服务 | launchd LaunchAgent | 计划任务（`schtasks`，登录时启动） |
| 进程发现依赖 | `/proc`（容器需共享 PID 命名空间） | 系统自带 `ps`、`lsof` | 系统自带 Toolhelp32、Restart Manager（`rstrtmgr.dll`） |
| 单实例锁 | `flock` | `flock` | `msvcrt.locking` |

## Linux（含 WSL2）后台服务

在已启用 systemd 的 Linux / WSL2 中，可以把监控器安装为当前用户的后台服务：

```bash
token-monitor \
  --state-dir "$HOME/.token-monitor" \
  --codex-home "$HOME/.codex" \
  service install \
  --dashboard \
  --dashboard-host 0.0.0.0 \
  --dashboard-port 8765
```

WSL2 需要先在 `/etc/wsl.conf` 里启用 `systemd=true` 并重启发行版。常用管理命令：

```bash
token-monitor --state-dir "$HOME/.token-monitor" service status
token-monitor --state-dir "$HOME/.token-monitor" service logs --lines 100
token-monitor --state-dir "$HOME/.token-monitor" service restart
token-monitor --state-dir "$HOME/.token-monitor" service stop
token-monitor --state-dir "$HOME/.token-monitor" service start
token-monitor --state-dir "$HOME/.token-monitor" service uninstall
```

systemd 单元保存在 `~/.config/systemd/user/token-monitor.service`。

## macOS 后台服务

`service` 子命令会按平台自动选择实现：macOS 上写入 LaunchAgent 并交给 `launchctl`
管理，参数与 Linux 完全一致：

```bash
token-monitor \
  --state-dir "$HOME/.token-monitor" \
  service install \
  --dashboard \
  --dashboard-host 127.0.0.1 \
  --dashboard-port 8765

token-monitor --state-dir "$HOME/.token-monitor" service status
token-monitor --state-dir "$HOME/.token-monitor" service logs --lines 100
token-monitor --state-dir "$HOME/.token-monitor" service uninstall
```

- LaunchAgent 位于 `~/Library/LaunchAgents/com.token-monitor.daemon.plist`，
  日志写入 `~/.token-monitor/launchd.log`；卸载会一并移除 plist 与 `service.json`。
- 想手工安装或审查服务定义时，用 `service plist` 打印当前平台的服务定义
  （Linux 输出 systemd 单元，macOS 输出 plist），例如：
  `token-monitor --state-dir "$HOME/.token-monitor" service plist > ~/Library/LaunchAgents/com.token-monitor.daemon.plist`。
- macOS 上没有 netlink，异常流量面板与 `traffic` 命令会明确提示「只显示进程与远端
  连接」，不会报错；字节级告警只在 Linux 生效。
- 活动会话依赖系统自带的 `ps` 与 `lsof`（macOS 默认都有）。若 `lsof` 被裁剪，
  会话仍能被识别，但缺少「打开了哪个会话文件」的证据，活动会话列表会为空。

## Windows 部署

安装方式与 Linux/macOS 相同（Python ≥ 3.10，零运行时依赖）：

```powershell
python -m pip install -e .
token-monitor --state-dir "$env:USERPROFILE\.token-monitor" sessions
```

后台服务用 Windows 计划任务实现，`service` 子命令会自动选择：

```powershell
token-monitor --state-dir "$env:USERPROFILE\.token-monitor" service install --dashboard
token-monitor --state-dir "$env:USERPROFILE\.token-monitor" service status
token-monitor --state-dir "$env:USERPROFILE\.token-monitor" service logs --lines 100
token-monitor --state-dir "$env:USERPROFILE\.token-monitor" service uninstall
```

- 计划任务名 `TokenMonitor`，配置写回 `<state_dir>\service.json`，任务定义备份在
  `<state_dir>\token-monitor-task.xml`（`service plist` 可打印同一份 XML）；
  触发方式是**登录时启动**，运行级别 `LeastPrivilege`，**不需要管理员权限**。
- 日志写到 `<state_dir>\daemon.log`（计划任务的 stdout/stderr 重定向），
  `service logs --follow` 用 Python 轮询该文件，不依赖 `tail`。
- 计划任务没有 POSIX 式的优雅停止信号：`service stop` 等价于结束进程。SQLite 事务与
  单实例锁由系统回收，不会留下损坏的索引；下次启动会从检查点继续。
- 进程发现走系统自带的 Toolhelp32 快照 + Restart Manager：前者给进程树、镜像路径和
  创建时间，后者反查「哪个进程持有会话文件」。工作目录不在进程表里，由会话文件自身
  的元数据推断（Grok/Claude 等适配器已经这么做）。
- 与 macOS 一样没有 netlink：`traffic` 与流量面板退化成 process-only
  （列出 agent 进程与远端连接，不统计字节、不产生流量告警），CLI 与 Dashboard
  都会说明原因。
- 注意事项：
  - `Restart Manager` 不可用（极少数裁剪系统）或目标文件被更高权限进程持有时，
    该会话的活动状态可能识别不到；`quota`、用量索引与 Dashboard 不受影响。
  - NTFS 之外的卷（FAT/exFAT、部分网络盘）拿不到稳定的文件 ID，日志轮转检测会退化
    成按大小/时间判断。
  - 状态目录的 `chmod 0700/0600` 在 Windows 上只影响只读位，不构成权限隔离；
    需要严格隔离时请自行用 `icacls` 收紧 ACL。
  - 路径超过 260 字符需要系统开启 `LongPathsEnabled`。
  - agent CLI 若是 npm 的 `.cmd` 包装，监控器会自动用 `cmd.exe /c` 启动
    （`CreateProcess` 不解析 `PATHEXT`），进程识别也会去掉 `.exe/.cmd/.bat`
    等后缀与 `node .../cli.js` 形式的包装。
  - 正在被 agent 打开着的会话文件在 Windows 上无法删除：归档/清理会跳过该文件并在
    结果里给出原因，而不是中断整批操作。
  - 容器/无计划任务场景可以直接前台运行：`token-monitor ... daemon`。

## 容器部署

- 监控其他 agent 进程需要看到宿主机的 PID 命名空间：`docker run --pid=host ...`；
  否则用量索引（纯文件解析）照常工作，但活动会话、进程证据和流量归属都为空。
- 状态目录挂载到容器内并保持可写，例如 `-v "$HOME/.token-monitor:/state"`，
  再配 `--state-dir /state`；agent 数据目录（`~/.codex`、`~/.claude`、`~/.grok` 等）
  按需只读挂载。
- Dashboard 端口用 `-p 8765:8765` 暴露，容器内需要 `--dashboard-host 0.0.0.0`。
- 容器里没有 systemd/launchd 时，直接用 `token-monitor ... daemon` 前台运行
  （进程管理器负责重启）；`service` 子命令会提示无法执行 `systemctl`/`launchctl`。
- `/healthz`（存活）与 `/readyz`（就绪）可用于容器与反向代理探测。

服务配置保存在 `~/.token-monitor/service.json`（两个平台一致）。卸载服务不会删除
监控数据库或用量索引。状态目录内各文件的用途：

| 文件 | 内容 |
| --- | --- |
| `monitor.sqlite3` | 会话登记、额度快照和恢复记录 |
| `usage-index.sqlite3` | 用量索引与增量读取检查点 |
| `traffic-alerts.sqlite3` | 异常流量告警历史（保留天数由 `--alert-retention-days` 控制） |
| `service.json` | `service install` 保存的 daemon 配置 |
| `scan-dirs.json` | Dashboard「扫描目录」面板保存的 Web 扫描目录覆盖配置 |
| `settings.json` | 设置页「历史数据」保存的 Web 保留天数覆盖配置 |
| `archives/` | 会话归档：`codex-sessions-*.tar.gz` 及其 `.manifest.json` |

Dashboard 除页面和只读接口（`/api/state`、`/api/usage`、`/api/usage/search`、
`/api/insights`、`/api/alerts`、`/api/housekeeping`）外，只额外接受两个写接口：
`POST /api/alerts`（标记已读、删除历史告警）和 `POST /api/housekeeping`
（`archive`、`clean`、`restore`，必须带 `confirm: true`，恢复只接受归档目录内的
文件名）。两者都要求 `Content-Type: application/json` 并限制请求体大小；
其余路径的 POST 仍返回 405。
由于页面默认没有鉴权，使用 `--dashboard-host 0.0.0.0` 暴露到局域网时，
应确认网络内的其他设备可信。

## DevDeck

DevDeck 可以将本项目配置为一个后端服务。工作目录填写项目根目录，启动命令使用：

```bash
PYTHONPATH=/path/to/token-monitor/src \
python -u -m token_monitor \
  --state-dir "$HOME/.token-monitor" \
  --codex-home "$HOME/.codex" \
  daemon \
  --codex /home/yourname/.local/bin/codex \
  --dashboard \
  --dashboard-host 0.0.0.0 \
  --dashboard-port {port}
```

DevDeck 会将 `{port}` 替换成项目配置的端口，因此可以直接在 DevDeck 的“配置端口”
中修改 Dashboard 端口。

## 开发验证

```bash
python -m unittest discover -s tests -v
```
