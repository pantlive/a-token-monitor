"""保存非 Codex provider 的每日额度基线，不保存凭据或会话内容。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .local_time import local_day_key
from .quota import QuotaDayBaseline, QuotaSnapshot, resolve_quota_day_baseline


class ProviderQuotaBaselineError(RuntimeError):
    """provider 额度基线数据库不可用。"""


class ProviderQuotaBaselineStore:
    """按产品、账号及额度窗口持久化本地自然日基线。"""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir.expanduser()
        self.database_file = self.state_dir / "provider_quota.sqlite3"
        self._ensure_database()

    def _ensure_database(self) -> None:
        """创建受保护的状态目录和基线表。"""

        try:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.state_dir.chmod(0o700)
            with self._connection() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS provider_quota_day_baseline (
                        product TEXT NOT NULL,
                        account_key TEXT NOT NULL,
                        limit_id TEXT NOT NULL,
                        name TEXT NOT NULL,
                        day_key TEXT NOT NULL,
                        used_percent REAL NOT NULL,
                        resets_at REAL,
                        observed_at REAL NOT NULL,
                        PRIMARY KEY (product, account_key, limit_id, name)
                    )
                    """
                )
        except (OSError, sqlite3.Error) as error:
            raise ProviderQuotaBaselineError(
                "无法初始化 provider 额度基线库"
            ) from error

    def _protect_database_files(self) -> None:
        """限制数据库及 WAL 辅助文件权限，与会话注册表保持一致。"""

        for suffix in ("", "-wal", "-shm"):
            path = self.database_file.with_name(
                self.database_file.name + suffix
            )
            try:
                if path.exists():
                    path.chmod(0o600)
            except OSError:
                continue

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """每次观察独立连接，提交或回滚后及时关闭。"""

        connection = sqlite3.connect(self.database_file, timeout=30.0)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            self._protect_database_files()

    def observe(
        self,
        product: str,
        account_key: str,
        snapshot: QuotaSnapshot,
    ) -> dict[tuple[str, str], QuotaDayBaseline]:
        """原子更新基线，返回快照所属本地日的窗口基线。"""

        day_key = local_day_key(snapshot.observed_at)
        try:
            with self._connection() as connection:
                # 中文注释：先锁定写事务，防止不同请求或进程同时建立不同的基线。
                connection.execute("BEGIN IMMEDIATE")
                baselines = {
                    (str(row["limit_id"]), str(row["name"])): QuotaDayBaseline(
                        day_key=str(row["day_key"]),
                        used_percent=float(row["used_percent"]),
                        resets_at=(
                            float(row["resets_at"])
                            if row["resets_at"] is not None
                            else None
                        ),
                        observed_at=float(row["observed_at"]),
                    )
                    for row in connection.execute(
                        "SELECT * FROM provider_quota_day_baseline "
                        "WHERE product = ? AND account_key = ?",
                        (product, account_key),
                    )
                }
                for window in snapshot.windows:
                    key = (window.limit_id, window.name)
                    previous = baselines.get(key)
                    # 中文注释：沿用 Codex 的一秒容差，晚到快照不能降级已有基线。
                    if (
                        previous is not None
                        and snapshot.observed_at < previous.observed_at - 1.0
                    ):
                        continue
                    used = resolve_quota_day_baseline(
                        window, snapshot.observed_at, previous
                    )
                    if used is None:
                        continue
                    # 中文注释：只比较基线内容，忽略观察时间，避免面板轮询反复写库。
                    if previous is not None and (
                        previous.day_key == day_key
                        and previous.used_percent == used
                        and previous.resets_at == window.resets_at
                    ):
                        continue
                    baseline = QuotaDayBaseline(
                        day_key, used, window.resets_at, snapshot.observed_at
                    )
                    connection.execute(
                        """
                        INSERT INTO provider_quota_day_baseline (
                            product, account_key, limit_id, name, day_key,
                            used_percent, resets_at, observed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(product, account_key, limit_id, name)
                        DO UPDATE SET
                            day_key=excluded.day_key,
                            used_percent=excluded.used_percent,
                            resets_at=excluded.resets_at,
                            observed_at=excluded.observed_at
                        """,
                        (
                            product,
                            account_key,
                            *key,
                            baseline.day_key,
                            baseline.used_percent,
                            baseline.resets_at,
                            baseline.observed_at,
                        ),
                    )
                    baselines[key] = baseline
        except sqlite3.Error as error:
            raise ProviderQuotaBaselineError(
                "无法更新 provider 额度基线"
            ) from error
        return {
            key: baseline
            for key, baseline in baselines.items()
            if baseline.day_key == day_key
        }
