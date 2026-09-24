# 待办事项

## 功能

- [ ] 告警详情关联会话
  - 将异常流量告警关联到触发时对应的 Agent、活动会话和项目。
  - 在告警详情中展示进程、模型、远端地址、上传量及触发规则等元数据。
  - 支持从历史告警跳转到对应会话，并保留会话结束后的关联快照。
  - 无法唯一匹配时明确展示候选会话或“未关联”，避免错误归属。
  - 仅保存关联所需元数据，不读取或保存网络传输内容及对话正文。

## 已完成

- [x] Dashboard 白天模式
  - 主题在「跟随系统 → 白天 → 夜间」之间循环，选择存 `localStorage`，默认跟随系统；
    首屏内联脚本在样式解析前写入 `data-theme`，切换与刷新都不闪屏。
  - 把原先散落在 CSS 里的 39 处硬编码颜色（状态色、边框、遮罩、图表轨道、阴影）
    收敛成 63 个语义变量，白天模式只覆盖同一组变量，组件与图表无需逐个适配。
  - 新增 3 个回归用例：白天模式必须覆盖全部变量、主题块之外不允许硬编码颜色、
    切换入口与系统偏好监听齐全。

- [x] 跨平台部署：Windows
  - P0 安全修复：存活探测改用 `OpenProcess` + `GetExitCodeProcess`（Windows 上
    `os.kill(pid, 0)` 会直接结束目标进程）、`SIGKILL` 缺失时退回 `SIGTERM`、
    `identify_agent` 兼容 `.exe/.cmd/.bat/.ps1` 与 `node …/cli.js` 包装、
    npm 的 `.cmd` 包装改用 `cmd.exe /c` 启动、CLI 输出固定 UTF-8。
  - P1 进程后端：Toolhelp32 快照拿进程树/镜像路径/启动时间，PEB 读命令行，
    Restart Manager 反查「谁持有会话文件」（不需要管理员）；TCP 连接用
    `GetExtendedTcpTable`，没有字节数，因此与 macOS 一样退化成 process-only。
  - P2 后台服务：`TaskSchedulerServiceManager`（`schtasks`，登录时启动、
    `LeastPrivilege`、日志写 `<state_dir>\daemon.log`），`service plist`
    打印计划任务 XML，卸载移除任务与 `service.json`。
  - P3/P4：README 增加 Windows 章节与三平台能力对照表；`tests/_platform_support.py`
    提供符号链接/POSIX 权限守卫，GitHub Actions 增加 Linux/macOS/Windows 矩阵。

- [x] 跨平台部署：Linux + macOS
  - 新增 `process_backend`：Linux 走 `/proc`，macOS 改用系统自带的 `ps` + `lsof`
    获取进程树、工作目录与打开的会话文件，结果带秒级缓存避免一轮刷新重复 fork；
    会话适配器、Codex 进程扫描与流量进程归属统一走这个后端。
  - macOS 没有 netlink（`INET_DIAG`）：`traffic` 不再抛 `AttributeError`，而是降级成
    process-only（列出 agent 进程与 `lsof` 远端连接，不统计字节、不产生流量告警），
    CLI 与 Dashboard 都会说明原因。
  - `service` 子命令按平台选择实现：Linux systemd 用户服务 / macOS launchd
    LaunchAgent（plist 由 `plistlib` 生成、日志写 `<state_dir>/launchd.log`），
    新增 `service plist` 打印当前平台服务定义；两端卸载都会移除服务定义与 `service.json`。
  - README 新增部署章节：运行环境（Python ≥3.10、SQLite JSON1 降级、Codex 可选）、
    平台能力对照表、Linux/WSL2、macOS 与容器（`--pid=host`、端口/状态目录挂载）说明。

- [x] 历史数据管理
  - 用量索引、会话历史、异常流量告警分别配置保留天数：
    `--usage-retention-days`（默认 90 天）、`--session-retention-days`（默认 30 天）、
    `--alert-retention-days`（默认 30 天），daemon 与 service install 均支持并随
    `service.json` 持久化；Dashboard 设置页「历史数据」子块可在线修改
    （Web 配置优先于命令行参数，保存于 `settings.json`，热生效无需重启）。
  - 设置页展示状态目录及各类索引/数据库的磁盘占用（含 wal/shm 兄弟文件）。
  - 清理预览明确将删除的数据范围和预计释放空间；确认后立即清理，
    删除后对数据库做 VACUUM 压缩。
  - 清理只删除过期历史行：不影响仍在活动的会话和用量索引的增量读取检查点，
    额度恢复记录始终保留。
  - daemon 每天按生效保留期自动清理；失败时记录原因并通过健康组件徽标在
    Dashboard 顶栏提示。

- [x] Daemon 健康检查
  - `GET /healthz` 存活检查：主循环在 `max(2×扫描间隔, 120s)` 内有心跳返回 200，
    卡死或未启动返回 503 `stuck`；`GET /readyz` 就绪检查：关键组件（主循环）失败或
    未就绪返回 503 `not_ready`，非关键组件故障不影响状态码。
  - 组件级状态覆盖主循环、各 Codex 账号、各 provider、流量采集器、用量索引器和
    housekeeping，包含最后成功时间、最近错误（主目录脱敏为 `~`）和数据是否过期。
  - Dashboard 顶栏显示「正常 / 启动中 / 部分降级 / 异常」指示，点击展开异常组件
    与最近错误；`/api/state` 携带同样的 `health` 摘要。
  - 单个账号或 provider 异常不再中断整轮监控；后台索引线程单轮失败不再退出，
    错误体现在组件状态里。

- [x] Web 端自定义扫描目录
  - Dashboard 新增「扫描目录」面板，可查看、添加和移除全部六个 provider
    （Codex / Grok / Kimi Code / DeepSeek Harness / Command Code / Claude Code）
    的数据目录。
  - 保存前校验目录存在、可读、位于主目录之内且符合 provider 结构；
    拒绝 `~/.ssh` 等敏感目录和监控状态目录本身。
  - 配置持久化到状态目录的 `scan-dirs.json`；优先级为 Web 配置 > 命令行参数 >
    自动探测，清空目录表示显式禁用该 provider，「重置」回退命令行/自动探测。
  - 修改后热重载相关 provider，无需重启 daemon，不丢失索引检查点；
    一次性命令在配置损坏时降级为命令行/自动探测并给出警告。
  - 网页不提供任意路径浏览，只回显已配置目录的校验状态。

- [x] 统一 Agent 会话模型
  - `TrackedSession` 承载统一字段：账号、产品、项目、模型、状态、token、
    开始时间与最后活动时间（`started_at` / `last_activity_at` / `resolved_project`）。
  - 各 provider 的发现与解析收敛在适配器里并统一填这组字段：Grok 不再把模型塞进
    `metadata`，Claude Code 新增活动会话适配器（打开文件识别 + 文件头部元数据）。
  - 合并了 Dashboard 与 CLI 各自重复的 `_session_summary` / `_display_session_error`，
    改为共用 `session_view()`；token / 上下文 / 轮数由 `enrich_session_views()` 统一回填。
  - 公共聚合与 Dashboard 只依赖统一模型，Grok / Claude Code 的接入没有产生平行结构。

- [x] Daemon 不强制 Codex 账号
  - 没有 Codex CLI、`CODEX_HOME` 或有效登录时仍能启动 daemon，Codex 账号变为可选配置。
  - 支持仅监控流量，或监控 Grok / Kimi / DeepSeek Harness / Claude Code / Command Code
    的任意组合；`MultiAccountMonitor` 零账号时 `registries` 为空，Dashboard 照常提供状态。
  - provider 目录不存在直接跳过；读取失败只降级该目录（Dashboard 状态与 `sessions`、
    `quota` 命令均按 provider 隔离，日志记录失败原因）。
  - `service install` 允许 `codex_homes: []` 并保持配置往返；没有账号时不解析
    Codex 可执行文件，缺省命令名原样保存。

- [x] Grok 活动会话与 Kimi / DSH 对齐
  - 以 `/proc/<pid>/fd` 里 Grok CLI 实际打开的会话文件为准识别活动会话，
    进程不持有句柄时按工作目录回退匹配。
  - 关联账号（GROK_HOME 身份）、项目目录（URL 编码目录名还原）和 session，
    并从 `summary.json` 取模型与创建/更新时间。
  - 同一会话被多个进程打开时合并 pids，进程退出后自动从列表消失，
    会话目录被轮转/移动后仍按路径报告。
  - 使用统一 `TrackedSession` 字段进入 Dashboard 与会话表，thread_id 前缀去重。

- [x] Claude Code 用量索引
  - 从 `~/.claude/projects/**/*.jsonl` 增量读取 token 数据，只读 `message.usage` 等元数据。
  - 纳入按日、模型、项目和会话的用量与 Anthropic API 等价成本；新增 Claude 模型单价。
  - 接入 Dashboard 用量趋势、模型/项目/会话统计与习惯分析，并支持用量检索。
  - 同一 `message.id` 去重、子代理目录按内联侧链检测决定是否索引、断点续扫与日志轮转；
    新增 15 个脱敏样本用例。

- [x] 性能优化（用量检索 / 目录扫描 / 索引刷新 / 归档）
  - 用量检索下推到 SQL 聚合并按长上下文标记分桶，结果缓存 30 秒：
    近 30 天 1.2s → 0.13s，全部历史 2.1s → 0.21s（8 万条记录）。
  - 索引新增 `long_context` 列，旧索引首次打开就地回填，不重读 JSONL。
  - 目录扫描改为单次遍历同时得到总量与一级子目录：63ms → 32ms。
  - daemon 的用量索引刷新移到后台线程，主循环不再做磁盘 I/O。
  - 网页归档/清理改为后台任务并轮询进度，提交立即返回。

- [x] 单会话归档入口
  - 活动会话表每个已结束会话带「归档此会话」按钮，后台执行并显示进度，可先预览原因。
  - 会话表保留最近 24 小时内结束的会话，运行中和刚写入过的会话不可归档并给出原因。
  - 命令行等价：`sessions --archive --session <会话 ID 或路径>`。

- [x] 会话卫生：长会话提醒、磁盘占用提醒、归档与清理
  - 活动会话轮数 ≥ 100 或最近一次上下文 ≥ 200k token 时提醒切换新会话，
    Dashboard 告警区与活动会话表、`a-token-monitor sessions` 和 daemon 日志同步提示；
    阈值可用 `--session-turn-warn` / `--session-context-warn-tokens` 调整。
  - 统计 Codex / Grok / Kimi / DeepSeek Harness / Command Code 数据目录和状态目录
    占用（含一级子目录排行），单目录 5 GiB / 合计 10 GiB 阈值提醒并可配置。
  - 会话归档打包 tar.gz + manifest（含 sha256 与用量摘要）后删除原文件，可恢复；
    也支持直接清理；两者都先预览、显式确认，并跳过活动会话与过新文件。
  - Dashboard「磁盘与会话管理」区、`a-token-monitor disk` 与
    `sessions --archive/--clean/--restore` 提供同样的能力。
  - v1 只归档/清理 Codex session JSONL；其他 agent 目录只统计和提醒。

- [x] token 用量历史检索
  - 直接检索用量索引里的 token 记录，按日期、模型、账号、会话（含项目路径关键词）筛选。
  - Dashboard 新增「用量检索」区：会话明细 / 按日期汇总 / 按模型汇总 / 按账号汇总
    四种视图，支持最近活动、token 用量、估算金额排序与翻页，可点击会话 ID 下钻。
  - `GET /api/usage/search` 提供同等的查询能力，单次扫描上限 20 万条并返回截断标记。
  - `a-token-monitor usage` 命令行支持 `--days` / `--from` / `--to` / `--model` /
    `--session` / `--project` / `--account` / `--query` / `--group` / `--sort` / `--json`。
  - 只读取 token 元数据，不读取提示词或工具输出等对话内容。

- [x] 额度窗口统一成固定行
  - 每张账号卡片固定渲染「5 小时 / 周 / 月」三行，缺的周期显示虚线「不适用」，
    订阅完全没有额度窗口时给一行说明；三态（有数据 / 不适用 / 待采集）区分明确。
  - 周期口径由 `quota_period()` 统一：窗口时长优先、窗口名兜底，Dashboard 与
    命令行共用；上游缺时长的月窗口（Kimi / Command Code）也能正确归到「月」。
  - 同一周期多条窗口取最紧的一条 + 「另有 N 条」角标（Kimi 的两个月窗口）。
  - 行布局两行式（周期/进度条/百分比 + 重置与时长），窄卡片不再把重置文案挤出边界。
  - 没有任何额度窗口的订阅（DeepSeek Harness、Claude Code）整张卡片不显示，
    「N 个账号」与 hero 徽标只算显示出来的订阅，全空时给空状态说明；
    这些 provider 的用量仍计入用量统计。

- [x] 账号卡片以订阅类型为主标题
  - 「账号与额度」卡片标题改为 `产品 · 套餐`（Codex · Plus / Codex · Pro Lite /
    Grok · SuperGrok / Command Code · GOAT），账号 ID 与 profile 降为次要信息行。
  - Codex 的套餐从 `auth.json` 的 `id_token` → `chatgpt_plan_type` claim 读取，
    按 mtime 缓存，不解析或记录令牌内容；扫描目录元数据里的值优先，额度快照
    缺失 plan_type 时由账号元数据补齐（Dashboard 与 CLI 共用同一份字段）。
  - DSH 不再把模型名塞进 `plan_type`（模型仍在 metadata.model），避免出现
    「DeepSeek Harness · deepseek/deepseek-v4.1-flash」这种伪套餐。
  - 修掉多账号串味：套餐原先在外层循环算好、被内层循环复用，导致两个 Codex
    账号都显示成最后一个账号的套餐；改为把值挂在各自的 state 上，并补了会在
    旧代码上失败的回归用例（两个账号 plus / prolite）。
  - 套餐优先级调整为 auth.json 优先、账号启动快照兜底：续费或换号后不必重启 daemon。

- [x] Dashboard 浏览器标签图标
  - `/favicon.svg` + `/favicon.ico`（16/32 PNG 回退）两个路由，Dashboard 与设置页
    共享同一段 `<link rel="icon">` 注入。
  - 图标几何只定义一次（渐变圆角徽章 + 图形原语），同一份几何生成矢量 SVG
    并用 zlib / struct 光栅化成 PNG/ICO，不依赖图像库、不写外部文件、不请求站外资源。
  - 图标 URL 带几何指纹并按内容长期缓存（页面与 /api 仍为 no-store），改图标即换 URL，
    避免浏览器把「此页面没有图标」长期缓存成空白标签。
  - 侧栏品牌图形换成同一份几何（原先页面内还是旧的紫色「文档」图形，标签页与页面不一致），
    两处共用 _icon_svg()，测试会比对两边的矩形与渐变色，防止再次画歪。
  - 图标引擎升级为几何原语（圆角矩形 / 圆 / 圆环 / 圆头线段 / 圆弧 / 折线 / 多边形 +
    挖洞），用 Stitch MCP 生成设计稿后落地五个候选方案，默认「用量柱 + 高水位刻度孔」；
    光栅化与 SVG 共用同一份原语，逐原语的点包含判定都有单测。
  - 最终选型「成本盾牌 + 脉搏线」（Stitch 方案里 16px 可读性最好的一个）：侧栏 logo 与
    标签页图标同时切换；测试会逐像素校验五个候选各自的图形/渐变/圆角透明，并用
    真实浏览器比对 SVG 与自研光栅化的差异（平均 1.6/255，仅抗锯齿边缘 0.3% 像素有差异）。
  - 图标配色对齐页面调色板：徽章渐变改为 violet（页面主色）→ cyan（状态色），
    解决「图标是数据蓝青、页面主色却是 violet」的双主色错位；页面配色本身不动，
    避免重校 21 处 violet token 的两套主题对比度。

- [x] 用量与成本估算按账号统计
  - 「用量与成本估算」区新增「按账号 / 按模型 / 按项目」统计维度切换：三种维度
    由同一份模型级叶子聚合，切换维度不改变合计口径，表尾给出合计行、每行给出
    金额占比；新增「账号成本排行 Top 5」（金额 + 占比），项目排行同样受筛选约束。
  - 筛选条件扩展为账号、模型、项目三者可叠加，账号下拉覆盖当前时间范围的全部账号。
  - 账号行标注产品来源（Codex / Grok / Kimi Code / Claude Code / DeepSeek Harness），
    统计口径仍是真实账号 ID 优先归并，同一账号跨产品只占一行。
  - 索引新增 `usage_file_account` 文件级账号表（每个索引轮次覆盖写入，不重读 JSONL、
    不动 token 明细），用量检索因此能在 SQL 层按账号 JOIN 与筛选；旧索引自动建表，
    补齐前按「未知账号」归组而不是丢数据。

- [x] 异常流量告警落盘与 Web 通知
  - 告警写入状态目录的 `traffic-alerts.sqlite3`，daemon 重启后仍可查询。
  - Dashboard 新增「告警历史」区，展示实时通知与历史告警，KPI 卡片显示未读数。
  - 支持已读状态、时间段 / 级别 / 规则 / 关键词筛选、单条与批量清理，以及
    5 分钟合并窗口内的重复告警合并；`--alert-retention-days` 控制保留天数。
  - `a-token-monitor alerts` 提供同等的命令行查询、已读和清理能力。
  - 暂不接入邮件、Telegram、企业微信等外部通知渠道。

- [x] Command Code 订阅额度与账号管理
  - 通过官方后台接口读取身份、套餐、5 小时 / 每周窗口和本月订阅扣费。
  - 支持 `--commandcode-home` 多数据目录，并随 `service install` 持久化。
  - Dashboard 展示 Command Code 账号卡片、额度窗口、活动会话与用量对账。
  - API Key 只用于鉴权请求，不写入返回值、日志或 Dashboard。
