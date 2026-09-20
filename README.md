# codex-reset-monitor

本地 code agent 监控器。它读取 Codex / Grok / Kimi 账号额度、扫描正在运行的
Codex session JSONL，并在网页中展示账号、额度窗口、活动会话、用量估算，以及
Codex CLI、Grok CLI、Kimi Code、DeepSeek Harness、Claude Code、OpenCode
等进程的异常流量。超出阈值的异常大上传会在 Dashboard 顶部告警。

当前版本只负责观察和统计，不会因为额度状态启动新的 Codex 任务，也不提供
额度中断后的自动处理入口。

## 安装

需要已经安装并登录 Codex CLI。项目只使用 Python 标准库，依赖环境按 conda 管理：

```bash
conda env create -f environment.yml
conda activate codex-reset-monitor
```

也可以直接使用当前 Python：

```bash
conda run -n codex-reset-monitor python -m pip install -e .
```

## 使用

全局参数放在子命令之前。默认状态目录是 `~/.codex-reset-monitor`，默认账号是
`~/.codex`。`--codex-home` 可以重复传入多个账号。

```bash
# 查看当前账号额度
codex-reset-monitor \
  --state-dir "$HOME/.codex-reset-monitor" \
  quota --json

# 单次发现活动会话
codex-reset-monitor \
  --state-dir "$HOME/.codex-reset-monitor" \
  sessions --json

# 扫描本机 code agent 异常流量（两次采样之间默认隔 1 秒）
codex-reset-monitor traffic --json

# 持续监控两个账号并启动网页 Dashboard
codex-reset-monitor \
  --state-dir "$HOME/.codex-reset-monitor" \
  --codex-home "$HOME/.codex" \
  --codex-home "$HOME/.codex-work" \
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

「账号与额度」区以看板卡片并列展示各账号的额度窗口；卡片内的活动会话默认
折叠为摘要按钮，点击「展开 N 个活动会话」再展开完整会话表，「收起会话列表」
恢复折叠，折叠状态在 5 秒自动刷新之间保持。

## 监控内容

- 额度查询使用 Codex App Server 的 `account/rateLimits/read`，不会发送模型提示词。
- 每个 `CODEX_HOME` 使用独立的登录状态、session 范围和额度快照。
- 活动会话以实际打开 JSONL 的进程和 App Server 会话状态为依据。
- JSONL 按字节偏移增量读取，避免重复扫描大型历史文件。
- 用量页按需汇总 Codex session JSONL、Grok unified 日志、Kimi wire 日志和
  DeepSeek Harness projcache 合计。
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
- 异常流量监控扫描本机 `codex` / `grok` / `kimi` / `dsh` / `claude` /
  `opencode` 等进程及其子进程的已建立 TCP 连接，使用内核 `tcp_info` 的
  `bytes_sent` 计算外发增量。回环地址（本机 Web UI）不计入外发告警；
  新连接第一次只记基线，避免把监控启动前的历史流量当成突发上传。
  DeepSeek Harness 的 Web UI（默认 `:3080`）把会话推给浏览器也不计入外发告警。
  默认 15 秒内外发 8 MiB 黄色告警、32 MiB 红色告警；5 分钟累计 64 MiB /
  256 MiB 同样分级。可用 `--upload-warn-mb`、`--upload-alert-mb`、
  `--upload-window-warn-mb`、`--upload-window-alert-mb` 调整。只记录
  进程、工作目录、对端地址和字节数，不读取连接内容。

金额是 OpenAI API 等价值估算，不代表 Plus 或其他订阅的实际账单。模型没有已知
API 单价时仍展示 token，但不会计入金额合计。

## WSL 后台服务

在已启用 systemd 的 WSL2 中，可以把监控器安装为当前用户的后台服务：

```bash
codex-reset-monitor \
  --state-dir "$HOME/.codex-reset-monitor" \
  --codex-home "$HOME/.codex" \
  service install \
  --dashboard \
  --dashboard-host 0.0.0.0 \
  --dashboard-port 8765
```

常用管理命令：

```bash
codex-reset-monitor --state-dir "$HOME/.codex-reset-monitor" service status
codex-reset-monitor --state-dir "$HOME/.codex-reset-monitor" service logs --lines 100
codex-reset-monitor --state-dir "$HOME/.codex-reset-monitor" service restart
codex-reset-monitor --state-dir "$HOME/.codex-reset-monitor" service stop
codex-reset-monitor --state-dir "$HOME/.codex-reset-monitor" service start
codex-reset-monitor --state-dir "$HOME/.codex-reset-monitor" service uninstall
```

服务配置保存在 `~/.codex-reset-monitor/service.json`，systemd 单元保存在
`~/.config/systemd/user/codex-reset-monitor.service`。卸载服务不会删除监控数据库
或用量索引。

## DevDeck

DevDeck 可以将本项目配置为一个后端服务。工作目录填写项目根目录，启动命令使用：

```bash
PYTHONPATH=/path/to/codex-reset-monitor/src \
python -u -m codex_reset_monitor \
  --state-dir "$HOME/.codex-reset-monitor" \
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
