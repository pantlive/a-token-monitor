"""多个 Codex 登录目录的配置与本地状态隔离。"""

from __future__ import annotations

import base64
import binascii
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
    # 中文注释：订阅类型（plus / pro / prolite …）来自本地 auth.json 的
    # chatgpt_plan_type，只用于展示，不参与账号归组。
    plan_type: str | None = None

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
                plan_type=read_codex_plan_type(home),
            )
        )
    return tuple(accounts)


def build_additional_account_spec(
    home: Path,
    state_dir: Path,
    existing: Sequence[CodexAccount],
) -> CodexAccount:
    """为运行中热新增的 ``CODEX_HOME`` 构造账号配置。

    命名和状态目录沿用 ``build_account_specs`` 的约定：已有账号为空且目录
    就是默认 ``~/.codex`` 时复用旧版 ``state_dir``，重新加回默认账号因此能
    恢复它之前的检查点；其他情况一律放到 ``state_dir/accounts`` 下，并避开
    与现有账号的名称和状态目录冲突。
    """

    normalized_home = _normalize_path(home)
    normalized_state_dir = state_dir.expanduser()
    used_names = {account.name for account in existing}
    name = _unique_name(normalized_home, used_names)
    default_home = _normalize_path(default_codex_home())
    if not existing and normalized_home == default_home:
        account_state_dir = normalized_state_dir
    else:
        # 中文注释：与 _account_state_dir 的非默认分支保持同一套约定，但
        # 热新增账号永不复用旧版 state_dir——已有账号时它总被旧账号占用。
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", normalized_home.name)
        safe_name = safe_name.strip("._") or f"account-{len(existing) + 2}"
        account_state_dir = normalized_state_dir / "accounts" / safe_name
        used_state_dirs = {account.state_dir for account in existing}
        if (
            account_state_dir in used_state_dirs
            or account_state_dir == normalized_state_dir
        ):
            account_state_dir = account_state_dir.with_name(
                f"{account_state_dir.name}-{_path_suffix(normalized_home)}"
            )
    return CodexAccount(
        name=name,
        home=normalized_home,
        session_root=normalized_home / "sessions",
        state_dir=account_state_dir,
        account_id=read_codex_account_id(normalized_home),
        plan_type=read_codex_plan_type(normalized_home),
    )


def _read_auth_payload(codex_home: Path) -> Mapping[str, object] | None:
    """读取 ``auth.json`` 的结构；认证文件缺失或不兼容时返回 ``None``。

    调用方只应提取账号 ID、订阅类型这类元数据字段，绝不能把令牌内容写进
    日志或返回值。
    """

    auth_file = codex_home.expanduser() / "auth.json"
    try:
        payload = json.loads(auth_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _auth_containers(payload: Mapping[str, object]) -> list[Mapping[str, object]]:
    """返回 auth.json 里可能存放元数据的层：tokens 优先，其次是根对象。"""

    containers: list[Mapping[str, object]] = [payload]
    tokens = payload.get("tokens")
    if isinstance(tokens, Mapping):
        containers.insert(0, tokens)
    return containers


def _jwt_claims(token: object) -> Mapping[str, object] | None:
    """解析 JWT 的 payload 段（不做签名校验，只读公开 claim）。"""

    if not isinstance(token, str) or token.count(".") != 2:
        return None
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return claims if isinstance(claims, Mapping) else None


# 中文注释：auth.json 变动很少但会被 Dashboard 每 5 秒读取一次，按 mtime 缓存。
_PLAN_CACHE: dict[Path, tuple[int, str | None]] = {}


def read_codex_plan_type(codex_home: Path) -> str | None:
    """读取 Codex 的订阅类型（``plus`` / ``pro`` / ``prolite`` 等）。

    来源优先级：auth.json（含 tokens）里的同名字段 → ``tokens.id_token`` 中
    ``https://api.openai.com/auth`` 的 ``chatgpt_plan_type`` claim。只解析套餐名，
    不校验签名、不发起网络请求，也不读取或返回任何令牌内容。
    """

    auth_file = codex_home.expanduser() / "auth.json"
    try:
        modified = auth_file.stat().st_mtime_ns
    except OSError:
        return None
    cached = _PLAN_CACHE.get(auth_file)
    if cached is not None and cached[0] == modified:
        return cached[1]
    plan = _extract_plan_type(_read_auth_payload(codex_home))
    _PLAN_CACHE[auth_file] = (modified, plan)
    return plan


def _extract_plan_type(payload: Mapping[str, object] | None) -> str | None:
    """从 auth.json 结构里提取订阅类型。"""

    if payload is None:
        return None
    for container in _auth_containers(payload):
        for key in ("chatgpt_plan_type", "plan_type", "planType"):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    for container in _auth_containers(payload):
        claims = _jwt_claims(container.get("id_token"))
        if claims is None:
            continue
        nested = claims.get("https://api.openai.com/auth")
        sources = [nested] if isinstance(nested, Mapping) else []
        sources.append(claims)
        for source in sources:
            value = source.get("chatgpt_plan_type")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def read_codex_account_id(codex_home: Path) -> str | None:
    """从一个 ``CODEX_HOME`` 的本地认证状态读取账号 ID。

    这里只读取 ``tokens.account_id`` 这类身份字段，不记录或返回访问令牌、
    刷新令牌及其他凭据内容。认证文件不存在或格式不兼容时返回 ``None``，
    由调用方退回使用登录目录名称。
    """

    payload = _read_auth_payload(codex_home)
    if payload is None:
        return None

    containers: list[Mapping[str, object]] = _auth_containers(payload)
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
