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
开发服务器。网页端口就是 `--dashboard-port` 指定的端口。

「告警历史」「用量检索」「磁盘与会话管理」属于按需查看的功能：默认折叠并排在页面
最后，折叠时只显示一行关键结论（未读数、索引规模、目录占用与可归档数量），展开或
点击侧边栏「按需查看」里的入口时才会拉取明细，展开状态记在浏览器本地。

「账号与额度」区以看板卡片并列展示各账号的额度窗口；卡片内的活动会话默认
折叠为摘要按钮，点击「展开 N 个活动会话」再展开完整会话表，「收起会话列表」
恢复折叠，折叠状态在 5 秒自动刷新之间保持。

## 监控内容

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
  全部历史或自定义起止日期）、模型、关键词（会话 ID、JSONL 文件名、项目路径、模型）
  三个维度筛选，可切换「会话明细 / 按日期汇总 / 按模型汇总」三种视图，并按
  最近活动、token 用量或估算金额排序，支持翻页和点击会话 ID 下钻。
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
- 用量检索在 SQL 里按「日期 + 会话 + 模型 + 长上下文」聚合后再算金额，相同筛选
  条件带 30 秒缓存：8 万条记录的索引上，近 30 天查询约 0.13 秒、全部历史约 0.21 秒，
  相同条件重复查询约 1 毫秒。
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
  `--query` 筛选，`--group` 选择分组，`--sort` 选择排序，`--limit` / `--offset`
  翻页，`--json` 输出机器可读结果。检索只读状态目录里的用量索引，
  不会触发重新扫描 JSONL，也不会读取对话内容。

金额是 OpenAI / Anthropic 等官方 API 等价估算，不代表 Plus、Claude 订阅或其他
订阅的实际账单。模型没有已知 API 单价时仍展示 token，但不会计入金额合计。

## WSL 后台服务

在已启用 systemd 的 WSL2 中，可以把监控器安装为当前用户的后台服务：

```bash
token-monitor \
  --state-dir "$HOME/.token-monitor" \
  --codex-home "$HOME/.codex" \
  service install \
  --dashboard \
  --dashboard-host 0.0.0.0 \
  --dashboard-port 8765
```

常用管理命令：

```bash
token-monitor --state-dir "$HOME/.token-monitor" service status
token-monitor --state-dir "$HOME/.token-monitor" service logs --lines 100
token-monitor --state-dir "$HOME/.token-monitor" service restart
token-monitor --state-dir "$HOME/.token-monitor" service stop
token-monitor --state-dir "$HOME/.token-monitor" service start
token-monitor --state-dir "$HOME/.token-monitor" service uninstall
```

服务配置保存在 `~/.token-monitor/service.json`，systemd 单元保存在
`~/.config/systemd/user/token-monitor.service`。卸载服务不会删除监控数据库
或用量索引。状态目录内各文件的用途：

| 文件 | 内容 |
| --- | --- |
| `monitor.sqlite3` | 会话登记、额度快照和恢复记录 |
| `usage-index.sqlite3` | 用量索引与增量读取检查点 |
| `traffic-alerts.sqlite3` | 异常流量告警历史（保留天数由 `--alert-retention-days` 控制） |
| `service.json` | `service install` 保存的 daemon 配置 |
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
