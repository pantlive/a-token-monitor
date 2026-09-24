# a-token-monitor

[![CI](https://github.com/pantlive/a-token-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/pantlive/a-token-monitor/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/a-token-monitor.svg)](https://pypi.org/project/a-token-monitor/)
[![Python](https://img.shields.io/pypi/pyversions/a-token-monitor.svg)](https://pypi.org/project/a-token-monitor/)
[![License: GPL v3+](https://img.shields.io/badge/License-GPL--3.0--or--later-blue.svg)](LICENSE)

[中文](README.md) | English

A local code-agent monitor: it reads the quota windows of Codex / Grok / Kimi /
Command Code and other accounts, discovers running agent sessions, aggregates token
usage and API-equivalent cost, and watches for abnormal upload traffic from Codex CLI,
Grok CLI, Kimi Code, DeepSeek Harness, Command Code, Claude Code, OpenCode and similar
processes. Everything is processed on this machine and shown through an embedded web
Dashboard, or installed as a systemd / launchd / Windows scheduled-task background
service.

This version only observes and reports: it never starts new agent tasks because of a
quota state, and it offers no entry point for automatic handling after a quota
interruption.

## Feature overview

- **Accounts & quotas**: side-by-side cards for each subscription (Codex Plus, Grok
  SuperGrok, Command Code GOAT, …) showing the 5-hour / weekly / monthly windows with
  progress and reset time; the top of the page warns when a quota is exhausted or usage
  passes 90%. Quota queries use the same read-only endpoints as each vendor's official
  CLI and never send model prompts.
- **Active sessions**: based on the session files processes actually hold open
  (`/proc/<pid>/fd`, `lsof`, Windows Restart Manager), showing account, product, project,
  model, status, tokens, start and last-activity time in one table; over-long sessions
  (turn count or context above the thresholds) are flagged with “start a new session”.
- **Usage & cost estimation**: JSONL is indexed incrementally by byte offset (resuming
  from a checkpoint after a restart), aggregating API-equivalent amounts by day / model /
  account / project, with a cost trend chart, cache savings and Top 5 accounts and
  projects by cost; monthly budgets (`--budget-usd`) and progress alerts are supported.
- **Insights**: local statistics for active hours, model cost distribution, conversation
  size and the most expensive conversations, plus rule-based, quantifiable token-saving
  advice. Only token metadata is read, never conversation content.
- **Traffic anomaly monitoring**: per-process outbound TCP bytes (the kernel `tcp_info`
  on Linux) with two-tier thresholds at 15 seconds / 5 minutes and alerts persisted for
  search and read-state tracking; only the process, directory, peer and byte counts are
  recorded, never connection content.
- **Disk & session management**: usage of every agent data directory with threshold
  reminders; Codex sessions can be archived (with manifest verification) or cleaned up,
  one session at a time, and restored from an archive.
- **Settings page**: manage each provider's scan directories from the web (hot reload,
  index checkpoints preserved) along with history retention days (cleanup preview + safe
  cleanup + VACUUM).
- **Health checks**: `/healthz` (liveness) and `/readyz` (per-component readiness), with a
  matching health badge in the Dashboard top bar for systemd, containers and reverse
  proxies.
- **Languages and themes**: UI / API / CLI in Chinese and English (auto-detected or
  forced with `--lang`); light / dark theme follows the system or a manual switch.

Amounts are API-equivalent estimates from OpenAI / Anthropic and similar public pricing
and do not represent your actual subscription bill; models without a known unit price
still show tokens but are left out of money totals.

## Installation

Python ≥ 3.10 is required; only the standard library is used, with no third-party
runtime dependencies:

```bash
pip install a-token-monitor
# or install it isolated with pipx
pipx install a-token-monitor
```

From source (development):

```bash
conda env create -f environment.yml   # or use your current Python
conda activate a-token-monitor
python -m pip install -e .
```

## Usage

Global options go before the subcommand. The default state directory is
`~/.a-token-monitor` and the default account directory is `~/.codex`; `--codex-home`,
`--grok-home`, `--kimi-home` and friends can be repeated for multiple directories.

```bash
# Show the current account quotas
a-token-monitor quota --json

# Discover active sessions once
a-token-monitor sessions --json

# Scan local code-agent processes for traffic anomalies (1 second between samples by default)
a-token-monitor traffic --json

# Query / clear persisted traffic alerts
a-token-monitor alerts --days 7 --unread
a-token-monitor alerts --ack-all
a-token-monitor alerts --clear-before 30 --dry-run

# Search token usage history (by date, model, account, session)
a-token-monitor usage --days 30 --group model --sort tokens
a-token-monitor usage --account account-work --group model

# Inspect agent data-directory usage, disk reminders and archivable sessions
a-token-monitor disk --days 30

# Session archiving (preview first, --yes actually runs it) and restore
a-token-monitor sessions --archive --older-than 30
a-token-monitor sessions --archive --older-than 30 --yes
a-token-monitor sessions --restore ~/.a-token-monitor/archives/codex-sessions-<timestamp>.tar.gz

# Keep monitoring several accounts and start the web Dashboard
a-token-monitor \
  --codex-home "$HOME/.codex" \
  --codex-home "$HOME/.codex-work" \
  --commandcode-home "$HOME/.commandcode" \
  daemon \
  --dashboard \
  --dashboard-port 8765
```

The Dashboard is served at `http://127.0.0.1:8765/` by default; pages and endpoints are
embedded in the daemon, so no separate front-end service is needed.
`--dashboard-host 0.0.0.0` exposes it to the local network (for example to reach WSL from
Windows) — the page has no authentication by default, so make sure the network is
trusted first.

### Common tuning flags

| Flag | Default | Description |
| --- | --- | --- |
| `--budget-usd` | none | Monthly API-equivalent budget; warning at 80%, alert at 100% |
| `--upload-warn-mb` / `--upload-alert-mb` | 8 / 32 | Warning / alert threshold for MiB sent by one process within 15 seconds |
| `--upload-window-warn-mb` / `--upload-window-alert-mb` | 64 / 256 | Warning / alert threshold for MiB accumulated over 5 minutes |
| `--disk-warn-gb` / `--disk-total-warn-gb` | 5 / 10 | Disk reminder thresholds for a single directory / all directories (GiB) |
| `--session-turn-warn` | 100 | Suggest a new session once a session reaches this many turns |
| `--session-context-warn-tokens` | 200000 | Suggest a new session once the latest context reaches this many tokens |
| `--usage-retention-days` / `--session-retention-days` | 90 / 30 | Retention days for the usage index / finished session history (editable in the settings page) |
| `--alert-retention-days` | 30 | Retention days for traffic alerts |
| `--lang en` / `--lang zh` | auto | Force the CLI and Dashboard language |

All of these work on both `daemon` and `service install`.

## Dashboard

- **Accounts & quotas**: one card per subscription, always showing the
  `5 hours / week / month` rows (missing periods are marked “N/A” and cards stay strictly
  aligned); the card title is the subscription type (`product · plan`) while the account
  ID and profile move to a secondary line. Codex plan names are read from the
  `chatgpt_plan_type` claim of `id_token` in the local `auth.json` (the token content
  itself is not parsed); Grok and Command Code use the plan names returned by their own
  quota endpoints, and Claude Code uses the subscription type (Pro / Max, …) stored in
  its local credentials.
- **Usage & cost estimation**: switch between the account / model / project dimensions
  with one set of aggregation rules; filters stack, the table ends with a total row and
  share percentages, and the Top 5 accounts and projects by cost are always shown.
- **Usage search**: query the usage index directly by time range, model, account and
  keyword, with four summary views, paging and per-session drill-down;
  `GET /api/usage/search` exposes the same capability.
- **Alert history / traffic anomalies**: a live process table plus persisted alerts
  searchable by time, severity, rule, read state and keyword, with read-state marking and
  range deletion.
- **Collapse on demand**: “Alert history”, “Usage search” and “Disk & session management”
  start collapsed into a single summary line and only fetch details when expanded; the
  expanded state is remembered in the browser.

### Settings page

A separate `/settings` page with two blocks:

- **Scan directories**: view, add, edit and remove the data directories of each provider
  (Codex / Grok / Kimi Code / DeepSeek Harness / Command Code / Claude Code). Changes are
  hot-reloaded without restarting the daemon and without losing usage-index checkpoints.
  Priority is **web config > CLI flags > auto-detection**, persisted in `scan-dirs.json`
  in the state directory; clearing a provider's directories disables it explicitly, and
  “Reset” falls back to the CLI flags or auto-detection. Safety limits: only directories
  that exist, are readable and live inside the current user's home are accepted;
  sensitive directories such as `~/.ssh` and the state directory itself cannot be
  configured, and the page offers no arbitrary path browsing.
- **History data**: shows the disk usage of the state directory and each index, and edits
  the retention of the usage index (90 days by default) and of session history (30 days
  by default) online, persisted in `settings.json`. Cleanup is previewed first (what will
  be deleted plus the expected space freed); the daemon cleans up automatically every day
  according to the effective retention and VACUUMs afterwards. Cleanup only removes
  expired rows and never touches active sessions or incremental-index checkpoints; a
  failed automatic cleanup records the reason and surfaces it in the Dashboard top bar.
  Alert retention is controlled by `--alert-retention-days` (30 days by default).

### Health checks

- `GET /healthz`: liveness. Returns 200 while the main loop has a heartbeat inside the
  threshold (`max(2 × scan interval, 120s)`); returns 503 when it is stuck or has never
  completed a first pass.
- `GET /readyz`: readiness. Returns 503 when a critical component (the main loop) has
  failed or has not finished starting; the body carries per-component state (last success
  time, redacted latest error). A single non-critical component (one provider, traffic
  collection, the indexer, …) failing only shows as degraded and does not change the
  readiness status code.

The Dashboard top bar shows an “OK / partly degraded / starting / error” badge from these
endpoints; click it for details about the failing components.

## Supported providers

| Provider | Quota | Active-session evidence | Usage source |
| --- | --- | --- | --- |
| Codex | App Server `account/rateLimits/read` | Open session JSONL + App Server state | Incremental session JSONL index |
| Grok | Quota endpoint (including plan name) | Open session files, falling back to working-directory matching | unified logs |
| Kimi Code | `GET {base}/usages` (including booster-wallet reconciliation) | Open `state.json` / `wire.jsonl` | wire logs |
| DeepSeek Harness | No local quota window | Open `session.lock` | projcache |
| Command Code | `/alpha/whoami`, `/alpha/billing/*`, `/alpha/usage/summary` | Open session JSONL, falling back to a working-directory lookup | Session JSONL |
| Claude Code | OAuth usage endpoint (5-hour / week / Design windows) | Open session JSONL (only the file header is read) | `message.usage` in session JSONL |

Shared rules:

- Every data directory is initialized independently: missing directories are skipped and
  read failures are only logged, without affecting other providers; the daemon still
  starts with no accounts at all (it then monitors traffic and disk only).
- Session detection never reads prompts or tool output; API keys / tokens are used only
  for authenticated requests and are never written to logs, return values or the
  Dashboard.
- When a Kimi access token expires it is refreshed with the same directory-lock protocol
  as the official CLI and written back atomically; every quota endpoint is cached
  (60 seconds on success, 15 seconds on failure) so that polling does not repeat requests.
- Claude Code quotas are read from `/api/oauth/usage` (the same undisclosed endpoint the
  Claude Code `/usage` command uses, so it may change upstream): Linux / Windows read
  `~/.claude/.credentials.json`, macOS reads the “Claude Code-credentials” Keychain entry.
  An expired access token is not refreshed here (Claude Code refreshes it itself), and the
  endpoint rate-limits aggressively, so successful results are cached for 5 minutes and
  failures for 1 minute; when it is rate-limited or unreadable, only the account identity
  is shown and local usage statistics are unaffected.

Built-in unit prices cover the GPT-6 family (`gpt-6-astra/sol/luna`), Xiaomi MiMo, Zhipu
GLM (the `glm-5.3` family) and StepFun (`step-5-preview`); aggregator prefixes, letter
case and official snapshot suffixes all resolve to the same price. Claude Code amounts are
converted from Anthropic's public API prices, and cache writes are estimated at 1.25× the
input price.

## Deployment and background service

### Requirements

- **Python ≥ 3.10**, standard library only. The `python3` shipped with macOS is usually
  3.9, so use Homebrew / python.org / conda for 3.10+; on Windows, python.org, the
  Microsoft Store or conda all work.
- SQLite's JSON1 extension speeds up aggregation; when it is missing, aggregation falls
  back to Python — nothing is lost, queries are just slower.
- The state directory is created as `0700` and lock files and configuration files as
  `0600`; the daemon uses a file lock to guarantee a single instance per state directory.

### Platform capability matrix

| Capability | Linux | macOS | Windows |
| --- | --- | --- | --- |
| Quota queries, usage index and search, alerts, disk and session management, Dashboard, health checks | ✅ | ✅ | ✅ |
| Active sessions and process evidence | ✅ `/proc` | ✅ `ps` + `lsof` | ✅ Toolhelp32 + Restart Manager |
| Traffic byte accounting | ✅ netlink `INET_DIAG` | ⚠️ lists processes and connections only, no byte counts | ⚠️ same as macOS |
| Background service | systemd user service | launchd LaunchAgent | Scheduled task (`schtasks`, starts at logon) |
| Single-instance lock | `flock` | `flock` | `msvcrt.locking` |

### Linux (including WSL2)

```bash
a-token-monitor service install --dashboard --dashboard-port 8765
a-token-monitor service status
a-token-monitor service logs --lines 100
a-token-monitor service restart
a-token-monitor service stop
a-token-monitor service uninstall
```

WSL2 needs `systemd=true` in `/etc/wsl.conf` followed by a restart of the distribution.
The systemd unit is stored at `~/.config/systemd/user/a-token-monitor.service`.

### macOS

`service` takes exactly the same arguments as on Linux and writes a LaunchAgent:

```bash
a-token-monitor service install --dashboard --dashboard-port 8765
a-token-monitor service status
a-token-monitor service logs --lines 100
a-token-monitor service uninstall
```

- The LaunchAgent lives at `~/Library/LaunchAgents/com.a-token-monitor.daemon.plist` and
  logs to `~/.a-token-monitor/launchd.log`.
- `service plist` prints the service definition for the current platform, for review or
  manual installation.
- There is no netlink: the traffic panel and `traffic` degrade to listing processes and
  remote connections only (no byte counts, no traffic alerts) and state the reason
  explicitly.
- Active sessions rely on the system `ps` and `lsof`; when `lsof` is unavailable, sessions
  are still detected by directory but the “which session file is open” evidence is
  missing.
- Claude Code quotas read the OAuth credential from the Keychain: on first access macOS
  shows an "allow Keychain access" dialog — pick Allow (it won't ask again); this is not
  a hang. Denying it only hides Claude quota; everything else keeps working.

### Windows

```powershell
pip install a-token-monitor
a-token-monitor service install --dashboard
a-token-monitor service status
a-token-monitor service logs --lines 100
a-token-monitor service uninstall
```

- The scheduled task is named `ATokenMonitor`, starts at logon, runs with
  `LeastPrivilege` and needs no administrator rights; its configuration is written back
  to `<state_dir>\service.json` and a backup of the task definition is kept at
  `<state_dir>\a-token-monitor-task.xml`.
- Logs go to `<state_dir>\daemon.log`, and `service logs --follow` polls in Python
  instead of relying on `tail`.
- Scheduled tasks have no POSIX-style graceful stop signal: `service stop` is equivalent
  to ending the process, SQLite transactions and the single-instance lock are reclaimed
  by the system, and the next start resumes from the checkpoint.
- Process discovery uses a Toolhelp32 snapshot plus the Restart Manager (to find out which
  process holds a session file); the working directory is inferred from the session file's
  own metadata.
- As on macOS there is no netlink, so traffic features degrade to process-only.
- Notes:
  - When the Restart Manager is unavailable or a file is held by a higher-privilege
    process, the activity state of individual sessions may not be detected; quotas, the
    usage index and the Dashboard are unaffected.
  - Volumes other than NTFS (FAT/exFAT, some network drives) have no stable file IDs, so
    log-rotation detection degrades to size/time comparison.
  - `chmod 0700/0600` on the state directory only affects the read-only bit on Windows and
    is not a permission boundary; use `icacls` to tighten the ACLs if you need strict
    isolation.
  - Paths longer than 260 characters require `LongPathsEnabled` on the system.
  - A CLI installed through an npm `.cmd` wrapper is started with `cmd.exe /c`
    automatically, and process matching also strips `.exe/.cmd/.bat` suffixes and
    `node .../cli.js` wrappers.
  - Session files currently held open by an agent cannot be deleted: archiving/cleanup
    skips them with an explanation instead of aborting the whole batch.
  - In a container or without scheduled tasks, run it in the foreground:
    `a-token-monitor ... daemon`.

### Containers

- Monitoring other agent processes requires sharing the host PID namespace:
  `docker run --pid=host ...`; otherwise the usage index keeps working, but active
  sessions, process evidence and traffic attribution stay empty.
- Mount the state directory read-write (for example
  `-v "$HOME/.a-token-monitor:/state"` together with `--state-dir /state`); mount agent
  data directories read-only as needed.
- Expose the Dashboard port with `-p 8765:8765`, and pass `--dashboard-host 0.0.0.0`
  inside the container.
- Without systemd/launchd, run `daemon` in the foreground; `/healthz` and `/readyz` work
  for container and reverse-proxy probes.

## Data and security

Uninstalling the service never deletes monitoring data. Files in the state directory
(`~/.a-token-monitor` by default):

| File | Contents |
| --- | --- |
| `monitor.sqlite3` | Session registry, quota snapshots and recovery records |
| `usage-index.sqlite3` | Usage index and incremental-read checkpoints |
| `traffic-alerts.sqlite3` | Traffic alert history |
| `service.json` | Daemon configuration saved by `service install` (identical on all three platforms) |
| `scan-dirs.json` | Web scan-directory overrides saved from the settings page |
| `settings.json` | Web retention overrides saved from the settings page |
| `archives/` | Session archives: `codex-sessions-*.tar.gz` plus `.manifest.json` |

Besides pages and read-only endpoints, the Dashboard accepts only a few write endpoints
(`POST /api/alerts`, `POST /api/housekeeping`, and the settings page's scan-directory and
history endpoints). All of them require `Content-Type: application/json` and enforce a
request-body size limit; POST to any other path returns 405. The page has no
authentication by default, so when you expose it to the local network with
`--dashboard-host 0.0.0.0`, make sure the other devices on that network are trusted.

## Development

```bash
python -m pytest tests/ -q    # or python -m unittest discover -s tests -v
ruff check src tests
```

CI runs the same test suite across the full Linux / macOS / Windows × Python 3.10 / 3.13
matrix; platform-specific capabilities (symlinks, POSIX permission bits, `/proc`) are
skipped explicitly by test decorators.

## License

GNU General Public License v3.0 or later (`GPL-3.0-or-later`); see [LICENSE](LICENSE) for
the full terms.

Copyright (C) 2026 pantlive

You are free to use, modify and distribute this program; but when you distribute it or a
modified version, you must license it under the GPL as well and provide the complete
source code to recipients, without adding further restrictions.
