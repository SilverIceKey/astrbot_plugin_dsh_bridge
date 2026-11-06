"""dsh_client 纯函数单元测试（不联网、不需要运行中的 DSH）。

运行方式（在插件目录下）:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dsh_client import DshClient, _b64url_decode, _is_loopback_host  # noqa: E402


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


SECRET_TEXT = "x" * 43
SECRET = bytes(32)


class SecretParsingTests(unittest.TestCase):
    def test_parse_unquoted_secret(self) -> None:
        text = (
            "version: 1\n"
            "records:\n"
            "  client-connection/browser-session:\n"
            "    kind: grant\n"
            "    payload:\n"
            "      version: 1\n"
            f"      secret: {SECRET_TEXT}\n"
            "  deepseek-account-platform/default:\n"
            "    kind: grant\n"
        )
        self.assertEqual(DshClient._extract_secret_text(text), SECRET_TEXT)

    def test_parse_quoted_secret(self) -> None:
        text = (
            "records:\n"
            "  client-connection/browser-session:\n"
            "    payload:\n"
            f"      secret: '{SECRET_TEXT}'\n"
        )
        self.assertEqual(DshClient._extract_secret_text(text), SECRET_TEXT)

    def test_missing_record(self) -> None:
        text = "records:\n  other/key:\n    payload:\n      secret: abc\n"
        self.assertIsNone(DshClient._extract_secret_text(text))


class AuthorityTests(unittest.TestCase):
    def test_http_authority(self) -> None:
        client = DshClient({})
        authority, secure = client._authority("http://127.0.0.1:53833")
        self.assertEqual(authority, "127.0.0.1:53833")
        self.assertFalse(secure)

    def test_https_authority(self) -> None:
        client = DshClient({})
        authority, secure = client._authority("https://127.0.0.1:53833")
        self.assertEqual(authority, "https://127.0.0.1:53833")
        self.assertTrue(secure)

    def test_ipv6_authority(self) -> None:
        client = DshClient({})
        self.assertEqual(client._authority("http://[::1]:53833")[0], "[::1]:53833")

    def test_default_port_stripped(self) -> None:
        client = DshClient({})
        self.assertEqual(client._authority("http://127.0.0.1:80")[0], "127.0.0.1")
        self.assertEqual(client._authority("https://127.0.0.1:443")[0], "https://127.0.0.1")

    def test_loopback_detection(self) -> None:
        for host in ("127.0.0.1", "127.1.2.3", "localhost", "::1"):
            self.assertTrue(_is_loopback_host(host), host)
        for host in ("10.0.0.2", "192.168.1.8", "example.com"):
            self.assertFalse(_is_loopback_host(host), host)


class CookieTests(unittest.TestCase):
    def test_cookie_matches_dsh_scheme(self) -> None:
        client = DshClient({})
        authority = "127.0.0.1:53833"
        cookie = client._mint_browser_cookie("http://127.0.0.1:53833", SECRET)
        name, _, value = cookie.partition("=")
        self.assertEqual(
            name, "dsh-auth-" + b64url(hashlib.sha256(authority.encode()).digest())
        )
        version, body, signature = value.split(".")
        self.assertEqual(version, "v1")
        payload = json.loads(_b64url_decode(body))
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["authority"], authority)
        self.assertGreater(payload["expiresAt"], payload["issuedAt"])
        self.assertLessEqual(
            payload["expiresAt"] - payload["issuedAt"], 24 * 3600 * 1000
        )
        expected = b64url(
            hmac.new(SECRET, body.encode("utf-8"), hashlib.sha256).digest()
        )
        self.assertEqual(signature, expected)

    def test_cookie_is_authority_bound(self) -> None:
        client = DshClient({})
        cookie_a = client._mint_browser_cookie("http://127.0.0.1:53833", SECRET)
        cookie_b = client._mint_browser_cookie("http://127.0.0.1:53834", SECRET)
        self.assertNotEqual(cookie_a.split("=", 1)[0], cookie_b.split("=", 1)[0])


class EventTextTests(unittest.TestCase):
    @staticmethod
    def _record(text: str) -> dict:
        return {
            "type": "event",
            "event": {
                "type": "assistant/message",
                "seq": 1,
                "time": 0,
                "data": {
                    "turn": 0,
                    "step": 0,
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "reasoning", "text": "internal"},
                            {"type": "text", "text": text},
                        ],
                    },
                },
            },
        }

    def test_newest_first_records_are_reordered(self) -> None:
        records = [self._record("second"), self._record("first")]
        self.assertEqual(DshClient._collect_assistant_text(records), "first\n\nsecond")

    def test_skip_empty_and_duplicate(self) -> None:
        records = [self._record("same"), self._record("same"), self._record("")]
        self.assertEqual(DshClient._collect_assistant_text(records), "same")

    def test_tool_call_message_yields_no_text(self) -> None:
        record = {
            "type": "event",
            "event": {
                "type": "assistant/message",
                "data": {
                    "message": {
                        "content": [{"type": "tool-call", "id": "1", "name": "pwsh"}]
                    }
                },
            },
        }
        self.assertEqual(DshClient._collect_assistant_text([record]), "")


class ConfigDefaultTests(unittest.TestCase):
    def test_ttl_and_scan_defaults(self) -> None:
        client = DshClient({})
        self.assertEqual(client.cache_ttl, 300.0)
        self.assertEqual(client.scan_concurrency, 512)
        self.assertAlmostEqual(client.connect_timeout, 0.3)

    def test_home_resolution(self) -> None:
        client = DshClient({"dsh_home": "~/.dsh-test"})
        self.assertEqual(client.credentials_path.name, ".credentials.yaml")
        self.assertIn(".dsh-test", str(client.credentials_path))

    def test_explicit_url_token(self) -> None:
        client = DshClient({"dsh_url": "http://127.0.0.1:53833/?token=abc123"})
        self.assertEqual(client._explicit_base(), "http://127.0.0.1:53833")
        self.assertEqual(client.token, "abc123")


if __name__ == "__main__":
    unittest.main()
