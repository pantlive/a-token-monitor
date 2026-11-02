"""脱敏规则、确定性占位符与本地密钥表。

占位符形如 ``[[SECRET:github_token:3fa9c01b2e]]``：类别让模型知道这里原来是什么，
十位十六进制是本机密钥对原值做 HMAC-SHA256 的前缀。同一个值永远换成同一个占位符，
所以多轮请求的前缀保持一致，prompt cache 照常命中；模型回复里出现的占位符可以按
密钥表还原。

密钥表（vault）保存「占位符 → 原值」，只用于还原，文件权限 0600，放在状态目录下。
代理和各 agent 的 hook 是不同进程，靠这个文件共享映射；读到未知占位符时会重新加载。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import threading
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

try:  # 中文注释：Windows 没有 fcntl，退化为进程内锁（hook 并发写只在 POSIX 上常见）。
    import fcntl
except ImportError:  # pragma: no cover - 平台相关
    fcntl = None  # type: ignore[assignment]


PLACEHOLDER_PATTERN = re.compile(r"\[\[SECRET:([a-z0-9_]{1,32}):([0-9a-f]{10})\]\]")
# 中文注释：流式还原时判断「文本末尾是不是半个占位符」用的前缀形状。
_PLACEHOLDER_PREFIX = "[[SECRET:"
_PARTIAL_TAIL = re.compile(r"\[(?:\[(?:S(?:E(?:C(?:R(?:E(?:T(?::[a-z0-9_]{0,32}(?::[0-9a-f]{0,10}\]?)?)?)?)?)?)?)?)?)?$")

DEFAULT_VAULT_TTL_DAYS = 30.0
_MIN_SECRET_LENGTH = 8
_GENERIC_MIN_LENGTH = 12
_GENERIC_MIN_ENTROPY = 3.0

# 中文注释：变量名里带这些词的环境变量，值会作为精确匹配的密钥。
_SECRET_ENV_NAME = re.compile(
    r"(?i)(?:^|_)(?:API_?KEY|SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE_?KEY|ACCESS_?KEY|AUTH|CREDENTIALS?)(?:_|$)"
)
_ENV_VALUE_SKIP = re.compile(r"^(?:true|false|yes|no|on|off|none|null|\d+)$", re.IGNORECASE)

# 中文注释：公网 IP 规则默认关闭（规则命中率高、日志里几乎都有），需要时在
# redact/config.json 的 enabled_rules 里显式开启，或按 allow_patterns 白名单放行。
DEFAULT_OFF_RULES: frozenset[str] = frozenset({"public_ipv4", "public_ipv6"})


@dataclass(frozen=True)
class Rule:
    """一条正则规则；``group`` 指定只替换哪个分组（0 表示整段）。"""

    name: str
    pattern: re.Pattern[str]
    group: int = 0
    # 中文注释：通用的「key = value」规则误报多，额外要求值看起来像随机串。
    require_entropy: bool = False
    # 中文注释：命中值要过回调校验（IBAN 校验位、Luhn、SSN 号段、生日等）。
    validate: Callable[[str], bool] | None = None
    # 中文注释：去掉命中值末尾的空格再校验/替换（IBAN 的宽松字符类会吞掉后面的空格）。
    trim_ws: bool = False
    # 中文注释：覆盖全局最小长度（IPv4 只有 7 个字符）。
    min_length: int = _MIN_SECRET_LENGTH


def _rule(name: str, pattern: str, group: int = 0, *, require_entropy: bool = False, validate: Callable[[str], bool] | None = None, trim_ws: bool = False, min_length: int = _MIN_SECRET_LENGTH) -> Rule:
    return Rule(name, re.compile(pattern), group, require_entropy, validate, trim_ws, min_length)


# ----- 值校验（命中后还要过格式与校验位检查） -----


def _luhn_valid(value: str) -> bool:
    """银行卡/信用卡号的 Luhn 校验位；位数 16–19。"""

    digits = [int(char) for char in value if char.isdigit()]
    if not 16 <= len(digits) <= 19:
        return False
    total = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _iban_valid(value: str) -> bool:
    """国际银行账号（IBAN）的 mod-97 校验位。"""

    compact = re.sub(r"\s+", "", value).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", compact):
        return False
    rearranged = compact[4:] + compact[:4]
    number = "".join(
        str(ord(char) - 55) if char.isalpha() else char for char in rearranged
    )
    try:
        return int(number) % 97 == 1
    except ValueError:  # pragma: no cover - 正则已保证全是字母数字
        return False


# 中文注释：SSA 从不发放 000、666 和 900-999 号段，其余号段都可能真实存在；
# 组号（第 4-5 位）和序号（第 6-9 位）也不能全 0。
_SSN_AREAS: tuple[tuple[int, int], ...] = ((1, 666), (667, 900))


def _ssn_valid(value: str) -> bool:
    """美国社会安全号：只认实际会发放的号段。"""

    if not re.fullmatch(r"[0-9]{3}-?[0-9]{2}-?[0-9]{4}", value):
        return False
    compact = re.sub(r"[^0-9]", "", value)
    area = int(compact[:3])
    group = int(compact[3:5])
    serial = int(compact[5:9])
    if not any(low <= area < high for low, high in _SSN_AREAS):
        return False
    return group != 0 and serial != 0


# 中文注释：中国大陆居民身份证号的校验码（ISO 7064:1983, MOD 11-2）。
_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK_CHARS = "10X98765432"


def _china_id_valid(value: str) -> bool:
    """中国大陆 18 位身份证号：校验码和出生日期都对得上才认。"""

    if not re.fullmatch(r"\d{17}[\dXx]", value):
        return False
    total = sum(int(digit) * weight for digit, weight in zip(value[:17], _ID_WEIGHTS))
    if _ID_CHECK_CHARS[total % 11] != value[17].upper():
        return False
    birth = f"{value[6:10]}-{value[10:12]}-{value[12:14]}"
    return _birthday_valid(birth)


def _birthday_valid(value: str) -> bool:
    """生日值：年月日数字都落在合理范围。"""

    match = re.fullmatch(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})日?", value)
    if match is None:
        return False
    year, month, day = (int(part) for part in match.groups())
    if not 1900 <= year <= 2100:
        return False
    if not 1 <= month <= 12:
        return False
    days_in_month = (31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                     31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    return 1 <= day <= days_in_month[month - 1]


def _public_ipv4_valid(value: str) -> bool:
    """公网 IPv4：排除内网、本机、文档示例和保留地址段。"""

    try:
        octets = [int(part) for part in value.split(".")]
    except ValueError:
        return False
    if len(octets) != 4 or any(not 0 <= octet <= 255 for octet in octets):
        return False
    first, second, third = octets[0], octets[1], octets[2]
    if first >= 224:  # 组播 / 保留
        return False
    if first == 10:  # 10.0.0.0/8
        return False
    if first == 127:  # 本机回环
        return False
    if first == 169 and second == 254:  # 链路本地
        return False
    if first == 172 and 16 <= second <= 31:  # 172.16.0.0/12
        return False
    if first == 192 and second == 168:  # 192.168.0.0/16
        return False
    if first == 100 and 64 <= second <= 127:  # CGNAT 100.64.0.0/10
        return False
    if first == 192 and second == 0 and third in (0, 2):  # 文档示例
        return False
    if first == 198 and (second in (18, 19) or (second == 51 and third == 100)):  # 文档示例
        return False
    if first == 203 and second == 0 and third == 113:  # 文档示例
        return False
    if first == 233 and second == 252 and third == 0:  # 组播文档示例
        return False
    return True


def _ipv6_groups(value: str) -> list[int] | None:
    """把 IPv6 地址拆成 8 组整数；不合法返回 None。"""

    if value.count("::") > 1:
        return None
    if "::" in value:
        head, _, tail = value.partition("::")
        left = [int(group, 16) for group in head.split(":") if group]
        right = [int(group, 16) for group in tail.split(":") if group]
        missing = 8 - len(left) - len(right)
        if missing < 1:
            return None
        return left + [0] * missing + right
    groups = value.split(":")
    if len(groups) != 8:
        return None
    return [int(group, 16) for group in groups]


def _public_ipv6_valid(value: str) -> bool:
    """公网 IPv6：全球单播（2000::/3），排除文档示例 2001:db8::/32。"""

    groups = _ipv6_groups(value)
    if groups is None or len(groups) != 8:
        return False
    if not 0x2000 <= groups[0] <= 0x3FFF:
        return False
    return not (groups[0] == 0x2001 and groups[1] == 0x0DB8)


BUILTIN_RULES: tuple[Rule, ...] = (
    _rule(
        "private_key",
        r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----[\s\S]+?-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----",
    ),
    _rule("anthropic_key", r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
    _rule("openai_key", r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}"),
    _rule("xai_key", r"\bxai-[A-Za-z0-9]{20,}"),
    _rule("github_token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})"),
    _rule("gitlab_token", r"\bglpat-[A-Za-z0-9_\-]{20,}"),
    _rule("aws_access_key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    _rule(
        "aws_secret_key",
        r"(?i)\baws_?secret_?access_?key\b[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9/+=]{40})\b",
        1,
    ),
    _rule("google_api_key", r"\bAIza[0-9A-Za-z_\-]{35}"),
    _rule("slack_token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    _rule("stripe_key", r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"),
    _rule("npm_token", r"\bnpm_[A-Za-z0-9]{36}\b"),
    _rule("huggingface_token", r"\bhf_[A-Za-z0-9]{30,}\b"),
    _rule(
        "jwt",
        r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    ),
    _rule("bearer_token", r"(?i)\bBearer\s+([A-Za-z0-9._~+/=\-]{20,})", 1),
    _rule("url_password", r"(?i)\b[a-z][a-z0-9+.\-]*://[^/\s:@\"']+:([^/\s@\"']+)@", 1),
    _rule(
        "secret_assignment",
        r"(?i)(?:\b|_)(?:api[_-]?key|apikey|secret|token|password|passwd|pwd|access[_-]?key|"
        r"private[_-]?key|client[_-]?secret|auth[_-]?key|credential)s?\b[\"']?\s*[:=]\s*"
        r"[\"']?([^\s\"',;`(){}<>\[\]]{12,})",
        1,
        require_entropy=True,
    ),
    # ----- 个人信息 -----
    _rule(
        "ssh_target",
        r"(?i)\b(?:ssh|scp|sftp|rsync)\s+([A-Za-z0-9._\-]{1,64}@[A-Za-z0-9][A-Za-z0-9\-]*(?:\.[A-Za-z0-9][A-Za-z0-9\-]*)*)",
        1,
    ),
    _rule(
        "email",
        r"(?i)(?<![A-Za-z0-9._\-])(?!git@)([A-Za-z0-9._\-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)+(?![A-Za-z0-9\-]))",
        1,
    ),
    _rule("phone_number", r"(?<![0-9+])(?:\+?86[\s\-]?)?1[3-9][0-9]{9}(?![0-9])"),
    _rule(
        "bank_card",
        r"(?<!\d)[0-9](?:[0-9 \-]{14,18})[0-9](?!\d)",
        validate=_luhn_valid,
    ),
    _rule(
        "china_id",
        r"(?<!\d)\d{17}[\dXx](?!\d)",
        validate=_china_id_valid,
    ),
    _rule(
        "ssn",
        r"(?<![\d-])[0-9]{3}-[0-9]{2}-[0-9]{4}(?![\d-])",
        validate=_ssn_valid,
    ),
    _rule(
        "passport",
        r"(?i)(?:\bpassport\b|护照|パスポ一卜)\s*(?:number|no\.?|番号|号)?\s*[:：]?\s*([A-Za-z]{1,3}[0-9]{5,9}|\d{6,9})",
        1,
    ),
    _rule(
        "birthday",
        r"(?i)(?:生日|出生年月日|出生日期|出生|birthday|DOB|born)\s*(?:日期|年月日)?\s*[:：]?\s*(\d{4}[-/年.]\d{1,2}[-/月.]\d{1,2}日?)",
        1,
        validate=_birthday_valid,
    ),
    _rule(
        "iban",
        r"(?i)\b[A-Z]{2}\d{2}[A-Z0-9 ]{11,30}\b",
        validate=_iban_valid,
        trim_ws=True,
    ),
    _rule("mac_address", r"(?i)(?<![0-9A-Fa-f]:)(?<![0-9A-Fa-f-])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f](?::|-))"),
    _rule(
        "serial_number",
        r"(?i)(?:\bS/N\b|\bSN\b|\bSerial\s+(?:No\.?|Number)?\b|\b序列号\b)\s*[:：]?\s*(?=[A-Za-z0-9\-_./]*\d)([A-Za-z0-9][A-Za-z0-9\-_./]{3,31})",
        1,
    ),
    # ----- 本机身份与公网地址 -----
    _rule(
        "home_username",
        r"(?i)(?<![A-Za-z0-9_])(?:/home/|/Users/|C:\\Users\\)([A-Za-z0-9][A-Za-z0-9._\-]{1,63})(?=/|\\|\s|$)",
        1,
    ),
    _rule(
        "user_at_host",
        r"(?i)([A-Za-z0-9._\-]{1,64}@[A-Za-z0-9][A-Za-z0-9\-]*(?:\.[A-Za-z0-9][A-Za-z0-9\-]*)*)(?=:[\w./~\-]*[$#>])",
        1,
    ),
    _rule(
        "hostname_line",
        r"(?im)^\s*(?:hostname|user(?:name)?)\s*[:=]\s*([A-Za-z0-9][A-Za-z0-9._\-]{2,63})",
        1,
    ),
    _rule(
        "public_ipv4",
        r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])",
        validate=_public_ipv4_valid,
        min_length=7,
    ),
    _rule(
        "public_ipv6",
        r"(?<![0-9A-Fa-f:])(?:(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,7}:|(?:[0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,5}(?::[0-9A-Fa-f]{1,4}){1,2}|(?:[0-9A-Fa-f]{1,4}:){1,4}(?::[0-9A-Fa-f]{1,4}){1,3}|(?:[0-9A-Fa-f]{1,4}:){1,3}(?::[0-9A-Fa-f]{1,4}){1,4}|(?:[0-9A-Fa-f]{1,4}:){1,2}(?::[0-9A-Fa-f]{1,4}){1,5}|[0-9A-Fa-f]{1,4}:(?:(?::[0-9A-Fa-f]{1,4}){1,6})|:(?:(?::[0-9A-Fa-f]{1,4}){1,7}|:))(?![0-9A-Fa-f:])",
        validate=_public_ipv6_valid,
        min_length=3,
    ),
)

RULE_NAMES: tuple[str, ...] = tuple(rule.name for rule in BUILTIN_RULES)


def default_disabled_rules() -> tuple[str, ...]:
    """默认关闭的规则：公网 IP 命中率太高，默认不启用。"""

    return tuple(name for name in RULE_NAMES if name in DEFAULT_OFF_RULES)


def _shannon_entropy(value: str) -> float:
    counts = Counter(value)
    total = len(value)
    return -sum(count / total * math.log2(count / total) for count in counts.values())


def _looks_random(value: str) -> bool:
    """通用规则的值要像随机串：够长、熵够高、字母数字混合，不是变量引用。"""

    if len(value) < _GENERIC_MIN_LENGTH:
        return False
    if value.startswith(("$", "%", "process.env", "os.environ", "env.", "${")):
        return False
    if not (re.search(r"[A-Za-z]", value) and re.search(r"\d", value)):
        return False
    return _shannon_entropy(value) >= _GENERIC_MIN_ENTROPY


@dataclass(frozen=True)
class Finding:
    """一次命中：类别、在原文中的位置和替换后的占位符（不含原值，便于记日志）。"""

    kind: str
    start: int
    end: int
    placeholder: str


@dataclass
class RedactConfig:
    """``<state_dir>/redact/config.json`` 的内容；缺省即全部内置规则。"""

    disabled_rules: tuple[str, ...] = ()
    enabled_rules: tuple[str, ...] = ()
    extra_patterns: tuple[tuple[str, str], ...] = ()
    allow_patterns: tuple[str, ...] = ()
    literal_values: tuple[str, ...] = ()
    dotenv_files: tuple[str, ...] = ()
    scan_env: bool = True
    restore: bool = True
    prompt_action: str = "block"
    vault_ttl_days: float = DEFAULT_VAULT_TTL_DAYS
    upstreams: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        unknown = (set(self.disabled_rules) | set(self.enabled_rules)) - set(RULE_NAMES)
        if unknown:
            raise ValueError(f"未知的脱敏规则: {', '.join(sorted(unknown))}")
        overlap = set(self.disabled_rules) & set(self.enabled_rules)
        if overlap:
            raise ValueError(f"规则不能同时出现在 disabled_rules 和 enabled_rules: {', '.join(sorted(overlap))}")
        if self.prompt_action not in {"block", "warn"}:
            raise ValueError("prompt_action 只能是 block 或 warn")
        if self.vault_ttl_days <= 0:
            raise ValueError("vault_ttl_days 必须大于 0")
        for name, pattern in self.extra_patterns:
            if not re.fullmatch(r"[a-z0-9_]{1,32}", name):
                raise ValueError(f"自定义规则名只能用小写字母、数字和下划线: {name}")
            re.compile(pattern)
        for pattern in self.allow_patterns:
            re.compile(pattern)
        for name, url in self.upstreams.items():
            if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,64}", name):
                raise ValueError(f"上游名称不合法: {name}")
            if not url.startswith(("http://", "https://")):
                raise ValueError(f"上游地址必须是 http(s) URL: {name}")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> RedactConfig:
        def strings(key: str) -> tuple[str, ...]:
            value = raw.get(key, [])
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ValueError(f"{key} 必须是字符串列表")
            return tuple(value)

        extra = raw.get("extra_patterns", [])
        if not isinstance(extra, list):
            raise ValueError("extra_patterns 必须是列表")
        extra_pairs: list[tuple[str, str]] = []
        for item in extra:
            if not isinstance(item, Mapping) or not isinstance(item.get("name"), str) or not isinstance(item.get("pattern"), str):
                raise ValueError("extra_patterns 的每一项必须有字符串 name 和 pattern")
            extra_pairs.append((item["name"], item["pattern"]))
        upstreams = raw.get("upstreams", {})
        if not isinstance(upstreams, Mapping) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in upstreams.items()
        ):
            raise ValueError("upstreams 必须是 名称 → URL 的对象")
        ttl = raw.get("vault_ttl_days", DEFAULT_VAULT_TTL_DAYS)
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
            raise ValueError("vault_ttl_days 必须是数值")
        prompt_action = raw.get("prompt_action", "block")
        if not isinstance(prompt_action, str):
            raise ValueError("prompt_action 必须是字符串")
        return cls(
            disabled_rules=strings("disabled_rules"),
            enabled_rules=strings("enabled_rules"),
            extra_patterns=tuple(extra_pairs),
            allow_patterns=strings("allow_patterns"),
            literal_values=strings("literal_values"),
            dotenv_files=strings("dotenv_files"),
            scan_env=raw.get("scan_env", True) is not False,
            restore=raw.get("restore", True) is not False,
            prompt_action=prompt_action,
            vault_ttl_days=float(ttl),
            upstreams=dict(upstreams),
        )


def _read_dotenv_values(path: Path) -> list[str]:
    """从 dotenv 文件里取出值（只取值，变量名不算密钥）。"""

    values: list[str] = []
    try:
        text = path.expanduser().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        value = line.split("=", 1)[1].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values.append(value)
    return values


def _secret_env_values(env: Mapping[str, str]) -> list[str]:
    return [
        value
        for name, value in env.items()
        if _SECRET_ENV_NAME.search(name) and not _ENV_VALUE_SKIP.match(value.strip())
    ]


class RedactStore:
    """状态目录下的脱敏文件：配置、HMAC 密钥和密钥表。"""

    def __init__(self, state_dir: Path) -> None:
        self.root = Path(state_dir) / "redact"
        self.config_path = self.root / "config.json"
        self.key_path = self.root / "key"
        self.vault_path = self.root / "vault.json"
        self.lock_path = self.root / "vault.lock"
        self._thread_lock = threading.Lock()

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.root.chmod(0o700)
        except OSError:  # pragma: no cover - 平台相关
            pass

    def load_config(self) -> RedactConfig:
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return RedactConfig()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"无法读取脱敏配置 {self.config_path}: {error}") from error
        if not isinstance(raw, Mapping):
            raise ValueError("脱敏配置根节点必须是 JSON 对象")
        return RedactConfig.from_mapping(raw)

    def save_config_upstreams(self, updates: Mapping[str, str | None]) -> None:
        """只改配置里的 upstreams，保留用户手写的其它字段。"""

        self.ensure()
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = {}
        if not isinstance(raw, dict):
            raise ValueError("脱敏配置根节点必须是 JSON 对象")
        upstreams = dict(raw.get("upstreams") or {})
        for name, url in updates.items():
            if url is None:
                upstreams.pop(name, None)
            else:
                upstreams[name] = url
        raw["upstreams"] = upstreams
        RedactConfig.from_mapping(raw)
        _write_private_json(self.config_path, raw)

    def load_key(self) -> bytes:
        try:
            key = self.key_path.read_bytes()
            if len(key) >= 32:
                return key
        except FileNotFoundError:
            pass
        self.ensure()
        with self._locked():
            try:
                key = self.key_path.read_bytes()
                if len(key) >= 32:
                    return key
            except FileNotFoundError:
                pass
            key = secrets.token_bytes(32)
            temporary = self.key_path.with_suffix(".tmp")
            temporary.write_bytes(key)
            temporary.chmod(0o600)
            temporary.replace(self.key_path)
            return key

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            if fcntl is None:  # pragma: no cover - 平台相关
                yield
                return
            self.ensure()
            with open(self.lock_path, "a+") as handle:
                os.chmod(self.lock_path, 0o600)
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def read_vault(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.vault_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            key: value
            for key, value in raw.items()
            if isinstance(value, dict) and isinstance(value.get("value"), str)
        }

    def vault_mtime(self) -> float:
        try:
            return self.vault_path.stat().st_mtime
        except OSError:
            return 0.0

    def merge_vault(self, entries: Mapping[str, Mapping[str, Any]], ttl_days: float) -> None:
        """把新映射并入密钥表，同时清掉超过保留期的条目。"""

        if not entries:
            return
        self.ensure()
        with self._locked():
            vault = self.read_vault()
            vault.update({key: dict(value) for key, value in entries.items()})
            cutoff = time.time() - ttl_days * 86400
            vault = {
                key: value
                for key, value in vault.items()
                if float(value.get("last_seen") or 0) >= cutoff
            }
            _write_private_json(self.vault_path, vault)


def _write_private_json(path: Path, payload: Any) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


class Redactor:
    """把文本里的密钥换成占位符，或把占位符还原成原值。线程安全。"""

    def __init__(
        self,
        store: RedactStore,
        config: RedactConfig | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.store = store
        self.config = config if config is not None else store.load_config()
        self._key = store.load_key()
        # 中文注释：默认关闭的规则（公网 IP）始终先按关闭处理，再由 enabled_rules
        # 里点名的规则覆盖；否则只启用一条公网 IP 规则时，另一条会被意外打开。
        disabled = set(self.config.disabled_rules) | DEFAULT_OFF_RULES
        disabled -= set(self.config.enabled_rules)
        self._rules: list[Rule] = [rule for rule in BUILTIN_RULES if rule.name not in disabled]
        self._rules.extend(
            Rule(name, re.compile(pattern)) for name, pattern in self.config.extra_patterns
        )
        self._allow = [re.compile(pattern) for pattern in self.config.allow_patterns]
        literals = list(self.config.literal_values)
        for dotenv in self.config.dotenv_files:
            literals.extend(_read_dotenv_values(Path(dotenv)))
        if self.config.scan_env:
            literals.extend(_secret_env_values(os.environ if env is None else env))
        self._literals = {value for value in literals if len(value) >= _MIN_SECRET_LENGTH}
        self._lock = threading.Lock()
        self._vault: dict[str, dict[str, Any]] = {}
        self._vault_mtime = -1.0
        self._pending: dict[str, dict[str, Any]] = {}
        self._known_pattern: re.Pattern[str] | None = None
        self._reload_vault(force=True)

    # ----- 占位符与密钥表 -----

    def placeholder_for(self, value: str, kind: str) -> str:
        digest = hmac.new(self._key, value.encode("utf-8"), hashlib.sha256).hexdigest()[:10]
        return f"[[SECRET:{kind}:{digest}]]"

    def _reload_vault(self, *, force: bool = False) -> None:
        mtime = self.store.vault_mtime()
        if not force and mtime == self._vault_mtime:
            return
        vault = self.store.read_vault()
        with self._lock:
            self._vault = vault
            self._vault.update(self._pending)
            self._vault_mtime = mtime
            self._rebuild_known_pattern()

    def _rebuild_known_pattern(self) -> None:
        # 中文注释：见过的密钥值以后在任何地方出现都直接替换，不再依赖规则命中。
        values = {entry["value"] for entry in self._vault.values()} | self._literals
        values = {value for value in values if len(value) >= _MIN_SECRET_LENGTH}
        if not values:
            self._known_pattern = None
            return
        ordered = sorted(values, key=len, reverse=True)
        self._known_pattern = re.compile("|".join(re.escape(value) for value in ordered))

    def _kind_for_known(self, value: str) -> str:
        for entry in self._vault.values():
            if entry["value"] == value:
                return str(entry.get("kind") or "literal")
        return "literal"

    def flush(self) -> None:
        """把本进程新产生的映射写进密钥表（hook 退出前、代理每个请求后调用）。"""

        with self._lock:
            pending = self._pending
            self._pending = {}
        if pending and self.config.restore:
            self.store.merge_vault(pending, self.config.vault_ttl_days)
            with self._lock:
                self._vault_mtime = self.store.vault_mtime()

    # ----- 脱敏 -----

    def _allowed(self, value: str) -> bool:
        return any(pattern.search(value) for pattern in self._allow)

    @classmethod
    def without_store(cls) -> Redactor:
        """构造一个不读写密钥表的实例，只做规则匹配（本地展示用）。"""

        instance = cls.__new__(cls)
        instance._rules = [rule for rule in BUILTIN_RULES if rule.name not in DEFAULT_OFF_RULES]
        instance._allow = []
        instance._known_pattern = None
        instance._lock = threading.Lock()
        return instance

    def find(self, text: str) -> list[tuple[int, int, str, str]]:
        """返回不重叠的命中 ``(start, end, kind, value)``，按位置排序。"""

        if not text:
            return []
        protected = [match.span() for match in PLACEHOLDER_PATTERN.finditer(text)]
        # (start, end, kind, value, priority)；priority 越小越优先（0 为已知密钥，
        # 1+ 为规则在列表里的顺序，靠前的规则在同一位置命中时优先）。
        candidates: list[tuple[int, int, str, str, int]] = []
        with self._lock:
            known = self._known_pattern
        if known is not None:
            for match in known.finditer(text):
                candidates.append((match.start(), match.end(), self._kind_for_known(match.group(0)), match.group(0), 0))
        for priority, rule in enumerate(self._rules, start=1):
            for match in rule.pattern.finditer(text):
                start, end = match.span(rule.group)
                if start < 0 or end <= start:
                    continue
                value = text[start:end]
                if rule.trim_ws:
                    trimmed = value.rstrip()
                    # 中文注释：宽松字符类可能吞掉后面的普通单词（例如 IBAN 后面的
                    # " x"）；校验不过就逐个去掉末尾的空白+单词，直到校验通过。
                    while (
                        rule.validate is not None
                        and trimmed
                        and not rule.validate(trimmed)
                    ):
                        tail = re.search(r"\s+\S+$", trimmed)
                        if tail is None:
                            break
                        trimmed = trimmed[: tail.start()]
                    if len(trimmed) != len(value):
                        end = start + len(trimmed)
                        value = trimmed
                if len(value) < rule.min_length:
                    continue
                if rule.require_entropy and not _looks_random(value):
                    continue
                if rule.validate is not None and not rule.validate(value):
                    continue
                candidates.append((start, end, rule.name, value, priority))
        # 中文注释：同一开始位置时，规则列表里靠前的更具体（例如 url_password
        # 优先于 email），同样位置再按长度取最长。
        candidates.sort(key=lambda item: (item[0], item[4], -(item[1] - item[0])))
        chosen: list[tuple[int, int, str, str]] = []
        last_end = -1
        for start, end, kind, value, _priority in candidates:
            if start < last_end:
                continue
            if any(start < p_end and p_start < end for p_start, p_end in protected):
                continue
            if self._allowed(value):
                continue
            chosen.append((start, end, kind, value))
            last_end = end
        return chosen

    def redact(self, text: str) -> tuple[str, list[Finding]]:
        hits = self.find(text)
        if not hits:
            return text, []
        parts: list[str] = []
        findings: list[Finding] = []
        cursor = 0
        now = time.time()
        for start, end, kind, value in hits:
            placeholder = self.placeholder_for(value, kind)
            parts.append(text[cursor:start])
            parts.append(placeholder)
            cursor = end
            findings.append(Finding(kind, start, end, placeholder))
            with self._lock:
                entry = self._vault.get(placeholder)
                # 中文注释：每天最多刷新一次 last_seen，避免每个请求都写文件。
                if entry is None or now - float(entry.get("last_seen") or 0) > 86400:
                    record = {"value": value, "kind": kind, "last_seen": now}
                    self._vault[placeholder] = record
                    self._pending[placeholder] = record
                    if entry is None:
                        self._rebuild_known_pattern()
        parts.append(text[cursor:])
        return "".join(parts), findings

    def redact_json(self, value: Any, *, skip_keys: frozenset[str] = frozenset()) -> tuple[Any, list[Finding]]:
        """递归脱敏 JSON 值里的字符串；``skip_keys`` 下的值原样保留。"""

        findings: list[Finding] = []

        def walk(node: Any) -> Any:
            if isinstance(node, str):
                redacted, hits = self.redact(node)
                findings.extend(hits)
                return redacted
            if isinstance(node, list):
                return [walk(item) for item in node]
            if isinstance(node, dict):
                return {
                    key: (item if key in skip_keys else walk(item))
                    for key, item in node.items()
                }
            return node

        return walk(value), findings

    # ----- 还原 -----

    def lookup(self, placeholder: str) -> str | None:
        with self._lock:
            entry = self._vault.get(placeholder)
        if entry is None:
            self._reload_vault()
            with self._lock:
                entry = self._vault.get(placeholder)
        return None if entry is None else str(entry["value"])

    def restore(self, text: str, *, json_escape: bool = False) -> str:
        """把占位符换回原值；``json_escape`` 用于 JSON 字符串片段（工具参数增量）。"""

        if not self.config.restore or _PLACEHOLDER_PREFIX not in text:
            return text

        def replace(match: re.Match[str]) -> str:
            value = self.lookup(match.group(0))
            if value is None:
                return match.group(0)
            return json.dumps(value, ensure_ascii=False)[1:-1] if json_escape else value

        return PLACEHOLDER_PATTERN.sub(replace, text)

    def restore_json(self, value: Any, *, json_string_keys: frozenset[str] = frozenset()) -> Any:
        """递归还原；``json_string_keys`` 下的字符串本身是 JSON 文本，插入时要转义。"""

        if not self.config.restore:
            return value

        def walk(node: Any, key: str | None = None) -> Any:
            if isinstance(node, str):
                return self.restore(node, json_escape=key in json_string_keys)
            if isinstance(node, list):
                return [walk(item, key) for item in node]
            if isinstance(node, dict):
                return {name: walk(item, name) for name, item in node.items()}
            return node

        return walk(value)


def split_partial_tail(text: str) -> tuple[str, str]:
    """把末尾可能是半个占位符的部分切出来：``("abc", "[[SEC")``。"""

    index = text.rfind("[")
    if index < 0:
        return text, ""
    # 中文注释：完整占位符以 "]]" 结尾，不算半个；只看最后一个 "[" 之前一位起的尾巴。
    start = index - 1 if index > 0 and text[index - 1] == "[" else index
    tail = text[start:]
    if PLACEHOLDER_PATTERN.fullmatch(tail):
        return text, ""
    if _PARTIAL_TAIL.fullmatch(tail):
        return text[:start], tail
    return text, ""


def summarize(findings: Sequence[Finding]) -> str:
    """按类别汇总命中数，例如 ``github_token×2, jwt×1``（不含任何原值）。"""

    counts = Counter(finding.kind for finding in findings)
    return ", ".join(f"{kind}×{count}" for kind, count in sorted(counts.items()))


def redact_text_plain(text: str) -> str:
    """把文本里的敏感值换成 ``[REDACTED]``（不写密钥表、不需要状态目录）。

    供告警内容摘要等只展示、不还原的场景使用；规则与 ``Redactor`` 完全一致，
    但默认关闭的规则（公网 IP）同样默认关闭。
    """

    redactor = Redactor.without_store()
    hits = redactor.find(text)
    if not hits:
        return text
    parts: list[str] = []
    cursor = 0
    for start, end, _kind, _value in hits:
        parts.append(text[cursor:start])
        parts.append("[REDACTED]")
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)
