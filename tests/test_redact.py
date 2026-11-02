"""脱敏功能测试：规则与占位符、流式还原、本地代理、各 agent 的 hook 与配置安装。"""

from __future__ import annotations

import http.client
import http.server
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from a_token_monitor.redact.control import RedactControlError, RedactController
from a_token_monitor.redact.events import RedactEvent
from a_token_monitor.redact.hooks import handle_event, run_hook
from a_token_monitor.redact.install import MARKER, AgentPaths, Installer, InstallError, proxy_url
from a_token_monitor.redact.proxy import ProxyError, RedactProxyServer
from a_token_monitor.redact.rules import (
    PLACEHOLDER_PATTERN,
    RedactConfig,
    RedactStore,
    Redactor,
    split_partial_tail,
)
from a_token_monitor.redact.streaming import SseRestorer, iter_sse_events, restore_json_body


GITHUB = "ghp_" + "a1B2c3D4e5" * 4
ANTHROPIC = "sk-ant-api03-" + "Zq9" * 10
AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
# 中文注释：合成夹具，集中放在这里并逐行豁免隐私扫描，用例只引用常量。手机号没有官方
# 保留号段，只能用公开的通用示例号和全 0 尾号；身份证的地区码 999999 不存在，不会撞上真人。
FAKE_PHONE = "13800138000"  # privacy-allow: 公开的通用示例号码
FAKE_PHONE_ALT = "18600000000"  # privacy-allow: 合成的全 0 尾号
FAKE_CHINA_ID = "999999199001010016"  # privacy-allow: 地区码不存在的合成身份证号


class _TempState(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name)
        self.store = RedactStore(self.state)

    def redactor(self, **config: Any) -> Redactor:
        return Redactor(self.store, RedactConfig(**config), env={})


class RuleTests(_TempState):
    def test_builtin_rules_replace_known_formats(self) -> None:
        text = (
            f"gh={GITHUB} ant={ANTHROPIC} aws={AWS} "
            "url=https://bob:hunter2pass@db.example.com/x "
            "password = 'h7Gk2pQ9zX4mL1vB' "
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"
        )
        redacted, findings = self.redactor().redact(text)
        kinds = {finding.kind for finding in findings}
        self.assertEqual(
            kinds,
            {"github_token", "anthropic_key", "aws_access_key", "url_password", "secret_assignment", "private_key"},
        )
        for secret in (GITHUB, ANTHROPIC, AWS, "hunter2pass", "h7Gk2pQ9zX4mL1vB", "MIIEow"):
            self.assertNotIn(secret, redacted)
        self.assertIn("https://bob:[[SECRET:url_password:", redacted)

    def test_generic_assignment_skips_low_entropy_and_references(self) -> None:
        redactor = self.redactor()
        for text in (
            "password = changeme",
            "token = process.env.GITHUB_TOKEN",
            "api_key: ${API_KEY}",
            "secret = aaaaaaaaaaaaaaaa",
        ):
            with self.subTest(text=text):
                self.assertEqual(redactor.redact(text)[1], [])

    def test_placeholder_is_deterministic_and_restorable(self) -> None:
        first = self.redactor()
        redacted, _ = first.redact(f"token {GITHUB}")
        again, _ = first.redact(f"again {GITHUB}")
        self.assertEqual(
            PLACEHOLDER_PATTERN.search(redacted).group(0),  # type: ignore[union-attr]
            PLACEHOLDER_PATTERN.search(again).group(0),  # type: ignore[union-attr]
        )
        first.flush()
        # 中文注释：另一个进程（hook / 代理）读同一份密钥表也能还原。
        second = self.redactor()
        self.assertEqual(second.restore(redacted), f"token {GITHUB}")
        self.assertEqual(second.redact(f"token {GITHUB}")[0], redacted)

    def test_seen_values_are_replaced_without_rule_match(self) -> None:
        redactor = self.redactor()
        redactor.redact(f"GITHUB_TOKEN={GITHUB}")
        # 中文注释：已知密钥被切开或换了上下文也照样替换。
        redacted, findings = redactor.redact(f"echo {GITHUB}x")
        self.assertNotIn(GITHUB, redacted)
        self.assertEqual(len(findings), 1)

    def test_existing_placeholders_are_not_double_redacted(self) -> None:
        redactor = self.redactor()
        once, _ = redactor.redact(f"token {GITHUB}")
        twice, findings = redactor.redact(once)
        self.assertEqual(once, twice)
        self.assertEqual(findings, [])

    def test_literal_env_and_dotenv_values(self) -> None:
        dotenv = self.state / ".env"
        dotenv.write_text('DB_PASSWORD="plain-but-real-1"\n# comment\nEMPTY=\n', encoding="utf-8")
        redactor = Redactor(
            self.store,
            RedactConfig(dotenv_files=(str(dotenv),), literal_values=("corp-internal-key",)),
            env={"MY_SERVICE_TOKEN": "env-secret-value", "DEBUG": "true", "HOME": "/home/x"},
        )
        redacted, findings = redactor.redact(
            "plain-but-real-1 corp-internal-key env-secret-value /home/x true"
        )
        self.assertEqual(len(findings), 3)
        self.assertIn("/home/x true", redacted)

    def test_allow_patterns_and_disabled_rules(self) -> None:
        redactor = self.redactor(allow_patterns=(r"^AKIAABCD",), disabled_rules=("github_token",))
        redacted, findings = redactor.redact(f"{AWS} {GITHUB}")
        self.assertEqual(findings, [])
        self.assertEqual(redacted, f"{AWS} {GITHUB}")

    def test_config_validation(self) -> None:
        with self.assertRaises(ValueError):
            RedactConfig(disabled_rules=("nope",))
        with self.assertRaises(ValueError):
            RedactConfig(prompt_action="drop")
        with self.assertRaises(ValueError):
            RedactConfig.from_mapping({"upstreams": {"x": "ftp://a"}})
        config = RedactConfig.from_mapping(
            {"extra_patterns": [{"name": "corp_id", "pattern": r"CORP-\d{8}"}], "restore": False}
        )
        redacted, findings = Redactor(self.store, config, env={}).redact("id CORP-12345678")
        self.assertEqual([finding.kind for finding in findings], ["corp_id"])
        self.assertNotIn("CORP-12345678", redacted)

    def test_restore_disabled_keeps_placeholders_and_vault_empty(self) -> None:
        redactor = self.redactor(restore=False)
        redacted, _ = redactor.redact(f"t {GITHUB}")
        redactor.flush()
        self.assertEqual(redactor.restore(redacted), redacted)
        self.assertFalse(self.store.vault_path.exists())

    def test_vault_is_private(self) -> None:
        redactor = self.redactor()
        redactor.redact(GITHUB)
        redactor.flush()
        self.assertEqual(self.store.vault_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.key_path.stat().st_mode & 0o777, 0o600)

    def test_restore_json_escapes_inside_json_strings(self) -> None:
        redactor = self.redactor()
        literal = 'quote"and\\slash-value'
        redactor = Redactor(self.store, RedactConfig(literal_values=(literal,)), env={})
        redacted, _ = redactor.redact(literal)
        arguments = json.dumps({"cmd": f"echo {redacted}"})
        restored = redactor.restore(arguments, json_escape=True)
        self.assertEqual(json.loads(restored)["cmd"], f"echo {literal}")

    def test_split_partial_tail(self) -> None:
        cases = {
            "abc[[SEC": ("abc", "[[SEC"),
            "abc[": ("abc", "["),
            "x [[SECRET:jwt:12ab": ("x ", "[[SECRET:jwt:12ab"),
            "done [[SECRET:jwt:0123456789]]": ("done [[SECRET:jwt:0123456789]]", ""),
            "a[b": ("a[b", ""),
            "list[0]": ("list[0]", ""),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(split_partial_tail(text), expected)


class PersonalInfoRuleTests(_TempState):
    """个人信息与机器标识规则：邮箱、手机号、证件号、生日、MAC、序列号等。"""

    def redact(self, text: str) -> list[str]:
        return [finding.kind for finding in self.redactor().redact(text)[1]]

    def test_email(self) -> None:
        text = "mail me@example.com now; contact me@example.com again"
        redacted, findings = self.redactor().redact(text)
        self.assertEqual({finding.kind for finding in findings}, {"email"})
        self.assertNotIn("me@example.com", redacted)
        # 中文注释：localhost 不算邮箱域名；URL 里的密码不被 email 抢走。
        self.assertEqual(self.redact("user@localhost"), [])
        self.assertEqual(self.redact("https://bob:s3cr3t-pass-9@example.com/x"), ["url_password"])
        # 中文注释：git@github.com 的 SCP 形式不误伤。
        self.assertEqual(self.redact("git clone git@github.com:org/repo.git"), [])

    def test_phone_number(self) -> None:
        text = f"tel {FAKE_PHONE} x"
        redacted, findings = self.redactor().redact(text)
        self.assertEqual([finding.kind for finding in findings], ["phone_number"])
        self.assertNotIn(FAKE_PHONE, redacted)
        self.assertEqual(self.redact(FAKE_PHONE_ALT), ["phone_number"])
        # 中文注释：3-4-4 分组写法也是手机号。
        for sep in ("-", " "):
            grouped = sep.join((FAKE_PHONE[:3], FAKE_PHONE[3:7], FAKE_PHONE[7:]))
            self.assertEqual(self.redact(f"tel {grouped} x"), ["phone_number"], grouped)
        # 中文注释：非 1[3-9] 开头的 11 位数字不是手机号。
        self.assertEqual(self.redact("126" + "12345678"), [])

    def test_bank_card_luhn(self) -> None:
        card = "4111 1111 1111 1111"
        redacted, findings = self.redactor().redact(f"card {card} ok")
        self.assertEqual([finding.kind for finding in findings], ["bank_card"])
        self.assertNotIn(card, redacted)
        # 中文注释：校验位不对的 16 位数字不替换。
        self.assertEqual(self.redact("card " + "4111 1111 1111 1111"[:-1] + "2 ok"), [])

    def test_bank_card_grouped_19_digits_and_trailing_digits(self) -> None:
        # 中文注释：19 位卡号分组后同样要认。
        self.assertEqual(self.redact("card 6222 0212 3456 7890 128 ok"), ["bank_card"])
        # 中文注释：卡号后面跟一组无关数字时只替换卡号，无关数字保留。
        redacted, findings = self.redactor().redact("card 4111 1111 1111 1111 20 ok")
        self.assertEqual([finding.kind for finding in findings], ["bank_card"])
        self.assertTrue(redacted.endswith(" 20 ok"), redacted)

    def test_china_id_checksum(self) -> None:
        redacted, findings = self.redactor().redact(f"id {FAKE_CHINA_ID} x")
        self.assertEqual([finding.kind for finding in findings], ["china_id"])
        self.assertNotIn(FAKE_CHINA_ID, redacted)
        # 中文注释：校验码不对不替换（改末位数字，形状仍然合法）。
        wrong_check = FAKE_CHINA_ID[:-1] + ("1" if FAKE_CHINA_ID[-1] != "1" else "2")
        self.assertEqual(self.redact(f"id {wrong_check} x"), [])

    def test_ssn_only_issued_areas(self) -> None:
        valid = "123-45-6789"
        redacted, findings = self.redactor().redact(f"ssn {valid} x")
        self.assertEqual([finding.kind for finding in findings], ["ssn"])
        self.assertNotIn(valid, redacted)
        # 中文注释：000 / 666 / 900 号段、全 0 组号/序号都不认。
        for bad in ("000-12-3456", "666-12-3456", "900-12-3456", "123-00-6789", "123-45-0000"):
            self.assertEqual(self.redact(f"ssn {bad} x"), [], bad)

    def test_passport_after_keyword(self) -> None:
        first, second = "E12345678", "AB9876543"
        cases = {
            f"passport {first}": first,
            f"passport number {first}": first,
            f"护照：{first}": first,
            f"护照号 {second}": second,
            f"パスポ一卜 {first}": first,
            f"パスポート {first}": first,
            f"パスポート番号: {first}": first,
            f"护照号码：{first}": first,
            "passport no. 123456": "123456",
        }
        for text, secret in cases.items():
            with self.subTest(text=text):
                redacted, findings = self.redactor().redact(text)
                self.assertEqual([finding.kind for finding in findings], ["passport"])
                self.assertNotIn(secret, redacted)
        # 中文注释：单独的数字不是护照号。
        self.assertEqual(self.redact(f"my number is {first}"), [])
        # 中文注释：超长数字不是护照号，不能只替换前面一截。
        self.assertEqual(self.redact("passport 123456789012"), [])

    def test_birthday_only_after_keyword(self) -> None:
        first, second = "1990-01-01", "1985/12/31"
        cases = {
            f"生日 {first}": first,
            f"出生日期：{second}": second,
            f"DOB: {first}": first,
            f"born {first}": first,
            f"born on {first}": first,
            "出生于1990年1月2日": "1990年1月2日",
            "DOB: 01/02/1990": "01/02/1990",
            "DOB 25.12.1990": "25.12.1990",
        }
        for text, secret in cases.items():
            with self.subTest(text=text):
                redacted, findings = self.redactor().redact(text)
                self.assertEqual([finding.kind for finding in findings], ["birthday"])
                self.assertNotIn(secret, redacted)
        # 中文注释：单独的日期不动；非法日期不替换。
        self.assertEqual(self.redact("今天是 2024-01-01"), [])
        self.assertEqual(self.redact("生日 1990-13-01"), [])
        self.assertEqual(self.redact("DOB 13/25/1990"), [])
        self.assertEqual(self.redact("reborn 1990-01-01"), [])

    def test_iban_checksum(self) -> None:
        iban = "DE89 3704 0044 0532 0130 00"
        redacted, findings = self.redactor().redact(f"iban {iban} x")
        self.assertEqual([finding.kind for finding in findings], ["iban"])
        self.assertNotIn(iban, redacted)
        # 中文注释：校验位不对的 IBAN 不替换。
        self.assertEqual(self.redact(f"iban {'DE89 3704 0044 0532 0130 01'} x"), [])

    def test_mac_address(self) -> None:
        for mac in ("00:11:22:33:44:55", "AA-BB-CC-DD-EE-FF"):
            with self.subTest(mac=mac):
                redacted, findings = self.redactor().redact(f"mac {mac} x")
                self.assertEqual([finding.kind for finding in findings], ["mac_address"])
                self.assertNotIn(mac, redacted)
        # 中文注释：IPv6 地址不是 MAC。
        self.assertEqual(self.redact("2606:4700:4700::1111"), [])

    def test_serial_number_after_keyword(self) -> None:
        first, second = "ABC-123456", "XYZ-987654"
        cases = {
            f"S/N: {first}": first,
            f"SN: {second}": second,
            f"Serial Number: {first}": first,
            f"序列号：{second}": second,
            f"Serial: {first}": first,
            f"Serial No. {first}": first,
            f"serial_number={first}": first,
            f"设备序列号 {second}": second,
            f"序列号{second}": second,
            "S/N: AB12": "AB12",
        }
        for text, secret in cases.items():
            with self.subTest(text=text):
                redacted, findings = self.redactor().redact(text)
                self.assertEqual([finding.kind for finding in findings], ["serial_number"])
                self.assertNotIn(secret, redacted)
        # 中文注释：没有 S/N 关键字的编号不替换；纯字母没有数字的也不像序列号。
        self.assertEqual(self.redact(f"order {first}"), [])
        self.assertEqual(self.redact("S/N: ABCDEFGH"), [])
        self.assertEqual(self.redact("serial port 1234"), [])
        self.assertEqual(self.redact("serialize 1234"), [])

    def test_home_username_in_paths(self) -> None:
        red = self.redactor(enabled_rules=("home_username",))
        for user in ("develuser", "bob"):
            cases = (
                f"/home/{user}/project",
                f"/Users/{user}/app",
                "C:\\Users\\" + user + "\\x",
                f'cwd="/home/{user}"',
            )
            for text in cases:
                with self.subTest(text=text):
                    redacted, findings = red.redact(text)
                    self.assertEqual([finding.kind for finding in findings], ["home_username"])
                    self.assertNotIn(user, redacted)
                    # 中文注释：不足 8 位的用户名也要能从回复里还原。
                    self.assertEqual(red.restore(redacted), text)
        # 中文注释：/srv/app 不是用户主目录。
        self.assertEqual([f.kind for f in red.redact("/srv/app/data")[1]], [])

    def test_home_username_is_off_by_default(self) -> None:
        # 中文注释：智能体每次请求都会带上所在目录，这条规则必须显式开启。
        text = "/home/develuser/project"
        self.assertEqual(self.redactor().redact(text), (text, []))

    def test_user_at_host_and_ssh(self) -> None:
        login = "develuser" + "@server"
        redacted, findings = self.redactor().redact(login + ":~$")
        self.assertEqual([finding.kind for finding in findings], ["user_at_host"])
        self.assertNotIn(login, redacted)
        redacted, findings = self.redactor().redact("ssh " + login + " -p 22")
        self.assertEqual([finding.kind for finding in findings], ["ssh_target"])
        self.assertNotIn(login, redacted)

    def test_prompt_variants(self) -> None:
        login = "alice" + "@box"
        for text in (f"[{login} ~]$ ls", f"{login} ~ % ls", f"{login}:/srv/app# ls"):
            with self.subTest(text=text):
                redacted, findings = self.redactor().redact(text)
                self.assertEqual([finding.kind for finding in findings], ["user_at_host"])
                self.assertNotIn(login, redacted)

    def test_ssh_target_after_options(self) -> None:
        login = "alice" + "@box"
        for text in (f"ssh -i key.pem {login}", f"ssh -p 22 {login}", f"scp notes.txt {login}:/tmp"):
            with self.subTest(text=text):
                redacted, findings = self.redactor().redact(text)
                self.assertEqual([finding.kind for finding in findings], ["ssh_target"])
                self.assertNotIn(login, redacted)
        # 中文注释：git@ 是代码托管地址，不是个人账号。
        self.assertEqual(self.redact("ssh -T git" + "@github.com"), [])

    def test_hostname_and_user_lines(self) -> None:
        user, host = "develuser", "myhost123"
        for text, secret in (
            (f"hostname: {host}", host),
            (f"user = {user}", user),
            (f"  username: {user}", user),
            # 中文注释：不足 8 位的主机名和用户名同样要替换。
            ("hostname: myhost", "myhost"),
            ("user=alice", "alice"),
        ):
            with self.subTest(text=text):
                redacted, findings = self.redactor().redact(text)
                self.assertEqual([finding.kind for finding in findings], ["hostname_line"])
                self.assertNotIn(secret, redacted)
        self.assertEqual(self.redact('{"hostname": "x"}'), [])


class PublicIpRuleTests(_TempState):
    """公网 IP 默认关闭，显式 enabled_rules 开启后生效。"""

    def redactor(self, **config: Any) -> Redactor:
        return Redactor(self.store, RedactConfig(**config), env={})

    def test_public_ips_are_off_by_default(self) -> None:
        text = "connect 8.8.8.8 via 2606:4700:4700::1111 and 192.168.1.1"
        redacted, findings = self.redactor().redact(text)
        self.assertEqual(findings, [])
        self.assertEqual(redacted, text)

    def test_enabled_rules_turns_them_on(self) -> None:
        ip_rules = ("public_ipv4", "public_ipv6")
        red = self.redactor(enabled_rules=ip_rules)
        text = "connect 8.8.8.8 and 2606:4700:4700::1111"
        redacted, findings = red.redact(text)
        self.assertEqual({finding.kind for finding in findings}, set(ip_rules))
        self.assertNotIn("8.8.8.8", redacted)
        self.assertNotIn("2606:4700:4700::1111", redacted)

    def test_enabling_one_public_rule_keeps_the_other_off(self) -> None:
        from a_token_monitor.redact.rules import default_disabled_rules

        ipv4 = next(name for name in default_disabled_rules() if name.endswith("v4"))
        ipv6 = next(name for name in default_disabled_rules() if name.endswith("v6"))
        red = self.redactor(enabled_rules=(ipv4,))
        redacted, findings = red.redact("connect 8.8.8.8 and 2606:4700:4700::1111")
        self.assertEqual([finding.kind for finding in findings], [ipv4])
        self.assertNotIn("8.8.8.8", redacted)
        # 中文注释：只点名启用 IPv4 时，IPv6 仍然保持默认关闭。
        self.assertIn("2606:4700:4700::1111", redacted)
        self.assertNotEqual(ipv4, ipv6)

    def test_private_doc_and_reserved_addresses_never_redact(self) -> None:
        from a_token_monitor.redact.rules import default_disabled_rules

        red = self.redactor(enabled_rules=tuple(default_disabled_rules()))
        text = (
            "10.0.0.1 127.0.0.1 192.168.1.1 172.16.0.1 169.254.1.1 "
            "100.64.0.1 203.0.113.5 198.51.100.7 233.252.0.1 "  # privacy-allow: RFC 5771 文档保留组播地址
            "2001:db8::1 fe80::1 ::1"
        )
        redacted, findings = red.redact(text)
        self.assertEqual(findings, [])
        self.assertEqual(redacted, text)

    def test_default_disabled_rules_listing(self) -> None:
        from a_token_monitor.redact.rules import DEFAULT_OFF_RULES, default_disabled_rules

        self.assertEqual(set(default_disabled_rules()), set(DEFAULT_OFF_RULES))
        self.assertEqual(
            set(default_disabled_rules()) - set(RedactConfig().enabled_rules),
            set(DEFAULT_OFF_RULES),
        )


class PlainRedactTests(unittest.TestCase):
    """``redact_text_plain``：只展示不还原，不写密钥表。"""

    def test_redacts_without_vault_and_keeps_public_ips_off(self) -> None:
        from a_token_monitor.redact.rules import redact_text_plain

        text = f"mail me@example.com tel {FAKE_PHONE} ip 8.8.8.8"
        out = redact_text_plain(text)
        self.assertIn("[REDACTED]", out)
        self.assertNotIn("me@example.com", out)
        self.assertNotIn(FAKE_PHONE, out)
        # 中文注释：公网 IP 默认关闭；不产生任何 vault 占位符。主目录用户名同样默认关闭。
        self.assertIn("/home/develuser/x", redact_text_plain("/home/develuser/x"))
        self.assertIn("8.8.8.8", out)
        self.assertNotIn("[[SECRET:", out)

    def test_plain_text_without_hits_is_unchanged(self) -> None:
        from a_token_monitor.redact.rules import redact_text_plain

        self.assertEqual(redact_text_plain("hello world"), "hello world")
        self.assertEqual(redact_text_plain(""), "")


def _sse(*payloads: Any) -> list[bytes]:
    lines: list[bytes] = []
    for payload in payloads:
        if isinstance(payload, str):
            lines.extend([f"data: {payload}\n".encode(), b"\n"])
            continue
        lines.append(f"event: {payload.get('type', 'message')}\n".encode())
        lines.append(f"data: {json.dumps(payload)}\n".encode())
        lines.append(b"\n")
    return lines


def _restore_stream(redactor: Redactor, lines: list[bytes]) -> list[dict[str, Any]]:
    restorer = SseRestorer(redactor)
    output = b"".join(restorer.feed(item) for item in iter_sse_events(lines)) + restorer.finish()
    events = []
    for line in output.decode().splitlines():
        if line.startswith("data: ") and line[6:] != "[DONE]":
            events.append(json.loads(line[6:]))
    return events


class StreamingTests(_TempState):
    def setUp(self) -> None:
        super().setUp()
        self.red = self.redactor()
        self.placeholder = self.red.redact(GITHUB)[0]

    def test_anthropic_text_split_across_deltas(self) -> None:
        ph = self.placeholder
        events = _restore_stream(
            self.red,
            _sse(
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "use " + ph[:5]}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": ph[5:19]}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": ph[19:] + " ok"}},
                {"type": "content_block_stop", "index": 0},
            ),
        )
        text = "".join(e["delta"]["text"] for e in events if e["type"] == "content_block_delta")
        self.assertEqual(text, f"use {GITHUB} ok")
        self.assertEqual(events[-1]["type"], "content_block_stop")

    def test_anthropic_tool_json_is_escaped(self) -> None:
        literal = 'a"b\\c-secret-x'
        red = Redactor(self.store, RedactConfig(literal_values=(literal,)), env={})
        ph = red.redact(literal)[0]
        fragment = json.dumps({"command": f"echo {ph}"})
        events = _restore_stream(
            red,
            _sse(
                {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": fragment[:20]}},
                {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": fragment[20:]}},
                {"type": "content_block_stop", "index": 1},
            ),
        )
        joined = "".join(e["delta"]["partial_json"] for e in events if e["type"] == "content_block_delta")
        self.assertEqual(json.loads(joined)["command"], f"echo {literal}")

    def test_thinking_is_never_rewritten(self) -> None:
        ph = self.placeholder
        events = _restore_stream(
            self.red,
            _sse({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": ph}}),
        )
        self.assertEqual(events[0]["delta"]["thinking"], ph)

    def test_responses_deltas_and_done_events(self) -> None:
        ph = self.placeholder
        events = _restore_stream(
            self.red,
            _sse(
                {"type": "response.output_text.delta", "item_id": "m1", "output_index": 0, "content_index": 0, "delta": ph[:12]},
                {"type": "response.output_text.delta", "item_id": "m1", "output_index": 0, "content_index": 0, "delta": ph[12:]},
                {"type": "response.output_text.done", "item_id": "m1", "text": f"x {ph}"},
                {"type": "response.reasoning_summary_text.delta", "delta": ph},
                {"type": "response.completed", "response": {"output": [{"type": "message", "content": [{"type": "output_text", "text": ph}]}, {"type": "reasoning", "summary": [{"text": ph}]}]}},
            ),
        )
        self.assertEqual(events[0]["delta"], GITHUB)
        self.assertEqual(events[1]["text"], f"x {GITHUB}")
        self.assertEqual(events[2]["delta"], ph)
        output = events[3]["response"]["output"]
        self.assertEqual(output[0]["content"][0]["text"], GITHUB)
        self.assertEqual(output[1]["summary"][0]["text"], ph)

    def test_unfinished_partial_is_flushed_verbatim(self) -> None:
        events = _restore_stream(
            self.red,
            _sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "array["}}),
        )
        self.assertEqual(events[0]["delta"]["text"], "array[")

    def test_chat_completions_content_and_tool_arguments(self) -> None:
        ph = self.placeholder
        events = _restore_stream(
            self.red,
            _sse(
                {"choices": [{"index": 0, "delta": {"content": "k=" + ph[:9]}}]},
                {"choices": [{"index": 0, "delta": {"content": ph[9:]}}]},
                {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"a":"' + ph[:4]}}]}}]},
                {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": ph[4:] + '"}'}}]}, "finish_reason": "tool_calls"}]},
                "[DONE]",
            ),
        )
        content = "".join(e["choices"][0]["delta"].get("content") or "" for e in events)
        arguments = "".join(
            call["function"]["arguments"]
            for e in events
            for call in e["choices"][0]["delta"].get("tool_calls") or []
        )
        self.assertEqual(content, f"k={GITHUB}")
        self.assertEqual(json.loads(arguments)["a"], GITHUB)

    def test_non_json_and_comment_lines_pass_through(self) -> None:
        restorer = SseRestorer(self.red)
        output = b"".join(restorer.feed(item) for item in iter_sse_events([b": ping\n", b"\n", b"data: not json\n", b"\n"]))
        self.assertEqual(output, b": ping\n\ndata: not json\n\n")

    def test_json_body_restore(self) -> None:
        body = json.dumps({"content": [{"type": "text", "text": self.placeholder}, {"type": "thinking", "thinking": self.placeholder}]}).encode()
        restored = json.loads(restore_json_body(self.red, body))
        self.assertEqual(restored["content"][0]["text"], GITHUB)
        self.assertEqual(restored["content"][1]["thinking"], self.placeholder)
        self.assertEqual(restore_json_body(self.red, b"<html>"), b"<html>")


class _Upstream(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.seen.append({"path": self.path, "body": body.decode(), "headers": dict(self.headers)})
        text = json.loads(body)["messages"][0]["content"]
        if self.path.endswith(("/stream", "/bare-stream")):
            payload = b"".join(
                _sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}})
            )
            self.send_response(200)
            # 中文注释：ChatGPT 后端的 Codex 流不带 Content-Type。
            if self.path.endswith("/stream"):
                self.send_header("Content-Type", "text/event-stream")
        else:
            payload = json.dumps({"content": [{"type": "text", "text": text}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:
        pass


class ProxyTests(_TempState):
    def setUp(self) -> None:
        super().setUp()
        _Upstream.seen = []
        self.upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        base = f"http://127.0.0.1:{self.upstream.server_address[1]}/base"
        self.proxy = RedactProxyServer(self.redactor(), port=0, upstreams={"t": base})
        self.proxy.start_background()
        self.addCleanup(self.proxy.server_close)
        self.addCleanup(self.proxy.shutdown)

    def _request(self, path: str, body: bytes | None = None, headers: dict[str, str] | None = None) -> http.client.HTTPResponse:
        connection = http.client.HTTPConnection("127.0.0.1", self.proxy.server_address[1], timeout=10)
        self.addCleanup(connection.close)
        connection.request("POST", path, body=body, headers=headers or {})
        return connection.getresponse()

    def _post(self, path: str) -> http.client.HTTPResponse:
        body = json.dumps({"messages": [{"role": "user", "content": f"token {GITHUB}"}]}).encode()
        return self._request(path, body, {"Content-Type": "application/json", "Authorization": "Bearer subscription"})

    def test_request_is_redacted_and_headers_forwarded(self) -> None:
        response = self._post("/t/v1/messages?beta=true")
        self.assertEqual(response.status, 200)
        restored = json.loads(response.read())
        self.assertEqual(restored["content"][0]["text"], f"token {GITHUB}")
        seen = _Upstream.seen[-1]
        self.assertEqual(seen["path"], "/base/v1/messages?beta=true")
        self.assertNotIn(GITHUB, seen["body"])
        self.assertIn("[[SECRET:github_token:", seen["body"])
        self.assertEqual(seen["headers"].get("Authorization"), "Bearer subscription")

    def test_streaming_response_is_restored(self) -> None:
        response = self._post("/t/stream")
        body = response.read().decode()
        self.assertIn(GITHUB, body)
        self.assertNotIn("[[SECRET:", body)

    def test_stream_without_content_type_is_detected_from_accept(self) -> None:
        body = json.dumps({"messages": [{"role": "user", "content": f"token {GITHUB}"}]}).encode()
        response = self._request("/t/bare-stream", body, {"Content-Type": "application/json", "Accept": "text/event-stream"})
        text = response.read().decode()
        self.assertIn(GITHUB, text)
        self.assertNotIn("[[SECRET:", text)

    def test_gzip_request_body(self) -> None:
        import gzip

        body = gzip.compress(json.dumps({"messages": [{"role": "user", "content": GITHUB}]}).encode())
        response = self._request("/t/v1/messages", body, {"Content-Type": "application/json", "Content-Encoding": "gzip"})
        response.read()
        self.assertNotIn(GITHUB, _Upstream.seen[-1]["body"])
        self.assertNotIn("Content-Encoding", _Upstream.seen[-1]["headers"])

    def test_unknown_encoding_is_rejected_not_forwarded(self) -> None:
        response = self._request("/t/v1/messages", b"\x00\x01", {"Content-Encoding": "br"})
        self.assertEqual(response.status, 415)
        response.read()
        self.assertEqual(_Upstream.seen, [])

    def test_browser_and_unknown_routes_are_rejected(self) -> None:
        cases = (
            ("/t/x", {"Origin": "https://evil.example"}, 403),
            ("/t/x", {"Host": "evil.example"}, 403),
            ("/nope/x", {}, 404),
            ("/t/x", {"Upgrade": "websocket", "Connection": "Upgrade"}, 426),
        )
        for path, headers, status in cases:
            with self.subTest(headers=headers):
                response = self._request(path, b"{}", headers)
                response.read()
                self.assertEqual(response.status, status)
        self.assertEqual(_Upstream.seen, [])

    def test_proxy_records_interception_metadata(self) -> None:
        response = self._post("/t/v1/messages")
        response.read()
        snapshot = self.store.events.snapshot()
        self.assertEqual(snapshot["stats"]["events"], 1)
        event = snapshot["events"][0]
        self.assertEqual(event["agent"], "t")
        self.assertEqual(event["channel"], "request")
        self.assertEqual(event["action"], "redact")
        self.assertEqual(event["findings"], 1)
        self.assertEqual(event["kinds"], {"github_token": 1})
        self.assertGreater(event["payload_bytes"], event["secret_bytes"])
        self.assertNotIn(GITHUB, self.store.events.events_path.read_text(encoding="utf-8"))

    def test_refuses_non_loopback_bind(self) -> None:
        with self.assertRaises(ProxyError):
            RedactProxyServer(self.redactor(), host="0.0.0.0", port=0)


class EventLogTests(_TempState):
    """拦截记录只保存元数据：时间、来源、类别与字节数，不保存原值和上下文。"""

    def test_records_metadata_and_updates_cumulative_stats(self) -> None:
        redactor = self.redactor()
        address = "alert@example.com"
        text = f"token {GITHUB} contact {address}"
        _, findings = redactor.redact(text)
        self.assertGreaterEqual(len(findings), 2)
        redactor.record_event(
            agent="claude",
            channel="prompt",
            action="block",
            findings=findings,
            payload_bytes=len(text.encode("utf-8")),
        )
        snapshot = self.store.events.snapshot()
        stats = snapshot["stats"]
        self.assertEqual(stats["events"], 1)
        self.assertEqual(stats["findings"], len(findings))
        self.assertEqual(stats["blocked_events"], 1)
        self.assertEqual(stats["redacted_events"], 0)
        self.assertEqual(stats["blocked_bytes"], len(text.encode("utf-8")))
        self.assertEqual(stats["kinds"]["github_token"], 1)
        self.assertEqual(stats["kinds"]["email"], 1)
        event = snapshot["events"][0]
        self.assertEqual(event["agent"], "claude")
        self.assertEqual(event["channel"], "prompt")
        self.assertEqual(event["action"], "block")
        self.assertEqual(event["findings"], len(findings))
        self.assertEqual(event["secret_bytes"], sum(finding.size for finding in findings))
        for path in (self.store.events.events_path, self.store.events.stats_path):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            content = path.read_text(encoding="utf-8")
            self.assertNotIn(GITHUB, content)
            self.assertNotIn(address, content)
            self.assertNotIn('"value"', content)

    def test_events_are_newest_first_and_limit_applies_to_details_only(self) -> None:
        log = self.store.events
        for at in (100.0, 300.0, 200.0):
            log.append(
                RedactEvent(
                    at=at,
                    agent="codex",
                    channel="request",
                    action="redact",
                    findings=1,
                    kinds={"jwt": 1},
                    secret_bytes=3,
                    payload_bytes=10,
                )
            )
        snapshot = log.snapshot(limit=2)
        self.assertEqual([event["at"] for event in snapshot["events"]], [300.0, 200.0])
        self.assertEqual(snapshot["stats"]["events"], 3)
        self.assertEqual(snapshot["stats"]["first_at"], 100.0)
        self.assertEqual(snapshot["stats"]["last_at"], 300.0)
        self.assertEqual(snapshot["stats"]["payload_bytes"], 30)

    def test_old_event_lines_are_trimmed_but_totals_survive(self) -> None:
        from unittest.mock import patch

        log = self.store.events
        with patch("a_token_monitor.redact.events._MAX_EVENT_FILE_BYTES", 1), patch(
            "a_token_monitor.redact.events.MAX_EVENTS", 2
        ):
            for at in (100.0, 200.0, 300.0, 400.0):
                log.append(
                    RedactEvent(
                        at=at,
                        agent="claude",
                        channel="prompt",
                        action="block",
                        findings=1,
                        kinds={"jwt": 1},
                        secret_bytes=3,
                        payload_bytes=10,
                    )
                )
        snapshot = log.snapshot(limit=10)
        self.assertEqual([event["at"] for event in snapshot["events"]], [400.0, 300.0])
        self.assertEqual(snapshot["stats"]["events"], 4)
        self.assertEqual(snapshot["stats"]["blocked_events"], 4)

    def test_missing_stats_falls_back_to_retained_events(self) -> None:
        redactor = self.redactor()
        _, findings = redactor.redact(GITHUB)
        redactor.record_event(agent="grok", channel="tool_output", action="redact", findings=findings, payload_bytes=64)
        self.store.events.stats_path.unlink()
        snapshot = self.store.events.snapshot()
        self.assertEqual(snapshot["stats"]["events"], 1)
        self.assertEqual(snapshot["stats"]["findings"], 1)
        self.assertEqual(snapshot["stats"]["redacted_events"], 1)
        # 中文注释：stats.json 丢失后继续写事件，会先从明细重建再累加。
        _, again = redactor.redact(GITHUB)
        redactor.record_event(agent="grok", channel="request", action="redact", findings=again, payload_bytes=32)
        self.assertEqual(self.store.events.snapshot()["stats"]["events"], 2)


class HookTests(_TempState):
    def setUp(self) -> None:
        super().setUp()
        self.red = self.redactor()

    def test_prompt_with_secret_is_blocked(self) -> None:
        claude = handle_event({"hook_event_name": "UserPromptSubmit", "prompt": f"use {GITHUB}"}, self.red)
        self.assertEqual(claude["decision"], "block")  # type: ignore[index]
        self.assertTrue(claude["hookSpecificOutput"]["suppressOriginalPrompt"])  # type: ignore[index]
        self.assertNotIn(GITHUB, claude["reason"])  # type: ignore[index]
        codex = handle_event({"hook_event_name": "UserPromptSubmit", "turn_id": "t", "prompt": GITHUB}, self.red)
        self.assertEqual(codex, {"decision": "block", "reason": codex["reason"]})  # type: ignore[index]
        self.assertIsNone(handle_event({"hook_event_name": "UserPromptSubmit", "prompt": "hello"}, self.red))

    def test_prompt_warn_mode(self) -> None:
        red = self.redactor(prompt_action="warn")
        result = handle_event({"hook_event_name": "UserPromptSubmit", "prompt": GITHUB}, red)
        self.assertIn("systemMessage", result)  # type: ignore[operator]

    def test_hook_records_interception_metadata(self) -> None:
        handle_event({"hook_event_name": "UserPromptSubmit", "prompt": f"use {GITHUB}"}, self.red)
        handle_event({"hook_event_name": "PostToolUse", "tool_response": {"stdout": GITHUB}}, self.red)
        snapshot = self.store.events.snapshot()
        self.assertEqual(snapshot["stats"]["blocked_events"], 1)
        self.assertEqual(snapshot["stats"]["redacted_events"], 1)
        self.assertEqual([event["channel"] for event in snapshot["events"]], ["tool_output", "prompt"])
        self.assertEqual([event["action"] for event in snapshot["events"]], ["redact", "block"])
        self.assertNotIn(GITHUB, json.dumps(snapshot, ensure_ascii=False))

    def test_post_tool_output_replaced_per_agent(self) -> None:
        claude = handle_event(
            {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_response": {"stdout": f"t={GITHUB}", "stderr": "", "interrupted": False, "isImage": False}},
            self.red,
        )
        output = claude["hookSpecificOutput"]["updatedToolOutput"]  # type: ignore[index]
        self.assertEqual(set(output), {"stdout", "stderr", "interrupted", "isImage"})
        self.assertNotIn(GITHUB, output["stdout"])
        grok = handle_event(
            {"hookEventName": "post_tool_use", "hook_event_name": "PostToolUse", "toolResult": {"type": "ReadFile", "content": GITHUB}},
            self.red,
        )
        self.assertEqual(grok["hookSpecificOutput"]["updatedToolOutput"]["type"], "ReadFile")  # type: ignore[index]
        codex = handle_event(
            {"hook_event_name": "PostToolUse", "turn_id": "t", "tool_name": "Bash", "tool_response": f"out {GITHUB}"},
            self.red,
        )
        self.assertEqual(codex["decision"], "block")  # type: ignore[index]
        self.assertNotIn(GITHUB, codex["reason"])  # type: ignore[index]
        self.assertIn("[[SECRET:github_token:", codex["reason"])  # type: ignore[index]

    def test_pre_tool_restores_placeholders(self) -> None:
        ph = self.red.redact(GITHUB)[0]
        event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": f"echo {ph}", "timeout": 5}}
        claude = handle_event(event, self.red)["hookSpecificOutput"]  # type: ignore[index]
        self.assertEqual(claude["updatedInput"], {"command": f"echo {GITHUB}", "timeout": 5})
        # 中文注释：Claude 默认要用户确认，不能替用户批准。
        self.assertEqual(claude["permissionDecision"], "ask")
        bypass = handle_event({**event, "permission_mode": "bypassPermissions"}, self.red)
        self.assertEqual(bypass["hookSpecificOutput"]["permissionDecision"], "allow")  # type: ignore[index]
        codex = handle_event({**event, "turn_id": "t"}, self.red)["hookSpecificOutput"]  # type: ignore[index]
        self.assertEqual(codex["permissionDecision"], "allow")
        grok = handle_event(
            {"hookEventName": "pre_tool_use", "hook_event_name": "PreToolUse", "toolInput": {"command": ph}},
            self.red,
        )["hookSpecificOutput"]  # type: ignore[index]
        self.assertNotIn("permissionDecision", grok)
        self.assertEqual(grok["updatedInput"], {"command": GITHUB})
        self.assertIsNone(handle_event({**event, "tool_input": {"command": "ls"}}, self.red))

    def test_run_hook_fails_open(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()

        def broken() -> Redactor:
            raise RuntimeError("boom")

        code = run_hook(None, broken, io.StringIO('{"hook_event_name": "PostToolUse"}'), stdout, stderr)
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("RuntimeError", stderr.getvalue())

    def test_run_hook_persists_mapping_for_later_restore(self) -> None:
        stdout = io.StringIO()
        event = json.dumps({"hook_event_name": "PostToolUse", "tool_response": GITHUB})
        run_hook(None, lambda: Redactor(self.store, RedactConfig(), env={}), io.StringIO(event), stdout, io.StringIO())
        placeholder = json.loads(stdout.getvalue())["hookSpecificOutput"]["updatedToolOutput"]
        self.assertEqual(self.redactor().restore(placeholder), GITHUB)


class InstallTests(_TempState):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.state / "home"
        self.paths = AgentPaths.default(self.home)
        self.lines: list[str] = []
        self.installer = Installer(self.state, self.paths, port=8790, python="/usr/bin/python3", log=self.lines.append)

    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_claude_hook_and_proxy_keep_existing_settings(self) -> None:
        other = {"type": "command", "command": "notify.sh"}
        self._write(
            self.paths.claude_settings,
            json.dumps({"model": "opus", "env": {"FOO": "1"}, "hooks": {"Stop": [{"hooks": [other]}], "UserPromptSubmit": [{"matcher": "", "hooks": [other]}]}}),
        )
        self.installer.install("claude", "both")
        settings = json.loads(self.paths.claude_settings.read_text())
        self.assertEqual(settings["model"], "opus")
        self.assertEqual(settings["env"], {"FOO": "1", "ANTHROPIC_BASE_URL": proxy_url("claude")})
        self.assertEqual(len(settings["hooks"]["UserPromptSubmit"]), 2)
        self.assertEqual(settings["hooks"]["Stop"], [{"hooks": [other]}])
        # 中文注释：重复安装不叠加。
        self.installer.install("claude", "both")
        settings = json.loads(self.paths.claude_settings.read_text())
        self.assertEqual(len(settings["hooks"]["PostToolUse"]), 1)
        self.assertEqual(self.installer.status()["claude"], {"hook": True, "proxy": True})
        self.installer.uninstall("claude")
        settings = json.loads(self.paths.claude_settings.read_text())
        self.assertEqual(settings, {"model": "opus", "env": {"FOO": "1"}, "hooks": {"Stop": [{"hooks": [other]}], "UserPromptSubmit": [{"matcher": "", "hooks": [other]}]}})
        self.assertTrue(any(self.installer.backup_dir.iterdir()))

    def test_claude_proxy_refuses_foreign_base_url(self) -> None:
        self._write(self.paths.claude_settings, json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://gw.example"}}))
        with self.assertRaises(InstallError):
            self.installer.install("claude", "both")
        # 中文注释：冲突时 hook 也不能装了一半。
        self.assertNotIn("hooks", json.loads(self.paths.claude_settings.read_text()))

    def test_invalid_json_is_not_overwritten(self) -> None:
        self._write(self.paths.claude_settings, "{ broken")
        with self.assertRaises(InstallError):
            self.installer.install("claude", "hook")
        self.assertEqual(self.paths.claude_settings.read_text(), "{ broken")

    def test_codex_toml_round_trip(self) -> None:
        original = '[model_providers.x]\nname = "x"\n\n[features]\ncampaigns = false\n'
        self._write(self.paths.codex_config, original)
        self.installer.install("codex", "both")
        text = self.paths.codex_config.read_text()
        self.assertTrue(text.startswith(f'openai_base_url = "{proxy_url("codex")}" {MARKER}\n'))
        try:
            import tomllib
        except ImportError:  # pragma: no cover - Python 3.10
            tomllib = None  # type: ignore[assignment]
        if tomllib is not None:
            parsed = tomllib.loads(text)
            self.assertEqual(parsed["openai_base_url"], proxy_url("codex"))
            self.assertFalse(parsed["features"]["campaigns"])
        self.assertTrue(json.loads(self.paths.codex_hooks.read_text())["hooks"]["PreToolUse"])
        self.installer.uninstall("codex")
        self.assertEqual(self.paths.codex_config.read_text(), original)
        self.assertEqual(json.loads(self.paths.codex_hooks.read_text()), {})

    def test_codex_refuses_existing_base_url(self) -> None:
        self._write(self.paths.codex_config, 'openai_base_url = "https://x"\n')
        with self.assertRaises(InstallError):
            self.installer.install("codex", "proxy")

    def test_grok_hook_file_and_endpoint_table(self) -> None:
        self._write(self.paths.grok_config, "[ui]\ntheme = \"x\"\n")
        self.installer.install("grok", "both")
        hook = json.loads(self.paths.grok_hook_file.read_text())
        self.assertEqual(set(hook["hooks"]), {"UserPromptSubmit", "PreToolUse", "PostToolUse"})
        text = self.paths.grok_config.read_text()
        try:
            import tomllib
        except ImportError:  # pragma: no cover - Python 3.10
            tomllib = None  # type: ignore[assignment]
        if tomllib is not None:
            self.assertEqual(tomllib.loads(text)["endpoints"]["cli_chat_proxy_base_url"], proxy_url("grok"))
        self.installer.uninstall("grok")
        self.assertEqual(self.paths.grok_config.read_text(), "[ui]\ntheme = \"x\"\n")
        self.assertFalse(self.paths.grok_hook_file.exists())

    def test_existing_endpoints_table_is_reused(self) -> None:
        original = "[endpoints]\nother = 1\n"
        self._write(self.paths.grok_config, original)
        self.installer.install("grok", "proxy")
        self.assertEqual(self.paths.grok_config.read_text().count("[endpoints]"), 1)
        self.installer.uninstall("grok")
        self.assertEqual(self.paths.grok_config.read_text(), original)

    def test_hook_command_uses_absolute_state_dir(self) -> None:
        import os

        cwd = os.getcwd()
        os.chdir(self.state)
        self.addCleanup(os.chdir, cwd)
        installer = Installer(Path("relative-state"), self.paths, python="/usr/bin/python3", log=self.lines.append)
        self.assertIn(f'--state-dir "{(self.state / "relative-state").resolve()}"', installer.command)

    def test_same_hook_command_for_every_agent(self) -> None:
        # 中文注释：Grok 兼容加载 ~/.claude/settings.json，命令完全一致才能去重，避免跑两遍。
        self.installer.install("claude", "hook")
        self.installer.install("grok", "hook")
        claude = json.loads(self.paths.claude_settings.read_text())["hooks"]["PostToolUse"][0]["hooks"][0]
        grok = json.loads(self.paths.grok_hook_file.read_text())["hooks"]["PostToolUse"][0]["hooks"][0]
        self.assertEqual(claude, grok)


class _FakeProxy:
    """不监听端口的代理替身，记录启停。"""

    instances: list["_FakeProxy"] = []

    def __init__(self, redactor: Redactor, *, port: int, logger: Any = None) -> None:
        self.redactor = redactor
        self.server_address = ("127.0.0.1", port)
        self.upstreams: dict[str, str] = {}
        self.running = False
        self.closed = False
        _FakeProxy.instances.append(self)

    def start_background(self) -> None:
        self.running = True

    def shutdown(self) -> None:
        self.running = False

    def server_close(self) -> None:
        self.closed = True


class ControllerTests(_TempState):
    def setUp(self) -> None:
        super().setUp()
        _FakeProxy.instances = []
        self.paths = AgentPaths.default(self.state / "home")

    def controller(self, **kwargs: Any) -> RedactController:
        controller = RedactController(
            self.state, paths=self.paths, server_factory=_FakeProxy, **kwargs  # type: ignore[arg-type]
        )
        self.addCleanup(controller.close)
        return controller

    def test_web_setting_overrides_cli_and_hot_toggles_proxy(self) -> None:
        controller = self.controller(cli_proxy=False, cli_port=8790)
        controller.start()
        self.assertEqual(_FakeProxy.instances, [])
        snapshot = controller.apply({"action": "set-proxy", "enabled": True, "port": 18801})
        self.assertTrue(snapshot["proxy"]["running"])
        self.assertEqual(snapshot["proxy"]["source"], "web")
        self.assertEqual(_FakeProxy.instances[-1].server_address[1], 18801)
        controller.apply({"action": "set-proxy", "enabled": False, "port": 18801})
        self.assertFalse(_FakeProxy.instances[-1].running)
        # 中文注释：重启 daemon 后仍按 Web 配置（关闭），不受 --redact-proxy 影响。
        again = self.controller(cli_proxy=True)
        self.assertEqual(again.effective_proxy(), (False, 18801, "web"))
        again.apply({"action": "reset-proxy", "confirm": True})
        self.assertEqual(again.effective_proxy(), (True, 8790, "cli"))
        with self.assertRaises(RedactControlError):
            again.apply({"action": "reset-proxy"})

    def test_set_agent_modes_round_trip(self) -> None:
        controller = self.controller()
        snapshot = controller.apply({"action": "set-agent", "agent": "claude", "mode": "both"})
        claude = next(item for item in snapshot["agents"] if item["key"] == "claude")
        self.assertEqual(claude["mode"], "both")
        self.assertTrue(any("代理当前没有运行" in line for line in snapshot["messages"]))
        snapshot = controller.apply({"action": "set-agent", "agent": "claude", "mode": "hook"})
        claude = next(item for item in snapshot["agents"] if item["key"] == "claude")
        self.assertEqual(claude["mode"], "hook")
        settings = json.loads(self.paths.claude_settings.read_text())
        self.assertNotIn("ANTHROPIC_BASE_URL", settings.get("env", {}))
        controller.apply({"action": "set-agent", "agent": "claude", "mode": "off"})
        self.assertEqual(controller.snapshot()["agents"][0]["mode"], "off")
        for bad in ({"agent": "vim", "mode": "hook"}, {"agent": "claude", "mode": "all"}):
            with self.subTest(body=bad), self.assertRaises(RedactControlError):
                controller.apply({"action": "set-agent", **bad})

    def test_port_change_moves_installed_proxy_urls(self) -> None:
        controller = self.controller(cli_port=8790)
        controller.apply({"action": "set-agent", "agent": "grok", "mode": "proxy"})
        controller.apply({"action": "set-proxy", "enabled": True, "port": 18802})
        grok = next(item for item in controller.snapshot()["agents"] if item["key"] == "grok")
        self.assertEqual(grok["mode"], "proxy")
        self.assertIn("127.0.0.1:18802", self.paths.grok_config.read_text())
        self.assertNotIn("127.0.0.1:8790", self.paths.grok_config.read_text())

    def test_set_config_validates_and_reloads_running_proxy(self) -> None:
        controller = self.controller()
        controller.apply({"action": "set-proxy", "enabled": True, "port": 18803})
        before = _FakeProxy.instances[-1].redactor
        with self.assertRaises(RedactControlError):
            controller.apply({"action": "set-config", "config": {"disabled_rules": ["nope"]}})
        with self.assertRaises(RedactControlError):
            controller.apply({"action": "set-config", "config": {"upstreams": {}}})
        self.store.ensure()
        self.store.config_path.write_text(json.dumps({"upstreams": {"m": "http://127.0.0.1:1/v1"}}))
        controller.apply(
            {"action": "set-config", "config": {"disabled_rules": ["jwt"], "extra_patterns": [{"name": "corp_id", "pattern": "CORP-[0-9]{8}"}]}}
        )
        saved = json.loads(self.store.config_path.read_text())
        # 中文注释：只改提交的字段，用户手写的 upstreams 保留。
        self.assertEqual(saved["upstreams"], {"m": "http://127.0.0.1:1/v1"})
        self.assertEqual(saved["disabled_rules"], ["jwt"])
        self.assertIsNot(_FakeProxy.instances[-1].redactor, before)
        result = controller.apply({"action": "scan", "text": "id CORP-12345678"})
        self.assertEqual(result["kinds"], ["corp_id"])
        self.assertNotIn("CORP-12345678", result["redacted"])

    def test_enabled_rules_round_trip_and_default_off_listing(self) -> None:
        from a_token_monitor.redact.rules import DEFAULT_OFF_RULES

        ipv4 = next(name for name in DEFAULT_OFF_RULES if "v4" in name)
        controller = self.controller()
        snap = controller.snapshot()
        # 中文注释：快照里列出默认关闭的规则（公网 IP）。
        self.assertEqual(set(snap["default_disabled_rules"]), set(DEFAULT_OFF_RULES))
        controller.apply({"action": "set-config", "config": {"enabled_rules": [ipv4]}})
        saved = json.loads(self.store.config_path.read_text())
        self.assertEqual(saved["enabled_rules"], [ipv4])
        result = controller.apply({"action": "scan", "text": "connect 8.8.8.8"})
        self.assertEqual(result["kinds"], [ipv4])
        self.assertEqual(result["count"], 1)
        # 中文注释：enabled_rules 与 disabled_rules 互斥。
        with self.assertRaises(RedactControlError):
            controller.apply(
                {"action": "set-config", "config": {"enabled_rules": [ipv4], "disabled_rules": [ipv4]}}
            )

    def test_upstreams_and_read_only_mode(self) -> None:
        controller = self.controller()
        controller.apply({"action": "set-upstream", "name": "magpie", "url": "http://127.0.0.1:3425/v1"})
        names = {item["name"]: item for item in controller.snapshot()["upstreams"]}
        self.assertFalse(names["magpie"]["builtin"])
        self.assertTrue(names["claude"]["builtin"])
        controller.apply({"action": "set-upstream", "name": "magpie", "url": None})
        self.assertNotIn("magpie", {item["name"] for item in controller.snapshot()["upstreams"]})
        with self.assertRaises(RedactControlError):
            controller.apply({"action": "set-upstream", "name": "x", "url": "ftp://a"})
        readonly = self.controller(writable=False)
        with self.assertRaises(RedactControlError):
            readonly.apply({"action": "set-agent", "agent": "grok", "mode": "hook"})
        self.assertFalse(self.paths.grok_hook_file.exists())
        # 中文注释：只读模式下仍可以测试规则，测试不写文件。
        self.assertEqual(readonly.apply({"action": "scan", "text": GITHUB})["count"], 1)
        self.assertFalse(self.store.vault_path.exists())

    def test_snapshot_includes_interception_stats_without_values(self) -> None:
        controller = self.controller()
        redactor = Redactor(self.store, RedactConfig(), env={})
        _, findings = redactor.redact(f"token {GITHUB}")
        redactor.record_event(
            agent="claude",
            channel="prompt",
            action="block",
            findings=findings,
            payload_bytes=len(f"token {GITHUB}".encode("utf-8")),
        )
        snapshot = controller.snapshot()
        self.assertEqual(snapshot["stats"]["events"], 1)
        self.assertEqual(snapshot["events"][0]["agent"], "claude")
        self.assertNotIn(GITHUB, json.dumps(snapshot, ensure_ascii=False))
        # 中文注释：规则自测只是预览，不写拦截记录。
        controller.apply({"action": "scan", "text": GITHUB})
        self.assertEqual(controller.snapshot()["stats"]["events"], 1)

    def test_proxy_start_failure_is_reported_not_raised(self) -> None:
        def broken(*args: Any, **kwargs: Any) -> Any:
            raise OSError("Address already in use")

        controller = RedactController(self.state, paths=self.paths, server_factory=broken)
        snapshot = controller.apply({"action": "set-proxy", "enabled": True, "port": 18804})
        self.assertFalse(snapshot["proxy"]["running"])
        self.assertIn("Address already in use", snapshot["proxy"]["error"])


class DashboardRedactApiTests(_TempState):
    def _server(self, controller: RedactController | None) -> str:
        from a_token_monitor.dashboard import DashboardConfig, DashboardServer

        empty = {key: () for key in ("grok", "kimi", "dsh", "commandcode", "claude", "opencode", "cursor", "gemini", "qwen", "aider")}
        server = DashboardServer(registries={}, config=DashboardConfig(port=0), homes=empty, redact=controller)
        server.start()
        self.addCleanup(server.close)
        host, port = server.address
        return f"http://{host}:{port}/api/redact"

    def _call(self, url: str, payload: object | None = None) -> tuple[int, dict[str, Any]]:
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen

        data = None if payload is None else json.dumps(payload).encode()
        request = Request(url, data=data, headers={"Content-Type": "application/json"}, method="GET" if data is None else "POST")
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.loads(error.read().decode())

    def test_get_and_post_through_dashboard(self) -> None:
        paths = AgentPaths.default(self.state / "home")
        controller = RedactController(self.state, paths=paths, server_factory=_FakeProxy)  # type: ignore[arg-type]
        self.addCleanup(controller.close)
        url = self._server(controller)
        status, payload = self._call(url)
        self.assertEqual(status, 200)
        self.assertTrue(payload["available"])
        self.assertEqual([agent["key"] for agent in payload["agents"]], ["claude", "codex", "grok"])
        self.assertEqual(payload["stats"]["events"], 0)
        self.assertEqual(payload["events"], [])
        status, payload = self._call(url, {"action": "set-agent", "agent": "grok", "mode": "hook"})
        self.assertEqual(status, 200)
        self.assertTrue(paths.grok_hook_file.is_file())
        status, payload = self._call(url, {"action": "bogus"})
        self.assertEqual(status, 400)

    def test_read_only_returns_403(self) -> None:
        controller = RedactController(self.state, paths=AgentPaths.default(self.state / "home"), writable=False)
        url = self._server(controller)
        status, payload = self._call(url, {"action": "set-proxy", "enabled": True})
        self.assertEqual(status, 403)
        status, _ = self._call(url, {"action": "scan", "text": "hello"})
        self.assertEqual(status, 200)

    def test_unavailable_without_controller(self) -> None:
        url = self._server(None)
        self.assertEqual(self._call(url)[1]["available"], False)
        self.assertEqual(self._call(url, {"action": "scan", "text": "x"})[0], 503)


if __name__ == "__main__":
    unittest.main()
