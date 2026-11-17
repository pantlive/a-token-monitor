"""保存非 Codex provider 的每日额度基线，不保存凭据或会话内容。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .local_time import local_day_key
from .quota import (
    QuotaDayBaseline,
    QuotaSnapshot,
    advance_quota_day_baseline,
    quota_day_baseline_changed,
)


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
                        high_water_percent REAL,
                        carried_percent REAL NOT NULL DEFAULT 0,
                        PRIMARY KEY (product, account_key, limit_id, name)
                    )
                    """
                )
                self._ensure_baseline_columns(connection)
        except (OSError, sqlite3.Error) as error:
            raise ProviderQuotaBaselineError(
                "无法初始化 provider 额度基线库"
            ) from error

    @staticmethod
    def _ensure_baseline_columns(connection: sqlite3.Connection) -> None:
        """为已有基线表补上最高水位和已结转消耗。"""

        columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(provider_quota_day_baseline)"
            )
        }
        if "high_water_percent" not in columns:
            connection.execute(
                "ALTER TABLE provider_quota_day_baseline "
                "ADD COLUMN high_water_percent REAL"
            )
        if "carried_percent" not in columns:
            connection.execute(
                "ALTER TABLE provider_quota_day_baseline "
                "ADD COLUMN carried_percent REAL NOT NULL DEFAULT 0"
            )
        # 中文注释：旧行只存了起点。最高水位先等于起点，下一次观察再往上累计。
        connection.execute(
            """
            UPDATE provider_quota_day_baseline
            SET high_water_percent = used_percent
            WHERE high_water_percent IS NULL
            """
        )

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
                    (str(row["limit_id"]), str(row["name"])): (
                        self._baseline_from_row(row)
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
                    baseline = advance_quota_day_baseline(
                        window, snapshot.observed_at, previous
                    )
                    if baseline is None:
                        continue
                    # 中文注释：回落或重复观察不改今日进度，避免面板轮询反复写库。
                    if previous is not None and not quota_day_baseline_changed(
                        previous, baseline
                    ):
                        continue
                    connection.execute(
                        """
                        INSERT INTO provider_quota_day_baseline (
                            product, account_key, limit_id, name, day_key,
                            used_percent, resets_at, observed_at,
                            high_water_percent, carried_percent
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(product, account_key, limit_id, name)
                        DO UPDATE SET
                            day_key=excluded.day_key,
                            used_percent=excluded.used_percent,
                            resets_at=excluded.resets_at,
                            observed_at=excluded.observed_at,
                            high_water_percent=excluded.high_water_percent,
                            carried_percent=excluded.carried_percent
                        """,
                        (
                            product,
                            account_key,
                            *key,
                            baseline.day_key,
                            baseline.used_percent,
                            baseline.resets_at,
                            baseline.observed_at,
                            baseline.high_water_percent,
                            baseline.carried_percent,
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

    @staticmethod
    def _baseline_from_row(row: sqlite3.Row) -> QuotaDayBaseline:
        """读取一行今日状态；旧库缺列时最高水位等于起点，已结转消耗为 0。"""

        names = set(row.keys())
        used_percent = float(row["used_percent"])
        high_water = row["high_water_percent"] if "high_water_percent" in names else None
        carried = row["carried_percent"] if "carried_percent" in names else None
        return QuotaDayBaseline(
            day_key=str(row["day_key"]),
            used_percent=used_percent,
            resets_at=(
                float(row["resets_at"]) if row["resets_at"] is not None else None
            ),
            observed_at=float(row["observed_at"]),
            high_water_percent=None if high_water is None else float(high_water),
            carried_percent=0.0 if carried is None else float(carried),
        )
