# a-token-monitor

[![CI](https://github.com/pantlive/a-token-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/pantlive/a-token-monitor/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/a-token-monitor.svg)](https://pypi.org/project/a-token-monitor/)
[![Python](https://img.shields.io/pypi/pyversions/a-token-monitor.svg)](https://pypi.org/project/a-token-monitor/)
[![License: GPL v3+](https://img.shields.io/badge/License-GPL--3.0--or--later-blue.svg)](LICENSE)

[English](README.en.md) | 中文

本地 code agent 监控器：读取 Codex / Grok / Kimi / Command Code 等账号的额度窗口，
发现正在运行的 agent 会话，统计 token 用量与 API 等价成本，并监控 Codex CLI、
Grok CLI、Kimi Code、DeepSeek Harness、Command Code、Claude Code、OpenCode 等
进程的异常上传流量。所有数据在本机处理，通过内嵌的网页 Dashboard 展示，
也可以安装为 systemd / launchd / Windows 计划任务后台常驻。

当前版本只负责观察和统计，不会因为额度状态启动新的 agent 任务，也不提供
额度中断后的自动处理入口。

## 功能概览

- **账号与额度看板**：各订阅（Codex Plus、Grok SuperGrok、Command Code GOAT 等）
  并列卡片展示 5 小时 / 周 / 月窗口的进度与重置时间；配额耗尽或用量超过 90%
  时页面顶部告警。额度查询走各官方 CLI 相同的只读接口，不发送模型提示词。
- **活动会话**：以进程实际打开的会话文件为依据（`/proc/<pid>/fd`、`lsof`、
  Windows Restart Manager），统一展示账号、产品、项目、模型、状态、token、
  开始与最后活动时间；超长会话（轮数或上下文超阈值）提示「建议开新会话」。
- **用量与成本估算**：JSONL 按字节偏移增量索引（重启后从检查点继续），按天 /
  模型 / 账号 / 项目聚合 API 等价金额，含成本趋势图、缓存节省金额、账号与项目
  成本 Top 5；支持每月预算（`--budget-usd`）和进度告警。
- **习惯分析**：本地统计活跃时段、模型成本分布、对话规模、最贵对话，
  并按规则给出可量化的省 token 建议。只读 token 元数据，不读对话内容。
- **异常流量监控**：按进程跟踪 agent 的外发 TCP 字节（Linux 用内核
  `tcp_info`），15 秒 / 5 分钟两档阈值分级告警，历史落盘可检索、可标记已读；
  只记录进程、目录、对端和字节数，不读取连接内容。
- **磁盘与会话管理**：统计各 agent 数据目录占用，超阈值提醒；Codex 会话可
  打包归档（含 manifest 校验）或清理，支持单会话归档与从归档恢复。
- **设置页**：网页在线管理各 provider 的扫描目录（热重载、不丢索引检查点）和
  历史数据保留天数（清理预览 + 安全清理 + VACUUM 压缩）。
- **健康检查**：`/healthz`（存活）与 `/readyz`（逐组件就绪状态），Dashboard
  顶栏同步显示健康徽标，便于 systemd、容器和反向代理监控。
- **多语言与主题**：界面 / API / CLI 支持中文与英文（自动判断或 `--lang` 指定）；
  白天 / 夜间主题跟随系统或手动切换。

金额是 OpenAI / Anthropic 等官方 API 等价估算，不代表订阅的实际账单；
没有已知单价的模型仍展示 token，但不计入金额合计。

## 安装

需要 Python ≥ 3.10；只使用标准库，没有第三方运行时依赖：

```bash
pip install a-token-monitor
# 或用 pipx 隔离安装
pipx install a-token-monitor
```

从源码安装（开发）：

```bash
conda env create -f environment.yml   # 或直接用当前 Python
conda activate a-token-monitor
python -m pip install -e .
```

## 使用

全局参数放在子命令之前。默认状态目录是 `~/.a-token-monitor`，默认账号目录是
`~/.codex`；`--codex-home`、`--grok-home`、`--kimi-home` 等都可以重复传入多个目录。

```bash
# 查看当前账号额度
a-token-monitor quota --json

# 单次发现活动会话
a-token-monitor sessions --json

# 扫描本机 code agent 异常流量（两次采样之间默认隔 1 秒）
a-token-monitor traffic --json

# 查询 / 清理历史异常流量告警
a-token-monitor alerts --days 7 --unread
a-token-monitor alerts --ack-all
a-token-monitor alerts --clear-before 30 --dry-run

# 检索 token 用量历史（按日期、模型、账号、会话）
a-token-monitor usage --days 30 --group model --sort tokens
a-token-monitor usage --account account-work --group model

# 查看 agent 数据目录占用、磁盘提醒和可归档会话
a-token-monitor disk --days 30

# 会话归档（先预览，加 --yes 才执行）与恢复
a-token-monitor sessions --archive --older-than 30
a-token-monitor sessions --archive --older-than 30 --yes
a-token-monitor sessions --restore ~/.a-token-monitor/archives/codex-sessions-<时间戳>.tar.gz

# 持续监控多个账号并启动网页 Dashboard
a-token-monitor \
  --codex-home "$HOME/.codex" \
  --codex-home "$HOME/.codex-work" \
  --commandcode-home "$HOME/.commandcode" \
  daemon \
  --dashboard \
  --dashboard-port 8765
```

Dashboard 默认地址是 `http://127.0.0.1:8765/`，页面与接口由 daemon 内嵌提供，
不需要单独的前端服务。`--dashboard-host 0.0.0.0` 可开放给局域网（例如从
Windows 访问 WSL）——页面默认没有鉴权，请先确认网络可信。

### 常用可调参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--budget-usd` | 无 | 每月 API 等价金额预算，80% 黄告警、100% 红告警 |
| `--upload-warn-mb` / `--upload-alert-mb` | 8 / 32 | 15 秒内单进程外发 MiB 的黄 / 红告警阈值 |
| `--upload-window-warn-mb` / `--upload-window-alert-mb` | 64 / 256 | 5 分钟累计外发 MiB 的黄 / 红告警阈值 |
| `--disk-warn-gb` / `--disk-total-warn-gb` | 5 / 10 | 单个目录 / 合计占用的磁盘提醒阈值（GiB） |
| `--session-turn-warn` | 100 | 会话轮数达到该值提示「建议开新会话」 |
| `--session-context-warn-tokens` | 200000 | 最近一次上下文达到该值提示「建议开新会话」 |
| `--usage-retention-days` / `--session-retention-days` | 90 / 30 | 用量索引 / 已结束会话历史的保留天数（设置页可在线改） |
| `--alert-retention-days` | 30 | 异常流量告警的保留天数 |
| `--lang en` / `--lang zh` | 自动 | 显式指定 CLI 与 Dashboard 语言 |

以上参数在 `daemon` 与 `service install` 上都可用。

## Dashboard

- **账号与额度**：每个订阅一张卡片，固定展示 `5 小时 / 周 / 月` 三行窗口
  （缺的周期占位「不适用」，卡片之间严格对齐）；卡片标题是订阅类型
  （`产品 · 套餐`），账号 ID、profile 退到次要信息行。Codex 的套餐名读本地
  `auth.json` 里 `id_token` 的 `chatgpt_plan_type` claim（不解析令牌内容），
  Grok / Command Code 用各自额度接口返回的套餐名，Claude Code 用本地凭据里的
  订阅类型（Pro / Max 等）。
- **用量与成本估算**：按账号 / 模型 / 项目三种维度切换，同一口径聚合；筛选
  条件可叠加，表尾给出合计行与占比，并始终显示账号、项目成本 Top 5。
- **用量检索**：直接检索用量索引：时间范围、模型、账号、关键词四条件筛选，
  四种汇总视图，支持翻页与会话下钻；`GET /api/usage/search` 提供同样能力。
- **告警历史 / 异常流量**：实时进程表 + 历史告警按时间、级别、规则、已读状态
  和关键词检索，可标记已读或按范围删除。
- **按需折叠**：「告警历史」「用量检索」「磁盘与会话管理」默认折叠为一行结论，
  展开时才拉取明细，展开状态记在浏览器本地。

### 设置页

独立的 `/settings` 页面，目前包含两块：

- **扫描目录**：查看、添加、编辑和移除各 provider（Codex / Grok / Kimi Code /
  DeepSeek Harness / Command Code / Claude Code）的数据目录，修改热重载生效，
  无需重启 daemon，不丢失用量索引检查点。优先级为
  **Web 配置 > 命令行参数 > 自动探测**，持久化在状态目录的 `scan-dirs.json`；
  清空某 provider 的目录表示显式禁用，「重置」回退到命令行参数或自动探测。
  安全限制：只允许当前用户主目录之内、确实存在且可读的目录，`~/.ssh` 等敏感
  目录和状态目录本身不可配置，网页不提供任意路径浏览。
- **历史数据**：展示状态目录与各类索引的磁盘占用，在线修改用量索引
  （默认 90 天）与会话历史（默认 30 天）的保留天数，持久化在 `settings.json`。
  清理前给出预览（删除范围 + 预计释放空间）；daemon 每天按生效保留期自动清理
  并 VACUUM 压缩。清理只删过期历史行，绝不影响活动会话和增量索引检查点；
  自动清理失败会记录原因并在 Dashboard 顶栏提示。告警历史的保留天数由
  `--alert-retention-days`（默认 30 天）控制。

### 健康检查

- `GET /healthz`：存活检查。主循环在阈值内（`max(2×扫描间隔, 120s)`）有心跳
  返回 200，卡死或从未完成首轮返回 503。
- `GET /readyz`：就绪检查。关键组件（主循环）失败或未启动完成返回 503；
  响应体带逐组件状态（最后成功时间、脱敏后的最近错误）。单个非关键组件
  （某 provider、流量采集、索引器等）故障只显示降级，不影响就绪状态码。

Dashboard 顶栏据此显示「正常 / 部分降级 / 启动中 / 异常」徽标，点击可查看
异常组件详情。

## 支持的 provider

| Provider | 额度 | 活动会话依据 | 用量来源 |
| --- | --- | --- | --- |
| Codex | App Server `account/rateLimits/read` | 打开的 session JSONL + App Server 状态 | session JSONL 增量索引 |
| Grok | 额度接口（含套餐名） | 打开的会话文件，退回按工作目录匹配 | unified 日志 |
| Kimi Code | `GET {base}/usages`（含 booster 钱包对账） | 打开的 `state.json` / `wire.jsonl` | wire 日志 |
| DeepSeek Harness | 无本地额度窗口 | 打开的 `session.lock` | projcache |
| Command Code | `/alpha/whoami`、`/alpha/billing/*`、`/alpha/usage/summary` | 打开的会话 JSONL，退回按工作目录反查 | 会话 JSONL |
| Claude Code | OAuth usage 接口（5 小时 / 周 / Design 窗口） | 打开的会话 JSONL（只读文件头部） | 会话 JSONL 的 `message.usage` |

共同口径：

- 每个数据目录独立初始化：目录不存在跳过，读取失败只记日志，不影响其他
  provider；没有任何账号时 daemon 仍可启动（只监控流量与磁盘）。
- 会话识别不读取提示词或工具输出；API Key / token 只用于鉴权请求，不写入
  日志、返回值或 Dashboard。
- Kimi 的 access token 过期时按官方相同的目录锁协议刷新并原子写回；
  各配额接口都有缓存（成功 60 秒、失败 15 秒），避免轮询反复请求。
- Claude Code 额度读 `/api/oauth/usage`（Claude Code `/usage` 命令同款、未公开
  接口，上游可能变动）：Linux / Windows 读 `~/.claude/.credentials.json`，
  macOS 读 Keychain 的「Claude Code-credentials」；access token 过期不主动刷新
  （交给 Claude Code 自己刷新），接口限速激进，所以成功缓存 5 分钟、失败缓存
  1 分钟，被限速或读取失败时只展示账号身份，本地用量统计不受影响。

内置单价覆盖 GPT-6 系列（`gpt-6-astra/sol/luna`）、小米 MiMo、智谱 GLM
（`glm-5.3` 系列）与阶跃星辰（`step-5-preview`）；聚合商前缀、大小写和
官方快照后缀都能命中同一份价格。Claude Code 金额按 Anthropic 公开 API 单价
换算，缓存写按 1.25× 输入价估算。

## 部署与后台服务

### 运行环境

- **Python ≥ 3.10**，只用标准库。macOS 自带的 `python3` 通常是 3.9，需要
  Homebrew / python.org / conda 的 3.10+；Windows 用 python.org、
  Microsoft Store 或 conda 均可。
- **SQLite** 的 JSON1 扩展用于加速聚合；缺失时自动退回 Python 侧聚合，
  功能不受影响，只是检索慢一些。
- 状态目录自动创建为 `0700`，锁文件与配置文件为 `0600`；daemon 通过文件锁
  保证同一状态目录只有一个实例。

### 平台能力对照

| 能力 | Linux | macOS | Windows |
| --- | --- | --- | --- |
| 额度查询、用量索引与检索、告警、磁盘与会话管理、Dashboard、健康检查 | ✅ | ✅ | ✅ |
| 活动会话与进程证据 | ✅ `/proc` | ✅ `ps` + `lsof` | ✅ Toolhelp32 + Restart Manager |
| 异常流量字节统计 | ✅ netlink `INET_DIAG` | ⚠️ 只列进程与连接，不统计字节 | ⚠️ 同 macOS |
| 后台服务 | systemd 用户服务 | launchd LaunchAgent | 计划任务（`schtasks`，登录时启动） |
| 单实例锁 | `flock` | `flock` | `msvcrt.locking` |

### Linux（含 WSL2）

```bash
a-token-monitor service install --dashboard --dashboard-port 8765
a-token-monitor service status
a-token-monitor service logs --lines 100
a-token-monitor service restart
a-token-monitor service stop
a-token-monitor service uninstall
```

WSL2 需要先在 `/etc/wsl.conf` 里启用 `systemd=true` 并重启发行版。
systemd 单元保存在 `~/.config/systemd/user/a-token-monitor.service`。

### macOS

`service` 子命令参数与 Linux 完全一致，自动写入 LaunchAgent：

```bash
a-token-monitor service install --dashboard --dashboard-port 8765
a-token-monitor service status
a-token-monitor service logs --lines 100
a-token-monitor service uninstall
```

- LaunchAgent 位于 `~/Library/LaunchAgents/com.a-token-monitor.daemon.plist`，
  日志写入 `~/.a-token-monitor/launchd.log`。
- `service plist` 打印当前平台的服务定义，便于审查或手工安装。
- 没有 netlink：流量面板与 `traffic` 退化为只显示进程与远端连接（不统计
  字节、不产生流量告警），会明确提示原因。
- 活动会话依赖系统自带的 `ps` 与 `lsof`；`lsof` 被裁剪时会话仍按目录识别，
  但缺少「打开了哪个会话文件」的证据。

### Windows

```powershell
pip install a-token-monitor
a-token-monitor service install --dashboard
a-token-monitor service status
a-token-monitor service logs --lines 100
a-token-monitor service uninstall
```

- 计划任务名 `ATokenMonitor`，登录时启动，`LeastPrivilege`，不需要管理员权限；
  配置写回 `<state_dir>\service.json`，任务定义备份在
  `<state_dir>\a-token-monitor-task.xml`。
- 日志写到 `<state_dir>\daemon.log`，`service logs --follow` 用 Python 轮询，
  不依赖 `tail`。
- 计划任务没有 POSIX 式的优雅停止信号：`service stop` 等价于结束进程，
  SQLite 事务与单实例锁由系统回收，下次启动从检查点继续。
- 进程发现走 Toolhelp32 快照 + Restart Manager（反查会话文件持有者）；
  工作目录由会话文件自身的元数据推断。
- 与 macOS 一样没有 netlink，流量功能退化为 process-only。
- 注意事项：
  - Restart Manager 不可用或文件被更高权限进程持有时，个别会话的活动状态
    可能识别不到；额度、用量索引与 Dashboard 不受影响。
  - NTFS 之外的卷（FAT/exFAT、部分网络盘）拿不到稳定的文件 ID，日志轮转
    检测退化为按大小/时间判断。
  - 状态目录的 `chmod 0700/0600` 在 Windows 上只影响只读位，不构成权限隔离，
    需要严格隔离请用 `icacls` 收紧 ACL。
  - 路径超过 260 字符需要系统开启 `LongPathsEnabled`。
  - npm 安装的 `.cmd` 包装 CLI 会自动用 `cmd.exe /c` 启动，进程识别也会去掉
    `.exe/.cmd/.bat` 后缀与 `node .../cli.js` 包装。
  - 正在被 agent 打开的会话文件无法删除：归档/清理会跳过并说明原因，
    不中断整批操作。
  - 容器或无计划任务场景可直接前台运行：`a-token-monitor ... daemon`。

### 容器

- 监控其他 agent 进程需要共享宿主机 PID 命名空间：`docker run --pid=host ...`；
  否则用量索引照常工作，但活动会话、进程证据和流量归属为空。
- 状态目录挂载为可写（如 `-v "$HOME/.a-token-monitor:/state"` 加
  `--state-dir /state`）；agent 数据目录按需只读挂载。
- Dashboard 端口用 `-p 8765:8765` 暴露，容器内需要 `--dashboard-host 0.0.0.0`。
- 没有 systemd/launchd 时直接前台运行 `daemon`；`/healthz` 与 `/readyz`
  可用于容器与反向代理探测。

## 数据与安全

卸载服务不会删除监控数据。状态目录（默认 `~/.a-token-monitor`）内各文件：

| 文件 | 内容 |
| --- | --- |
| `monitor.sqlite3` | 会话登记、额度快照和恢复记录 |
| `usage-index.sqlite3` | 用量索引与增量读取检查点 |
| `traffic-alerts.sqlite3` | 异常流量告警历史 |
| `service.json` | `service install` 保存的 daemon 配置（三个平台一致） |
| `scan-dirs.json` | 设置页保存的 Web 扫描目录覆盖配置 |
| `settings.json` | 设置页保存的 Web 保留天数覆盖配置 |
| `archives/` | 会话归档：`codex-sessions-*.tar.gz` 及 `.manifest.json` |

Dashboard 除页面和只读接口外，只额外接受少数写接口（`POST /api/alerts`、
`POST /api/housekeeping`、设置页的扫描目录与历史数据接口），都要求
`Content-Type: application/json` 并限制请求体大小；其余路径的 POST 返回 405。
页面默认没有鉴权，使用 `--dashboard-host 0.0.0.0` 暴露到局域网时请确认
网络内的其他设备可信。

## 开发验证

```bash
python -m pytest tests/ -q    # 或 python -m unittest discover -s tests -v
ruff check src tests
```

CI 在 Linux / macOS / Windows × Python 3.10 / 3.13 全矩阵跑同一套测试；
平台专有能力（符号链接、POSIX 权限位、`/proc`）由测试装饰器显式跳过。

## 许可证

GNU General Public License v3.0 或更新版本（`GPL-3.0-or-later`），完整条款见
[LICENSE](LICENSE)。

Copyright (C) 2026 pantlive

可以自由使用、修改和分发本程序；但分发本程序或其修改版时，必须同样以 GPL
授权，并一并向接收者提供完整源码，不得附加额外限制。
