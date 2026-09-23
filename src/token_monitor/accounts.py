"""多个 Codex 登录目录的配置与本地状态隔离。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class CodexAccount:
    """一个独立 ``CODEX_HOME`` 及其监控状态位置。"""

    name: str
    home: Path
    session_root: Path
    state_dir: Path
    account_id: str | None = None

    def __post_init__(self) -> None:
        """校验账号配置不会产生空标识。"""

        if not self.name.strip():
            raise ValueError("Codex 账号名称不能为空")

    @property
    def identity_key(self) -> str:
        """返回优先使用真实账号 ID 的稳定身份键。"""

        return self.account_id or f"profile:{self.name}"


def default_codex_home() -> Path:
    """返回 Codex 默认登录目录。"""

    configured_home = os.environ.get("CODEX_HOME")
    if configured_home:
        return Path(configured_home).expanduser()
    return Path.home() / ".codex"


def build_account_specs(
    homes: Sequence[Path] | None,
    state_dir: Path,
    session_root: Path | None = None,
) -> tuple[CodexAccount, ...]:
    """根据多个 ``CODEX_HOME`` 构造彼此隔离的账号配置。

    默认账号继续使用旧版 ``state_dir``，这样已有单账号队列和额度快照无需
    迁移；增加其他账号时，其他状态放到 ``state_dir/accounts`` 下。如果没有
    配置默认 ``~/.codex``，则传入的第一个账号使用旧版目录。显式传入
    ``session_root`` 只允许配合一个账号，避免把两个账号的 JSONL 混在一起。
    """

    if homes is None:
        # 中文注释：没有显式配置时只在默认 CODEX_HOME 确实存在时建账号，
        # 这样既没有 Codex CLI 也没有 CODEX_HOME 的机器也能只监控其他 provider。
        default_home = default_codex_home().expanduser()
        raw_homes = (default_home,) if default_home.exists() else ()
    else:
        raw_homes = tuple(homes)
    normalized_homes = _unique_paths(raw_homes)
    if not normalized_homes:
        if session_root is not None:
            raise ValueError("--session-root 需要至少一个 --codex-home")
        # 中文注释：没有任何 Codex 账号是合法配置：daemon 可以只监控
        # 流量、Kimi、DSH、Grok、Claude Code 或 Command Code。
        return ()
    if session_root is not None and len(normalized_homes) != 1:
        raise ValueError("--session-root 只能和一个 --codex-home 一起使用")

    normalized_state_dir = state_dir.expanduser()
    default_home = _normalize_path(default_codex_home())
    primary_home = (
        default_home if default_home in normalized_homes else normalized_homes[0]
    )
    used_names: set[str] = set()
    used_state_dirs: set[Path] = set()
    accounts: list[CodexAccount] = []
    for index, home in enumerate(normalized_homes):
        name = _unique_name(home, used_names)
        if session_root is not None:
            account_session_root = session_root.expanduser()
        else:
            account_session_root = home / "sessions"
        account_state_dir = _account_state_dir(
            home=home,
            index=index,
            total=len(normalized_homes),
            primary_home=primary_home,
            state_dir=normalized_state_dir,
            used_state_dirs=used_state_dirs,
        )
        accounts.append(
            CodexAccount(
                name=name,
                home=home,
                session_root=account_session_root,
                state_dir=account_state_dir,
                account_id=read_codex_account_id(home),
            )
        )
    return tuple(accounts)


def read_codex_account_id(codex_home: Path) -> str | None:
    """从一个 ``CODEX_HOME`` 的本地认证状态读取账号 ID。

    这里只读取 ``tokens.account_id`` 这类身份字段，不记录或返回访问令牌、
    刷新令牌及其他凭据内容。认证文件不存在或格式不兼容时返回 ``None``，
    由调用方退回使用登录目录名称。
    """

    auth_file = codex_home.expanduser() / "auth.json"
    try:
        payload = json.loads(auth_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None

    containers: list[Mapping[str, object]] = [payload]
    tokens = payload.get("tokens")
    if isinstance(tokens, Mapping):
        containers.insert(0, tokens)
    for container in containers:
        for key in (
            "account_id",
            "chatgpt_account_id",
            "accountId",
            "chatgptAccountId",
        ):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _unique_paths(paths: Sequence[Path]) -> tuple[Path, ...]:
    """按用户传入顺序去重路径，避免重复扫描同一个账号。"""

    result: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        normalized = _normalize_path(path)
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return tuple(result)


def _normalize_path(path: Path) -> Path:
    """展开用户目录并尽量生成稳定的绝对路径。"""

    expanded = path.expanduser()
    try:
        return expanded.resolve()
    except (OSError, RuntimeError):
        return expanded.absolute()


def _unique_name(home: Path, used_names: set[str]) -> str:
    """从目录名生成 Dashboard 账号名称，并处理同名目录。"""

    base_name = home.name.lstrip(".") or "codex"
    name = base_name
    if name in used_names:
        name = f"{base_name}-{_path_suffix(home)}"
    used_names.add(name)
    return name


def _account_state_dir(
    home: Path,
    index: int,
    total: int,
    primary_home: Path,
    state_dir: Path,
    used_state_dirs: set[Path],
) -> Path:
    """为账号分配状态目录，并尽量复用默认账号的旧状态。"""

    if total == 1 or home == primary_home:
        candidate = state_dir
    else:
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", home.name).strip("._")
        safe_name = safe_name or f"account-{index + 1}"
        candidate = state_dir / "accounts" / safe_name
        if candidate in used_state_dirs or candidate == state_dir:
            candidate = candidate.with_name(f"{candidate.name}-{_path_suffix(home)}")
    used_state_dirs.add(candidate)
    return candidate


def _path_suffix(path: Path) -> str:
    """返回用于解决目录名冲突的短路径摘要。"""

    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:8]
