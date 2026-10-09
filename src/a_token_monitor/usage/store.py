"""用量增量的 SQLite 持久索引。"""

from __future__ import annotations

import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..local_time import local_day_key
from .pricing import (
    _is_long_context,
    _lookup_pricing,
)
from .records import (
    TokenUsage,
    UsageDelta,
    _CachedFile,
    _IndexRow,
    _UsageSource,
    _delta_from_row,
    _state_from_json,
    _state_to_json,
    _usage_from_object,
)
from .search import (
    _FILE_ACCOUNT_VIEW,
    _account_clause,
    _empty_search_result,
    _file_projects,
    _keyword_clause,
    _project_clause,
    _search_pattern,
    _sql_search_page,
)


# 中文注释：解析规则变化时必须升版本，避免沿用错误的历史增量。
_USAGE_INDEX_VERSION = 6



# 中文注释：现行时区的 UTC 偏移都是 15 分钟的整数倍，本地零点必然落在 900 秒
# 的整数倍上，所以同一个 900 秒桶里的时间戳一定属于同一个本地自然日。
_LOCAL_DAY_BUCKET_SECONDS = 900


def _sql_local_day_function() -> Callable[[float | None], str | None]:
    """SQLite 回调：把时间戳换算为本地自然日，空值原样返回。

    逐行调 Python 比 SQLite 内置 'localtime' 慢，按 900 秒分桶缓存后，一次检索
    只需换算几千个桶。缓存跟随单个短连接，不会跨越时区变化长期存活。
    """

    cache: dict[int, str] = {}

    def local_day(timestamp: float | None) -> str | None:
        if timestamp is None:
            return None
        bucket = math.floor(float(timestamp) / _LOCAL_DAY_BUCKET_SECONDS)
        key = cache.get(bucket)
        if key is None:
            key = local_day_key(bucket * _LOCAL_DAY_BUCKET_SECONDS)
            cache[bucket] = key
        return key

    return local_day



# 中文注释：长上下文计价规则的版本。修改 pricing.py 里任何模型的长上下文阈值或
# 分档方式时加 1，已索引的标记会在下次打开索引时按新规则重算。
# 2：Claude 改按官方规则（4.6+ 不分档、Haiku 5.5 超过 100K ×5、Sonnet 4/4.5 超过 200K）。
_LONG_CONTEXT_RULES_VERSION = 2

class _UsageIndexStore:
    """把已解析的偏移和 token 增量保存到轻量 SQLite 索引。"""

    def __init__(self, path: Path) -> None:
        """初始化索引文件和表结构。"""

        self.path = path.expanduser()
        self._json1: bool | None = None
        self._pending_checked = False
        # 中文注释：只在状态目录写入索引，不复制或压缩原始 JSONL。
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        # 中文注释：索引含有项目路径和统计信息，只允许当前用户读取。
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        """打开一次短连接，避免 Dashboard 长期占用数据库句柄。"""

        connection = sqlite3.connect(self.path, timeout=1.0)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=1000")
        # 中文注释：SQLite 的 'localtime' 只认 C 库时区，测试注入的时区和 Python
        # 侧的按天汇总会对不上；按天分组统一走 local_time，保证两边日界线一致。
        connection.create_function(
            "usage_local_day", 1, _sql_local_day_function(), deterministic=True
        )
        return connection

    def _initialize(self) -> None:
        """创建索引所需的最小表和查询索引。"""

        try:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS usage_file_state (
                        path TEXT PRIMARY KEY,
                        inode INTEGER NOT NULL,
                        mtime_ns INTEGER NOT NULL,
                        file_size INTEGER NOT NULL,
                        next_offset INTEGER NOT NULL,
                        complete INTEGER NOT NULL,
                        state_json TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS usage_delta (
                        path TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        timestamp REAL NOT NULL,
                        model TEXT NOT NULL,
                        usage_json TEXT NOT NULL,
                        billing_usage_json TEXT,
                        project TEXT,
                        FOREIGN KEY(path) REFERENCES usage_file_state(path)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS usage_delta_path_index
                    ON usage_delta(path)
                    """
                )
                # 中文注释：用量检索按时间、模型和会话过滤，补上对应索引，
                # 避免历史记录变多后每次检索都全表扫描。
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS usage_delta_timestamp_index
                    ON usage_delta(timestamp)
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS usage_delta_model_index
                    ON usage_delta(model, timestamp)
                    """
                )
                # 中文注释：检索要按账号聚合和筛选，但账号身份来自
                # profile / auth.json，不属于 token 明细本身，因此单独按文件
                # 落一张表；只要索引轮次跑过就能 JOIN，不必重读 JSONL。
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS usage_file_account (
                        path TEXT PRIMARY KEY,
                        account_key TEXT NOT NULL,
                        account_name TEXT NOT NULL,
                        account_id TEXT,
                        profile_name TEXT,
                        product TEXT
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS usage_file_account_key_index
                    ON usage_file_account(account_key)
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS usage_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )
                version_row = connection.execute(
                    "SELECT value FROM usage_meta WHERE key = 'parser_version'"
                ).fetchone()
                if version_row is None or version_row[0] != str(_USAGE_INDEX_VERSION):
                    connection.execute("DELETE FROM usage_delta")
                    connection.execute("DELETE FROM usage_file_state")
                    connection.execute(
                        """
                        INSERT INTO usage_meta(key, value)
                        VALUES ('parser_version', ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """,
                        (str(_USAGE_INDEX_VERSION),),
                    )
                delta_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(usage_delta)")
                }
                if "project" not in delta_columns:
                    connection.execute(
                        "ALTER TABLE usage_delta ADD COLUMN project TEXT"
                    )
                if "long_context" not in delta_columns:
                    connection.execute(
                        "ALTER TABLE usage_delta ADD COLUMN long_context INTEGER"
                    )
                # 中文注释：长上下文的计价规则（阈值、哪些模型分档）变化后，已落盘的
                # 标记就过期了；清空后由下面的回填按当前价目表重算，不重读 JSONL。
                rules_row = connection.execute(
                    "SELECT value FROM usage_meta WHERE key = 'long_context_rules'"
                ).fetchone()
                if rules_row is None or rules_row[0] != str(_LONG_CONTEXT_RULES_VERSION):
                    connection.execute("UPDATE usage_delta SET long_context = NULL")
                    connection.execute(
                        """
                        INSERT INTO usage_meta(key, value)
                        VALUES ('long_context_rules', ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """,
                        (str(_LONG_CONTEXT_RULES_VERSION),),
                    )
                # 中文注释：旧索引按行回填一次，避免为了聚合重读所有 JSONL；
                # 回填中断后再次打开会继续补齐。
                if _has_pending_long_context(connection):
                    _backfill_long_context(connection)
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS usage_delta_kind_path_index
                    ON usage_delta(kind, path)
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS usage_delta_pending_index
                    ON usage_delta(long_context) WHERE long_context IS NULL
                    """
                )
        except (OSError, sqlite3.DatabaseError):
            # 中文注释：索引损坏时不影响 Dashboard 继续使用内存增量解析。
            raise

    def count_deltas(self) -> int:
        """返回用量增量明细的总行数，供历史数据预览使用。"""

        with closing(self._connect()) as connection:
            row = connection.execute("SELECT COUNT(*) FROM usage_delta").fetchone()
        return int(row[0]) if row is not None else 0

    def count_deltas_before(self, cutoff: float) -> int:
        """返回时间早于 ``cutoff`` 的用量增量行数。"""

        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM usage_delta WHERE timestamp < ?",
                (float(cutoff),),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def delete_deltas_before(self, cutoff: float) -> int:
        """删除时间早于 ``cutoff`` 的用量增量，返回删除行数。

        中文注释：只删 ``usage_delta`` 明细，绝不动 ``usage_file_state`` 里的
        增量读取检查点，否则已解析文件会被当作新文件全量重读。
        """

        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "DELETE FROM usage_delta WHERE timestamp < ?",
                (float(cutoff),),
            )
        return int(cursor.rowcount or 0)

    def vacuum(self) -> None:
        """删除历史行后压缩索引文件;数据库繁忙时把错误抛给上层处理。"""

        with closing(self._connect()) as connection:
            connection.execute("VACUUM")

    def close(self) -> None:
        """本类只使用短连接，没有需要释放的持久资源;为调用方统一接口保留。"""

    def load(self, path: Path) -> _CachedFile | None:
        """读取一个文件的持久化解析状态；损坏记录按未缓存处理。"""

        try:
            with closing(self._connect()) as connection, connection:
                row = connection.execute(
                    """
                    SELECT inode, mtime_ns, file_size, next_offset, complete,
                           state_json
                    FROM usage_file_state
                    WHERE path = ?
                    """,
                    (str(path),),
                ).fetchone()
                if row is None:
                    return None
                state = _state_from_json(row[5])
                if state is None:
                    return None
                total_deltas: list[UsageDelta] = []
                fallback_deltas: list[UsageDelta] = []
                delta_rows = connection.execute(
                    """
                    SELECT kind, timestamp, model, usage_json,
                           billing_usage_json, project
                    FROM usage_delta
                    WHERE path = ?
                    ORDER BY rowid
                    """,
                    (str(path),),
                )
                for delta_row in delta_rows:
                    delta = _delta_from_row(delta_row)
                    if delta is None:
                        return None
                    if delta_row[0] == "total":
                        total_deltas.append(delta)
                    elif delta_row[0] == "fallback":
                        fallback_deltas.append(delta)
                    else:
                        return None
                return _CachedFile(
                    signature=(int(row[0]), int(row[1]), int(row[2])),
                    next_offset=int(row[3]),
                    total_deltas=tuple(total_deltas),
                    fallback_deltas=tuple(fallback_deltas),
                    state=state,
                    complete=bool(row[4]),
                )
        except (
            OSError,
            OverflowError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            sqlite3.DatabaseError,
        ):
            return None

    def search_deltas(
        self,
        *,
        since: float | None = None,
        until: float | None = None,
        models: Sequence[str] = (),
        session: str | None = None,
        project: str | None = None,
        account: str | None = None,
        keyword: str | None = None,
        limit: int | None = None,
    ) -> tuple[_IndexRow, ...]:
        """按时间、模型、会话和账号关键词检索索引里的 token 记录。"""

        clauses: list[str] = []
        parameters: list[Any] = []
        if since is not None:
            clauses.append("timestamp >= ?")
            parameters.append(float(since))
        if until is not None:
            clauses.append("timestamp <= ?")
            parameters.append(float(until))
        selected_models = [item for item in models if item]
        if selected_models:
            placeholders = ", ".join("?" for _ in selected_models)
            clauses.append(f"model IN ({placeholders})")
            parameters.extend(selected_models)
        session_pattern = _search_pattern(session)
        if session_pattern is not None:
            clauses.append("path LIKE ? ESCAPE '\\'")
            parameters.append(session_pattern)
        try:
            with closing(self._connect()) as connection:
                file_projects = _file_projects(connection)
                project_pattern = _search_pattern(project)
                if project_pattern is not None:
                    clause, clause_parameters = _project_clause(
                        project_pattern,
                        file_projects,
                    )
                    clauses.append(clause)
                    parameters.extend(clause_parameters)
                keyword_pattern = _search_pattern(keyword)
                if keyword_pattern is not None:
                    keyword_clause, keyword_parameters = _keyword_clause(
                        keyword_pattern,
                        file_projects,
                    )
                    clauses.append(keyword_clause)
                    parameters.extend(keyword_parameters)
                account_pattern = _search_pattern(account)
                if account_pattern is not None:
                    clauses.append(_account_clause(account_pattern))
                    parameters.extend([account_pattern] * 4)
                where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
                statement = (
                    "SELECT path, kind, timestamp, model, usage_json, "
                    "billing_usage_json, project, accounts.account_key, "
                    "accounts.account_name, accounts.account_id, accounts.product "
                    "FROM usage_delta LEFT JOIN "
                    f"({_FILE_ACCOUNT_VIEW}) AS accounts "
                    "ON accounts.account_path = usage_delta.path"
                    # 中文注释：JOIN 了账号视图，rowid 必须写明表名；较老的 SQLite
                    # （如 3.46）会把裸 rowid 判为歧义列名，查询失败后检索结果变空。
                    f"{where} ORDER BY timestamp DESC, usage_delta.rowid DESC"
                )
                if limit is not None:
                    statement += " LIMIT ?"
                    parameters.append(int(limit))
                rows = connection.execute(statement, parameters).fetchall()
        except sqlite3.DatabaseError:
            # 中文注释：索引损坏或并发写入冲突时退回空结果，不影响 Dashboard。
            return ()
        result: list[_IndexRow] = []
        for row in rows:
            timestamp = row[2]
            model = row[3]
            usage_json = row[4]
            if not isinstance(timestamp, (int, float)) or not isinstance(model, str):
                continue
            if not isinstance(usage_json, str):
                continue
            billing = row[5] if isinstance(row[5], str) else None
            project = row[6] if isinstance(row[6], str) and row[6] else None
            if project is None:
                # 中文注释：索引行本身不带项目时回退到文件级 session 工作目录。
                project = file_projects.get(str(row[0]))
            result.append(
                _IndexRow(
                    path=str(row[0]),
                    kind=str(row[1]),
                    timestamp=float(timestamp),
                    model=model,
                    usage_json=usage_json,
                    billing_usage_json=billing,
                    project=project,
                    account_key=row[7] if isinstance(row[7], str) else None,
                    account_name=row[8] if isinstance(row[8], str) else None,
                    account_id=row[9] if isinstance(row[9], str) else None,
                    product=row[10] if isinstance(row[10], str) else None,
                )
            )
        return tuple(result)

    def session_rows(self, paths: Sequence[str]) -> tuple[_IndexRow, ...]:
        """按路径读取会话的索引记录，用于统计轮数、上下文和累计用量。"""

        wanted = [str(path) for path in paths if path][:500]
        if not wanted:
            return ()
        placeholders = ", ".join("?" for _ in wanted)
        statement = (
            "SELECT path, kind, timestamp, model, usage_json, "
            "billing_usage_json, project FROM usage_delta "
            f"WHERE path IN ({placeholders}) ORDER BY path, timestamp, rowid"
        )
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(statement, wanted).fetchall()
        except sqlite3.DatabaseError:
            return ()
        result: list[_IndexRow] = []
        for row in rows:
            if not isinstance(row[2], (int, float)) or not isinstance(row[3], str):
                continue
            if not isinstance(row[4], str):
                continue
            result.append(
                _IndexRow(
                    path=str(row[0]),
                    kind=str(row[1]),
                    timestamp=float(row[2]),
                    model=str(row[3]),
                    usage_json=row[4],
                    billing_usage_json=row[5] if isinstance(row[5], str) else None,
                    project=row[6] if isinstance(row[6], str) else None,
                )
            )
        return tuple(result)

    def supports_json_aggregation(self) -> bool:
        """探测索引是否支持 JSON1 聚合（用量检索的快速路径）。"""

        if self._json1 is None:
            try:
                with closing(self._connect()) as connection:
                    connection.execute(
                        "SELECT json_extract('{\"a\":1}', '$.a')"
                    ).fetchone()
                self._json1 = True
            except sqlite3.DatabaseError:
                self._json1 = False
        return self._json1

    def has_pending_long_context(self) -> bool:
        """判断是否还有未回填长上下文标记的行（结果只探测一次）。"""

        if self._pending_checked:
            return False
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT 1 FROM usage_delta WHERE long_context IS NULL LIMIT 1"
                ).fetchone()
        except sqlite3.DatabaseError:
            return True
        if row is None:
            self._pending_checked = True
            return False
        return True

    def search_groups(self, **criteria: Any) -> tuple[dict[str, Any], ...]:
        """兼容旧内部调用；页面检索优先使用有界分页入口。"""

        result = self._query_groups(**criteria)
        assert isinstance(result, tuple)
        return result

    def search_page(
        self,
        *,
        group: str,
        sort: str,
        limit: int,
        offset: int,
        **criteria: Any,
    ) -> dict[str, Any]:
        """在 SQLite 内分组、排序和分页，仅将当前页与全量合计返回 Python。"""

        result = self._query_groups(**criteria, page=(group, sort, limit, offset))
        if isinstance(result, dict):
            return result
        return _empty_search_result(
            group=group, sort=sort, limit=limit, offset=offset
        )

    def _query_groups(
        self,
        *,
        since: float | None = None,
        until: float | None = None,
        models: Sequence[str] = (),
        session: str | None = None,
        project: str | None = None,
        account: str | None = None,
        keyword: str | None = None,
        page: tuple[str, str, int, int] | None = None,
    ) -> tuple[dict[str, Any], ...] | dict[str, Any]:
        """在 SQL 里按「日期 + 会话 + 模型 + 长上下文」聚合出 token 分桶。

        只返回聚合结果，不把逐条用量读进 Python；成本由调用方用每个分桶的
        显式长上下文标记计算，保证与逐条估算一致。账号身份用文件级账号表
        JOIN 出来，缺少账号信息的旧索引行按未知账号处理。
        """

        clauses: list[str] = [
            "(kind = 'total' OR path NOT IN "
            "(SELECT path FROM usage_delta WHERE kind = 'total'))"
        ]
        parameters: list[Any] = []
        if since is not None:
            clauses.append("timestamp >= ?")
            parameters.append(float(since))
        if until is not None:
            clauses.append("timestamp <= ?")
            parameters.append(float(until))
        selected_models = [item for item in models if item]
        if selected_models:
            placeholders = ", ".join("?" for _ in selected_models)
            clauses.append(f"model IN ({placeholders})")
            parameters.extend(selected_models)
        session_pattern = _search_pattern(session)
        if session_pattern is not None:
            clauses.append("path LIKE ? ESCAPE '\\'")
            parameters.append(session_pattern)
        try:
            with closing(self._connect()) as connection:
                file_projects = _file_projects(connection)
                project_pattern = _search_pattern(project)
                if project_pattern is not None:
                    clause, clause_parameters = _project_clause(
                        project_pattern,
                        file_projects,
                    )
                    clauses.append(clause)
                    parameters.extend(clause_parameters)
                keyword_pattern = _search_pattern(keyword)
                if keyword_pattern is not None:
                    keyword_clause, keyword_parameters = _keyword_clause(
                        keyword_pattern,
                        file_projects,
                    )
                    clauses.append(keyword_clause)
                    parameters.extend(keyword_parameters)
                account_pattern = _search_pattern(account)
                if account_pattern is not None:
                    clauses.append(_account_clause(account_pattern))
                    parameters.extend([account_pattern] * 4)
                where = " WHERE " + " AND ".join(clauses)
                connection.row_factory = sqlite3.Row
                statement = (
                    "SELECT usage_local_day(timestamp) "
                    "AS day, path, model, COALESCE(long_context, 0) AS long_context, "
                    "accounts.account_key AS account_key, "
                    "accounts.account_name AS account_name, "
                    "accounts.account_id AS account_id, "
                    "accounts.product AS product, "
                    "COUNT(*) AS records, MIN(timestamp) AS first_at, "
                    "MAX(timestamp) AS last_at, "
                    "SUM(json_extract(usage_json, '$.input_tokens')) AS input_tokens, "
                    "SUM(json_extract(usage_json, '$.cached_input_tokens')) "
                    "AS cached_input_tokens, "
                    "SUM(json_extract(usage_json, '$.cache_write_input_tokens')) "
                    "AS cache_write_input_tokens, "
                    "SUM(json_extract(usage_json, '$.output_tokens')) AS output_tokens, "
                    "SUM(json_extract(usage_json, '$.reasoning_output_tokens')) "
                    "AS reasoning_output_tokens, "
                    "SUM(json_extract(usage_json, '$.total_tokens')) AS total_tokens, "
                    "SUM(COALESCE(json_extract(billing_usage_json, '$.input_tokens'), "
                    "json_extract(usage_json, '$.input_tokens'))) AS billing_input_tokens, "
                    "SUM(COALESCE(json_extract(billing_usage_json, "
                    "'$.cached_input_tokens'), "
                    "json_extract(usage_json, '$.cached_input_tokens'))) "
                    "AS billing_cached_input_tokens, "
                    "SUM(COALESCE(json_extract(billing_usage_json, "
                    "'$.cache_write_input_tokens'), "
                    "json_extract(usage_json, '$.cache_write_input_tokens'))) "
                    "AS billing_cache_write_input_tokens, "
                    "SUM(COALESCE(json_extract(billing_usage_json, '$.output_tokens'), "
                    "json_extract(usage_json, '$.output_tokens'))) "
                    "AS billing_output_tokens "
                    f"FROM usage_delta LEFT JOIN ({_FILE_ACCOUNT_VIEW}) AS accounts "
                    "ON accounts.account_path = usage_delta.path"
                    f"{where} "
                    "GROUP BY day, path, model, long_context, account_key, "
                    "account_name, account_id, product "
                    "ORDER BY last_at DESC"
                )
                if page is not None:
                    return _sql_search_page(
                        connection, statement, parameters, file_projects, page
                    )
                rows = connection.execute(statement, parameters).fetchall()
                results: list[dict[str, Any]] = []
                for row in rows:
                    payload = dict(row)
                    project = payload.get("project")
                    if not (isinstance(project, str) and project):
                        # 中文注释：索引行不带项目时回退到文件级 session 工作目录。
                        project = file_projects.get(str(payload.get("path")))
                    payload["project"] = project
                    results.append(payload)
        except sqlite3.DatabaseError:
            return ()
        return tuple(results)

    def facets(self) -> dict[str, Any]:
        """返回索引整体的模型、账号列表和覆盖时间范围。"""

        try:
            with closing(self._connect()) as connection:
                summary = connection.execute(
                    "SELECT COUNT(*), MIN(timestamp), MAX(timestamp), "
                    "COUNT(DISTINCT path), COUNT(DISTINCT model) FROM usage_delta"
                ).fetchone()
                model_rows = connection.execute(
                    "SELECT DISTINCT model FROM usage_delta "
                    "WHERE model <> '' ORDER BY model"
                ).fetchall()
                account_rows = connection.execute(
                    "SELECT DISTINCT account_name FROM usage_file_account "
                    "WHERE account_name <> '' ORDER BY account_name"
                ).fetchall()
        except sqlite3.DatabaseError:
            return {
                "records": 0,
                "sessions": 0,
                "models": [],
                "accounts": [],
                "first_at": None,
                "last_at": None,
            }
        return {
            "records": int(summary[0] or 0),
            "sessions": int(summary[3] or 0),
            "models": [str(row[0]) for row in model_rows],
            "accounts": [str(row[0]) for row in account_rows][:200],
            "first_at": float(summary[1]) if summary[1] is not None else None,
            "last_at": float(summary[2]) if summary[2] is not None else None,
        }

    def save(
        self,
        path: Path,
        cached_file: _CachedFile,
        total_deltas: tuple[UsageDelta, ...],
        fallback_deltas: tuple[UsageDelta, ...],
        replace_deltas: bool,
    ) -> None:
        """保存文件检查点，只追加本轮新产生的增量。"""

        try:
            with closing(self._connect()) as connection, connection:
                if replace_deltas:
                    connection.execute(
                        "DELETE FROM usage_delta WHERE path = ?",
                        (str(path),),
                    )
                rows = [
                    _delta_to_row(str(path), "total", delta)
                    for delta in total_deltas
                ]
                rows.extend(
                    _delta_to_row(str(path), "fallback", delta)
                    for delta in fallback_deltas
                )
                if rows:
                    connection.executemany(
                        """
                        INSERT INTO usage_delta(
                            path, kind, timestamp, model, usage_json,
                            billing_usage_json, project, long_context
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        rows,
                    )
                connection.execute(
                    """
                    INSERT INTO usage_file_state(
                        path, inode, mtime_ns, file_size, next_offset,
                        complete, state_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        inode = excluded.inode,
                        mtime_ns = excluded.mtime_ns,
                        file_size = excluded.file_size,
                        next_offset = excluded.next_offset,
                        complete = excluded.complete,
                        state_json = excluded.state_json
                    """,
                    (
                        str(path),
                        cached_file.signature[0],
                        cached_file.signature[1],
                        cached_file.signature[2],
                        cached_file.next_offset,
                        int(cached_file.complete),
                        _state_to_json(cached_file.state),
                    ),
                )
        except (OSError, OverflowError, TypeError, ValueError, sqlite3.DatabaseError):
            # 中文注释：持久化失败只损失下次启动的缓存，不中断额度监控。
            return

    def prune(self, paths: Mapping[Path, Any]) -> None:
        """删除已经不在当前扫描范围的文件索引。"""

        allowed = {str(path) for path in paths}
        try:
            with closing(self._connect()) as connection, connection:
                rows = connection.execute(
                    "SELECT path FROM usage_file_state"
                ).fetchall()
                stale = [(row[0],) for row in rows if row[0] not in allowed]
                if stale:
                    connection.executemany(
                        "DELETE FROM usage_delta WHERE path = ?",
                        stale,
                    )
                    connection.executemany(
                        "DELETE FROM usage_file_state WHERE path = ?",
                        stale,
                    )
                account_rows = connection.execute(
                    "SELECT path FROM usage_file_account"
                ).fetchall()
                stale_accounts = [
                    (row[0],) for row in account_rows if row[0] not in allowed
                ]
                if stale_accounts:
                    connection.executemany(
                        "DELETE FROM usage_file_account WHERE path = ?",
                        stale_accounts,
                    )
        except (OSError, sqlite3.DatabaseError):
            return

    def save_file_accounts(self, sources: Mapping[Path, _UsageSource]) -> None:
        """把每个 JSONL 文件所属的账号身份写入索引，供按账号聚合使用。

        中文注释：账号身份来自 profile / auth.json，不在 token 明细里；这里按
        文件覆盖写入，既让检索能按账号分组和筛选，也保证 profile 换号后旧会话
        仍然归到写入当时的账号。
        """

        rows = [
            (
                str(path),
                source.account_key,
                source.account_name,
                source.account_id,
                source.profile_name,
                source.product,
            )
            for path, source in sources.items()
        ]
        if not rows:
            return
        try:
            with closing(self._connect()) as connection, connection:
                connection.executemany(
                    """
                    INSERT INTO usage_file_account(
                        path, account_key, account_name, account_id,
                        profile_name, product
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        account_key = excluded.account_key,
                        account_name = excluded.account_name,
                        account_id = excluded.account_id,
                        profile_name = excluded.profile_name,
                        product = excluded.product
                    WHERE usage_file_account.account_key IS NOT excluded.account_key
                        OR usage_file_account.account_name IS NOT excluded.account_name
                        OR usage_file_account.account_id IS NOT excluded.account_id
                        OR usage_file_account.profile_name IS NOT excluded.profile_name
                        OR usage_file_account.product IS NOT excluded.product
                    """,
                    rows,
                )
        except (OSError, OverflowError, TypeError, ValueError, sqlite3.DatabaseError):
            # 中文注释：账号表写入失败只影响分组展示，不影响 token 索引。
            return


def _delta_to_row(path: str, kind: str, delta: UsageDelta) -> tuple[object, ...]:
    """把一个 token 增量转换成 SQLite 行。"""

    billing_usage = delta.billing_usage or delta.usage
    return (
        path,
        kind,
        delta.timestamp,
        delta.model,
        json.dumps(delta.usage.to_dict(), separators=(",", ":")),
        json.dumps(billing_usage.to_dict(), separators=(",", ":")),
        delta.project,
        _long_context_flag(billing_usage, delta.model),
    )


def _long_context_flag(usage: TokenUsage, model: str) -> int:
    """判断一次用量是否按长上下文计价，结果随行落盘供聚合查询分组。"""

    pricing = _lookup_pricing(model)
    if pricing is None:
        return 0
    return 1 if _is_long_context(usage.input_tokens, pricing) else 0


def _has_pending_long_context(connection: sqlite3.Connection) -> bool:
    """判断索引里是否还有等待回填长上下文标记的行。"""

    try:
        row = connection.execute(
            "SELECT 1 FROM usage_delta WHERE long_context IS NULL LIMIT 1"
        ).fetchone()
    except sqlite3.DatabaseError:
        return False
    return row is not None


def _backfill_long_context(connection: sqlite3.Connection) -> int:
    """给旧索引行补齐长上下文标记，只做一次。"""

    rows = connection.execute(
        "SELECT rowid, model, billing_usage_json, usage_json FROM usage_delta"
    ).fetchall()
    updates: list[tuple[int, int]] = []
    for row_id, model, billing_json, usage_json in rows:
        if not isinstance(model, str):
            continue
        payload = billing_json if isinstance(billing_json, str) else usage_json
        if not isinstance(payload, str):
            continue
        try:
            usage = _usage_from_object(json.loads(payload))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if usage is None:
            continue
        updates.append((_long_context_flag(usage, model), int(row_id)))
    if updates:
        connection.executemany(
            "UPDATE usage_delta SET long_context = ? WHERE rowid = ?",
            updates,
        )
    return len(updates)
