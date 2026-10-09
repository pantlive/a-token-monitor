# 贡献指南

## 开发环境

项目运行时零第三方依赖，开发工具只装在本地和 CI：

```bash
python -m pip install -e .
python -m pip install ruff pytest coverage "mypy==1.13.0"

ruff check src tests          # 代码风格
python -m pytest -q           # 全量测试（约 45 秒）
python -m mypy                # 类型检查，范围见 pyproject.toml 的 [tool.mypy]
python -m coverage run -m pytest -q && python -m coverage report   # 覆盖率，门槛 78%
```

CI 在 Linux / macOS / Windows × Python 3.10 / 3.13 上跑 ruff 和 pytest；覆盖率与类型检查只在
ubuntu / 3.13 跑一次。平台不具备的能力（符号链接、`/proc`、POSIX 权限位）由
`tests/_platform_support.py` 的装饰器显式跳过，新用例用到这些能力时也要加上对应装饰器。

## 代码结构

| 位置 | 内容 |
|---|---|
| `providers.py` | agent 注册表：每个 agent 的目录、命令行参数、账号/额度/活动会话读取 |
| `local_time.py` | 本地时区的唯一入口 |
| `cli/` | 命令行：`parser` 参数定义，`main` 分派，每个子命令一个模块 |
| `usage/` | 用量索引：解析、SQLite 索引、检索、习惯分析、`UsageAggregator` |
| `dashboard/` | 网页：`static/` 前端资源、`handler` 路由、`state` 状态构建 |
| `alert_context/` | 告警上下文：`lookup` 分派，每个 agent 一个模块 |
| `claude.py`、`grok.py`、`kimi.py`、`local_agents.py` 等 | 各 agent 的目录解析、会话发现与日志解析 |

## 新增一个 agent

1. 在对应模块实现目录解析（`default_xxx_home` / `resolve_xxx_homes`）、会话日志解析，
   以及需要的活动会话探测、账号与额度读取。
2. 在 `providers.py` 的 `PROVIDER_SPECS` 登记一条 `ProviderSpec`。命令行参数、设置页、
   磁盘统计、活动会话保护、`service.json` 字段、Dashboard 账号卡片都由注册表生成。
3. 接入各功能的分派表，直到 `tests/test_providers.py` 通过：进程识别
   （`agents._AGENT_BINARIES`、`PRODUCT_LABELS`）、告警上下文（`alert_context/lookup.py` 的
   `_SOURCES`）、归档规则（`housekeeping._SESSION_SPECS`）。确实不适用的组合写进该测试的
   `EXEMPT` 并说明原因。

各层之间传递 `homes` 映射（`{provider key: 目录元组}`），不要再为某个 agent 单独加
`xxx_homes` 参数。注意配置键与产品 ID 可能不同：Command Code 的配置键是 `commandcode`，
产品 ID 是 `command-code`。

## 约定

- **本地时间一律经 `local_time`**：`to_local`、`local_day_key`、`local_day_start`、
  `local_naive_to_timestamp` 等。不要直接用 `datetime.fromtimestamp(ts)`、`.astimezone()`、
  `time.localtime()` 或 SQLite 的 `'localtime'`，`tests/test_local_time_usage.py` 会拦下这些
  写法；`datetime.strptime(...).timestamp()` 这类按本地时间解析的写法它拦不住，同样要改用
  `local_naive_to_timestamp`。测试通过 `tests/conftest.py` 把时区固定为 UTC+8。
- **共享用量索引**：`usage-index.sqlite3` 由 daemon 与一次性命令共享。一次性命令创建
  `UsageAggregator` 时必须传 `prune_stale=False`，否则会按自己的扫描范围删掉 daemon 的索引。
- **不吞异常**：可选功能（额度、单个 provider）失败不能拖垮监控，但必须留下日志；
  可能接触凭据的路径，正文只记异常类型与 `sanitize_error` 后的信息，堆栈放 debug 级别。
- **中英 README 同步**：`README.md`（英文）与 `README.zh-CN.md`（中文）结构必须一一对应，
  `tests/test_readme.py` 会检查章节、代码块和命令。
- **测试替换注册表而不是模块函数**：读取函数经 `PROVIDER_SPECS` 查找，测试用
  `tests/_provider_patch.py` 的 `patch_provider` 替换注册表条目。

## 提交信息

标题用 `[feat]` / `[fix]` / `[refactor]` / `[docs]` 前缀加一句英文概述，正文说明动机与行为变化。
