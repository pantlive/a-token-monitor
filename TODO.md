# 待办事项

## 功能

- [ ] 历史数据管理
  - 支持分别配置用量、告警和会话历史的保留天数。
  - 在 Dashboard 展示状态目录及各类索引的磁盘占用。
  - 提供清理预览，明确将删除的数据范围和预计释放空间。
  - 支持安全清理与索引压缩，不影响仍在活动的会话和增量读取检查点。
  - 后台自动清理失败时记录原因并在 Dashboard 提示。
  - 说明：告警历史的保留天数（`--alert-retention-days`）和删除前条数预览已完成，
    用量与会话历史的保留策略仍待实现。

- [ ] Daemon 健康检查
  - 提供轻量的 `/healthz` 存活检查和 `/readyz` 就绪检查端点。
  - 返回各 provider、用量索引器、流量采集器和 Dashboard 的独立状态。
  - 包含最后成功时间、最近错误和数据是否过期，不暴露凭据及敏感路径。
  - 使用明确的 HTTP 状态码，方便 systemd、DevDeck、容器和反向代理探测。
  - Dashboard 展示降级组件，单个非关键 provider 故障不应误判整个 daemon 已停止。

- [ ] 告警详情关联会话
  - 将异常流量告警关联到触发时对应的 Agent、活动会话和项目。
  - 在告警详情中展示进程、模型、远端地址、上传量及触发规则等元数据。
  - 支持从历史告警跳转到对应会话，并保留会话结束后的关联快照。
  - 无法唯一匹配时明确展示候选会话或“未关联”，避免错误归属。
  - 仅保存关联所需元数据，不读取或保存网络传输内容及对话正文。

- [ ] Web 端自定义扫描目录
  - 在 Dashboard 中查看、添加、编辑和移除 Codex 扫描目录，例如 `~/.codex`、`~/.codex-work`。
  - 后续可复用同一界面管理 Grok、Kimi 和 DSH 的数据目录。
  - 校验目录是否存在、是否可读及是否符合对应 provider 的目录结构。
  - 将配置持久化到状态目录，并明确区分命令行参数与 Web 配置的优先级。
  - 修改后安全重载相关 provider，无需重启整个 daemon，且不丢失索引检查点。
  - 限制可配置路径和敏感信息回显，避免通过 Web 任意浏览服务器文件系统。

## 已完成

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
    Dashboard 告警区与活动会话表、`token-monitor sessions` 和 daemon 日志同步提示；
    阈值可用 `--session-turn-warn` / `--session-context-warn-tokens` 调整。
  - 统计 Codex / Grok / Kimi / DeepSeek Harness / Command Code 数据目录和状态目录
    占用（含一级子目录排行），单目录 5 GiB / 合计 10 GiB 阈值提醒并可配置。
  - 会话归档打包 tar.gz + manifest（含 sha256 与用量摘要）后删除原文件，可恢复；
    也支持直接清理；两者都先预览、显式确认，并跳过活动会话与过新文件。
  - Dashboard「磁盘与会话管理」区、`token-monitor disk` 与
    `sessions --archive/--clean/--restore` 提供同样的能力。
  - v1 只归档/清理 Codex session JSONL；其他 agent 目录只统计和提醒。

- [x] token 用量历史检索
  - 直接检索用量索引里的 token 记录，按日期、模型、会话（含项目路径关键词）筛选。
  - Dashboard 新增「用量检索」区：会话明细 / 按日期汇总 / 按模型汇总三种视图，
    支持最近活动、token 用量、估算金额排序与翻页，可点击会话 ID 下钻。
  - `GET /api/usage/search` 提供同等的查询能力，单次扫描上限 20 万条并返回截断标记。
  - `token-monitor usage` 命令行支持 `--days` / `--from` / `--to` / `--model` /
    `--session` / `--project` / `--query` / `--group` / `--sort` / `--json`。
  - 只读取 token 元数据，不读取提示词或工具输出等对话内容。

- [x] 异常流量告警落盘与 Web 通知
  - 告警写入状态目录的 `traffic-alerts.sqlite3`，daemon 重启后仍可查询。
  - Dashboard 新增「告警历史」区，展示实时通知与历史告警，KPI 卡片显示未读数。
  - 支持已读状态、时间段 / 级别 / 规则 / 关键词筛选、单条与批量清理，以及
    5 分钟合并窗口内的重复告警合并；`--alert-retention-days` 控制保留天数。
  - `token-monitor alerts` 提供同等的命令行查询、已读和清理能力。
  - 暂不接入邮件、Telegram、企业微信等外部通知渠道。

- [x] Command Code 订阅额度与账号管理
  - 通过官方后台接口读取身份、套餐、5 小时 / 每周窗口和本月订阅扣费。
  - 支持 `--commandcode-home` 多数据目录，并随 `service install` 持久化。
  - Dashboard 展示 Command Code 账号卡片、额度窗口、活动会话与用量对账。
  - API Key 只用于鉴权请求，不写入返回值、日志或 Dashboard。
