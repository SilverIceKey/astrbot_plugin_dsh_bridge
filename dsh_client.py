"""DSH Bridge - DeepSeek Harness 本地 API 客户端。

同时兼容两代 DSH 接口：

  1) DSH 0.2.2-rc.1+（新协议）
     - /api 需要浏览器会话认证：读取 DSH 数据目录 .credentials.yaml 中
       client-connection/browser-session 的签名密钥，自行签发 v1 浏览器 Cookie；
       或使用 `dsh web` 启动 URL 的 ?token= 经 /connect 换取 API Cookie。
     - 方法名使用斜杠命名、参数使用命名参数信封：
         POST /api/<method>
         {"type":"client-request","rpcId":...,"method":...,"payload":{"args":{...}}}
       session/create、session/list、session/state、session/prompt、session/wait、session/cancel
     - 完成判定：session/wait 等到空闲，output.records 中取 assistant/message 文本。

  2) 旧版 DSH（0.1.x / 早期 0.2.x，无认证）
     - GET / 直接返回含 "DeepSeek Harness" / "__DSH_BOOT__" 的 index.html；
     - 方法名使用点号命名、payload 直接是参数对象：
         session.create、session.list、session.prompt、session.history、session.cancel、host.describe
     - 完成判定：轮询 session.history，直到出现 turn/end 事件。

发现策略：
    dsh_url（可含 ?token=）→ dsh_port → DSH_WEB_URL 环境变量 → 历史默认端口 59934
    → 配置端口区间（默认 32768-65535，并兼容旧版默认区间）。先做 TCP 连通预筛，
    再 GET / 做指纹识别：
        401 + "dsh web authentication required" → 新版（需认证）
        200 + 页面 DSH 标记                    → 旧版（无需认证）
    端口发现结果缓存 cache_ttl_seconds，失败后自动重扫。

跨设备适配：
    - 本机认证需要能读到 DSH 数据目录下的 .credentials.yaml；
    - 跨设备认证使用 dsh_url/dsh_token；DSH Desktop 只监听 127.0.0.1，
      跨设备需隧道/代理，或改用 `dsh web --host <局域网IP> --trusted-host <来源>`
      的 CLI 实例。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import platform
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, quote, urlsplit

import aiohttp

# DSH 历史默认监听端口（0.1.x 桌面壳；0.2.x Desktop 改为随机临时端口）
DSH_DEFAULT_PORT = 59934
# 新默认扫描区间：覆盖 Windows/Linux 临时端口段
DEFAULT_PORT_RANGES = "32768-65535"
# 1.0.0 时期保存下来的旧默认区间：读取时自动迁移，避免升级后旧配置漏扫高端口
LEGACY_DEFAULT_PORT_RANGES = "59900-60100,49152-60000,32768-60999"
# 指纹标记
FINGERPRINT_TITLE = "DeepSeek Harness"
FINGERPRINT_MARKER = "__DSH_BOOT__"
AUTH_REQUIRED_MARKER = "dsh web authentication required"
# 凭据文件中的浏览器会话签名密钥记录
CREDENTIAL_RECORD = "client-connection/browser-session"
# 自签 Cookie 的有效期：1 天（DSH 允许的最小 cookieMaxAgeDays 也是 1 天）
COOKIE_MAX_AGE_MS = 24 * 60 * 60 * 1000
# 新版 session/wait 单次等待窗口（秒）
WAIT_SEGMENT_SECONDS = 20.0
# 旧版轮询兜底：判空后重试间隔（秒）
IDLE_RETRY_MIN_SECONDS = 0.2
IDLE_RETRY_MAX_SECONDS = 1.0
# 协议标识
PROTOCOL_MODERN = "modern"
PROTOCOL_LEGACY = "legacy"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _split_host_port(base_url: str) -> tuple[str, int | None, str]:
    parts = urlsplit(base_url)
    host = (parts.hostname or "").strip().lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    return host, port, parts.netloc.lower()


def _is_loopback_host(host: str) -> bool:
    host = (host or "").strip().lower()
    if host in ("localhost", "::1"):
        return True
    parts = host.split(".")
    if len(parts) != 4 or parts[0] != "127":
        return False
    return all(len(p) <= 3 and p.isdigit() and int(p) <= 255 for p in parts)


class DshError(Exception):
    """DSH 桥接层错误（带面向用户的友好信息）。"""


class DshClient:
    """DSH API 客户端：发现、认证、新旧协议 RPC、任务投递与结果等待。"""

    def __init__(
        self,
        config: dict,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config or {}
        self.logger = logger or logging.getLogger("astrbot")
        self._http: aiohttp.ClientSession | None = None
        self._discovery_lock = asyncio.Lock()
        self._base_url: str | None = None
        self._discovered_at: float = 0.0
        self._warm_task: asyncio.Task | None = None
        # 认证缓存：{"base": str, "cookie": "name=value", "mode": "credentials"|"token"}
        self._auth: dict[str, str] | None = None
        self._secret_cache: tuple[tuple[str, int, int], bytes] | None = None
        self._url_token: str | None = None
        self._release_version: str | None = None
        self._version_base: str | None = None
        # 协议与候选指纹类型缓存
        self._protocol: str | None = None
        self._protocol_base: str | None = None
        self._probe_kinds: dict[str, str] = {}

    # ------------------------------------------------------------------ 配置

    def _cfg(self, key: str, default):
        return self.config.get(key, default)

    @property
    def host(self) -> str:
        return str(self._cfg("dsh_host", "127.0.0.1") or "127.0.0.1")

    @property
    def port_override(self) -> int:
        try:
            return int(self._cfg("dsh_port", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def port_ranges(self) -> list[tuple[int, int]]:
        raw = str(self._cfg("port_ranges", DEFAULT_PORT_RANGES) or "").strip()
        # 升级迁移：1.0.0 保存的旧默认区间缺少 61000-65535
        if raw == LEGACY_DEFAULT_PORT_RANGES:
            raw = DEFAULT_PORT_RANGES
        ranges: list[tuple[int, int]] = []
        for part in raw.replace("，", ",").replace("；", ",").split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                lo_s, _, hi_s = part.partition("-")
                try:
                    lo, hi = int(lo_s.strip()), int(hi_s.strip())
                except ValueError:
                    continue
            else:
                try:
                    lo = hi = int(part)
                except ValueError:
                    continue
            lo, hi = max(1, min(lo, hi)), max(1, max(lo, hi))
            if 1 <= lo <= 65535 and 1 <= hi <= 65535:
                ranges.append((lo, hi))
        if not ranges:
            ranges = [(DSH_DEFAULT_PORT, DSH_DEFAULT_PORT)]
        return ranges

    @property
    def cache_ttl(self) -> float:
        try:
            return max(5.0, float(self._cfg("cache_ttl_seconds", 300) or 300))
        except (TypeError, ValueError):
            return 300.0

    @property
    def scan_concurrency(self) -> int:
        try:
            return max(8, min(1024, int(self._cfg("scan_concurrency", 256) or 256)))
        except (TypeError, ValueError):
            return 256

    @property
    def connect_timeout(self) -> float:
        try:
            return max(0.1, min(3.0, float(self._cfg("port_probe_timeout_seconds", 0.5) or 0.5)))
        except (TypeError, ValueError):
            return 0.5

    @property
    def dsh_home(self) -> Path:
        """DSH 数据目录（凭据文件所在目录）。"""
        raw = str(self._cfg("dsh_home", "") or "").strip()
        if raw:
            return Path(os.path.expanduser(raw))
        env = os.environ.get("DSH_HOME", "").strip()
        if env:
            return Path(os.path.expanduser(env))
        return Path.home() / ".dsh"

    @property
    def credentials_path(self) -> Path:
        return self.dsh_home / ".credentials.yaml"

    @property
    def token(self) -> str | None:
        """启动令牌：dsh_token 配置优先，其次 dsh_url 里的 ?token=。"""
        raw = str(self._cfg("dsh_token", "") or "").strip()
        if raw:
            return raw
        if self._url_token is None:
            self._explicit_base()
        return self._url_token

    # -------------------------------------------------------------- HTTP 层

    async def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30, connect=5),
                headers={"User-Agent": "astrbot-plugin-dsh-bridge/1.1.1"},
            )
        return self._http

    async def close(self) -> None:
        if self._warm_task and not self._warm_task.done():
            self._warm_task.cancel()
        if self._http and not self._http.closed:
            await self._http.close()
        self._http = None

    # -------------------------------------------------------------- 认证

    def _explicit_base(self) -> str | None:
        """解析 dsh_url 配置（可带 ?token=），返回去掉路径/查询串的基地址。"""
        raw = str(self._cfg("dsh_url", "") or "").strip()
        if not raw:
            return None
        if "://" not in raw:
            raw = "http://" + raw
        try:
            parts = urlsplit(raw)
        except ValueError:
            return None
        if not parts.netloc:
            return None
        token = parse_qs(parts.query).get("token")
        if token:
            self._url_token = token[0]
        scheme = (parts.scheme or "http").lower()
        return f"{scheme}://{parts.netloc}"

    def _authority(self, base_url: str) -> tuple[str, bool]:
        """计算 Cookie 的 authority 文本与是否使用 TLS（与 DSH 侧规则一致）。"""
        parts = urlsplit(base_url)
        secure = (parts.scheme or "http").lower() == "https"
        host = (parts.hostname or "").lower()
        host_text = f"[{host}]" if ":" in host else host
        try:
            port = parts.port
        except ValueError:
            port = None
        default_port = 443 if secure else 80
        authority = host_text if port in (None, default_port) else f"{host_text}:{port}"
        if secure:
            authority = f"https://{authority}"
        return authority, secure

    def _mint_browser_cookie(self, base_url: str, secret: bytes) -> str:
        """用凭据文件中的签名密钥自签浏览器会话 Cookie。"""
        authority, _secure = self._authority(base_url)
        now = int(time.time() * 1000)
        payload = {
            "version": 1,
            "authority": authority,
            "issuedAt": now,
            "expiresAt": now + COOKIE_MAX_AGE_MS,
        }
        body = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signature = _b64url(hmac.new(secret, body.encode("utf-8"), hashlib.sha256).digest())
        cookie_name = "dsh-auth-" + _b64url(hashlib.sha256(authority.encode("utf-8")).digest())
        return f"{cookie_name}=v1.{body}.{signature}"

    @staticmethod
    def _extract_secret_text(text: str) -> str | None:
        """从 .credentials.yaml 文本中提取浏览器会话签名密钥（base64url 文本）。"""
        try:
            import yaml  # type: ignore

            data = yaml.safe_load(text)
            record = ((data or {}).get("records") or {}).get(CREDENTIAL_RECORD)
            secret = ((record or {}).get("payload") or {}).get("secret")
            if isinstance(secret, str) and secret.strip():
                return secret.strip()
        except Exception:  # noqa: BLE001
            pass

        in_record = False
        record_indent = 0
        for line in text.splitlines():
            head = re.match(r"^(\s*)client-connection/browser-session:\s*$", line)
            if head:
                in_record = True
                record_indent = len(head.group(1))
                continue
            if not in_record:
                continue
            if line.strip() and not line.startswith(" " * (record_indent + 1)):
                in_record = False
                continue
            secret_match = re.match(
                r"^\s*secret:\s*['\"]?([A-Za-z0-9_\-+/=]+)['\"]?\s*$", line
            )
            if secret_match:
                return secret_match.group(1)
        return None

    def _load_secret(self) -> bytes | None:
        """读取并解码浏览器会话签名密钥；文件不存在或格式异常时返回 None。"""
        path = self.credentials_path
        try:
            stat = path.stat()
        except OSError:
            return None
        cache_key = (str(path), stat.st_mtime_ns, stat.st_size)
        if self._secret_cache and self._secret_cache[0] == cache_key:
            return self._secret_cache[1]
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None
        raw = self._extract_secret_text(text)
        if not raw:
            return None
        try:
            secret = _b64url_decode(raw)
        except Exception:  # noqa: BLE001
            return None
        if len(secret) != 32:
            return None
        self._secret_cache = (cache_key, secret)
        return secret

    async def _exchange_token(self, base_url: str, token: str) -> str:
        """用启动令牌换取 API Cookie（GET /connect?token=...&interactions=none）。"""
        url = f"{base_url}/connect?token={quote(token, safe='')}&interactions=none"
        session = await self._session()
        try:
            async with session.get(
                url,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 401:
                    raise DshError("DSH 启动令牌无效或已过期，请重新从 `dsh web` 输出中获取带 ?token= 的 URL。")
                if resp.status == 403:
                    raise DshError(self._trust_hint())
                if resp.status not in (301, 302, 303, 307, 308):
                    raise DshError(f"DSH 令牌换取 Cookie 失败：HTTP {resp.status}")
                for raw_cookie in resp.headers.getall("Set-Cookie", []):
                    name, _, rest = raw_cookie.partition("=")
                    if name.strip().startswith("dsh-auth-"):
                        return f"{name.strip()}={rest.split(';', 1)[0].strip()}"
        except aiohttp.ClientError as exc:
            raise DshError(f"无法连接 DSH（令牌换取）：{exc}") from exc
        raise DshError("DSH 未返回认证 Cookie（令牌换取失败）。")

    async def _ensure_auth(self, base_url: str, force: bool = False) -> str:
        """确保已获得新版 DSH 的认证 Cookie；本机凭据优先，失败时换启动令牌。"""
        if not force and self._auth and self._auth.get("base") == base_url:
            return self._auth["cookie"]

        _host, _port, _netloc = _split_host_port(base_url)
        local = _is_loopback_host(_host or self.host)
        secret = self._load_secret() if local else None
        token = self.token

        methods: list[str] = []
        if secret is not None:
            methods.append("credentials")
        if token:
            methods.append("token")
        if not methods:
            raise DshError(self._auth_hint(base_url))

        if force and len(methods) > 1:
            previous = (self._auth or {}).get("mode")
            methods.sort(key=lambda item: 0 if item != previous else 1)

        last_error: DshError | None = None
        for method in methods:
            try:
                if method == "credentials":
                    cookie = self._mint_browser_cookie(base_url, secret)  # type: ignore[arg-type]
                else:
                    cookie = await self._exchange_token(base_url, str(token))
            except DshError as exc:
                last_error = exc
                continue
            self._auth = {"base": base_url, "cookie": cookie, "mode": method}
            return cookie
        raise last_error or DshError(self._auth_hint(base_url))

    def _auth_hint(self, base_url: str) -> str:
        _host, _port, _netloc = _split_host_port(base_url)
        local = _is_loopback_host(_host or self.host)
        if local:
            return (
                f"DSH 要求认证，但未找到可用的凭据文件：{self.credentials_path}。"
                "请确认 dsh_home 指向 DSH 数据目录（默认取环境变量 DSH_HOME 或 ~/.dsh）；"
                "或者把 `dsh web` 启动输出中带 ?token= 的完整 URL 填入 dsh_url（也可只把 token 填入 dsh_token）。"
            )
        return (
            "跨设备访问 DSH 需要认证：请把 `dsh web` 启动输出中带 ?token= 的完整 URL 填入 dsh_url，"
            "或把 token 填入 dsh_token；同时 DSH 端需以 --trusted-host 放行本插件所在地址。"
        )

    def _trust_hint(self) -> str:
        return (
            "DSH 拒绝了请求来源（HTTP 403：来源地址不在信任列表）。"
            "DSH 只信任回环地址与自身绑定地址；跨设备使用时，请在 DSH 启动参数中加入 "
            "--trusted-host <本插件访问 DSH 时使用的 host:port>。"
        )

    async def _release_version_of(self, base_url: str) -> str | None:
        """新版 DSH 认证后读取版本（index.html 的 __DSH_BOOT__.release.version）。"""
        if self._version_base == base_url and self._release_version:
            return self._release_version
        try:
            cookie = await self._ensure_auth(base_url)
            session = await self._session()
            async with session.get(
                base_url + "/",
                headers={"Cookie": cookie},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                html = (await resp.text(errors="ignore"))[:60000]
        except (DshError, aiohttp.ClientError, asyncio.TimeoutError):
            return None
        match = re.search(r'"release"\s*:\s*\{[^{}]*"version"\s*:\s*"([^"]+)"', html)
        if match:
            self._release_version = match.group(1)
            self._version_base = base_url
            return self._release_version
        return None

    # -------------------------------------------------------------- RPC

    async def call(
        self,
        method: str,
        payload: dict | None = None,
        timeout: float = 30.0,
    ) -> Any:
        """按检测到的协议调用一个 DSH 方法（新版斜杠命名，旧版点号命名）。"""
        base = await self.discover()
        protocol = await self._ensure_protocol(base)
        if protocol == PROTOCOL_LEGACY:
            legacy_method = method.replace("/", ".")
            legacy_payload = self._modern_args_to_legacy(payload or {})
            return await self._legacy_rpc(base, legacy_method, legacy_payload, timeout)
        return await self._modern_rpc(base, method, payload or {}, timeout)

    @staticmethod
    def _modern_args_to_legacy(args: dict) -> dict:
        if set(args.keys()) == {"_request"}:
            return {}
        if len(args) == 1 and "request" in args and isinstance(args["request"], dict):
            return dict(args["request"])
        return dict(args)

    async def _modern_rpc(
        self,
        base_url: str,
        method: str,
        args: dict,
        timeout: float = 30.0,
        allow_unauth: bool = False,
    ) -> Any:
        """新版 RPC：斜杠方法名 + payload.args 命名参数 + Cookie 认证。"""
        body = {
            "type": "client-request",
            "rpcId": str(uuid.uuid4()),
            "method": method,
            "payload": {"args": args or {}},
        }
        session = await self._session()
        last_error: DshError | None = None
        for attempt in range(2):
            cookie: str | None = None
            auth_error: DshError | None = None
            try:
                cookie = await self._ensure_auth(base_url, force=attempt > 0)
            except DshError as exc:
                auth_error = exc
                if not allow_unauth:
                    raise
            headers = {"Accept": "application/json"}
            if cookie:
                headers["Cookie"] = cookie
            try:
                async with session.post(
                    f"{base_url}/api/{method}",
                    json=body,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status == 401:
                        last_error = auth_error or DshError(
                            "DSH 拒绝了认证（HTTP 401）。"
                            "请确认 dsh_home 指向正在运行的 DSH 实例的数据目录，"
                            "或重新获取 `dsh web` 输出中的启动令牌。"
                        )
                        self._auth = None
                        if cookie and attempt == 0:
                            continue
                        raise last_error
                    if resp.status == 403:
                        raise DshError(self._trust_hint())
                    if resp.status == 404:
                        raise DshError(
                            f"DSH 不存在方法 {method}（HTTP 404），可能是旧版本或未启用该能力"
                        )
                    if resp.status != 200:
                        detail = (await resp.text(errors="ignore"))[:200].strip()
                        raise DshError(
                            f"DSH API 返回 HTTP {resp.status}（{method} @ {base_url}）：{detail}"
                        )
                    raw = await resp.read()
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        content_type = resp.headers.get("Content-Type", "unknown")
                        snippet = raw[:200].decode("utf-8", "replace").replace("\n", " ")
                        raise DshError(
                            f"DSH API 响应不是合法 JSON（{method} @ {base_url}，"
                            f"HTTP {resp.status}，Content-Type={content_type}）：{snippet!r}"
                        ) from exc
            except asyncio.TimeoutError as exc:
                raise DshError(f"DSH API 调用超时（{method}）") from exc
            except aiohttp.ClientError as exc:
                raise DshError(f"无法连接 DSH（{method}）：{exc}") from exc

            result = data.get("result") if isinstance(data, dict) else None
            if not isinstance(result, dict):
                raise DshError(f"DSH API 响应格式异常（{method}）")
            if not result.get("ok"):
                error = result.get("error") or {}
                code = str(error.get("code") or "unknown")
                message = str(error.get("message") or error.get("msg") or code)
                raise DshError(f"DSH 拒绝了 {method}（{code}）：{message}")
            value = result.get("value")
            return {} if value is None else value
        raise last_error or DshError(f"DSH 认证失败（{method}）")

    async def _legacy_rpc(
        self,
        base_url: str,
        method: str,
        payload: dict | None = None,
        timeout: float = 30.0,
    ) -> Any:
        """旧版 RPC：点号方法名 + 扁平 payload + 无认证。"""
        body = {
            "type": "client-request",
            "rpcId": str(uuid.uuid4()),
            "method": method,
            "payload": payload or {},
        }
        session = await self._session()
        try:
            async with session.post(
                f"{base_url}/api/{method}",
                json=body,
                headers={"Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status == 401:
                    raise DshError(
                        "DSH 要求认证（旧协议接口返回 401）。"
                        "请使用新版 DSH，或配置 dsh_url/dsh_token 后重试。"
                    )
                if resp.status == 403:
                    raise DshError(self._trust_hint())
                if resp.status == 404:
                    raise DshError(f"旧版 DSH 不存在方法 {method}（HTTP 404）")
                if resp.status != 200:
                    detail = (await resp.text(errors="ignore"))[:200].strip()
                    raise DshError(
                        f"DSH API 返回 HTTP {resp.status}（{method} @ {base_url}）：{detail}"
                    )
                raw = await resp.read()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as exc:
                    content_type = resp.headers.get("Content-Type", "unknown")
                    snippet = raw[:200].decode("utf-8", "replace").replace("\n", " ")
                    raise DshError(
                        f"DSH API 响应不是合法 JSON（{method} @ {base_url}，"
                        f"HTTP {resp.status}，Content-Type={content_type}）：{snippet!r}"
                    ) from exc
        except asyncio.TimeoutError as exc:
            raise DshError(f"DSH API 调用超时（{method}）") from exc
        except aiohttp.ClientError as exc:
            raise DshError(f"无法连接 DSH（{method}）：{exc}") from exc

        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, dict):
            raise DshError(f"DSH API 响应格式异常（{method}）")
        if not result.get("ok"):
            code = result.get("code") or result.get("error") or "unknown"
            message = result.get("message") or result.get("msg") or str(code)
            raise DshError(f"DSH 拒绝了 {method}（{code}）：{message}")
        value = result.get("value")
        return {} if value is None else value

    async def _ensure_protocol(self, base_url: str) -> str:
        """检测 / 缓存当前 DSH 使用新版还是旧版协议。"""
        if self._protocol and self._protocol_base == base_url:
            return self._protocol

        kind = self._probe_kinds.get(base_url)
        if kind is None:
            kind = await self._probe(base_url)

        modern_ok = False
        try:
            await self._modern_rpc(
                base_url, "session/list", {"_request": {}}, timeout=10, allow_unauth=True
            )
            modern_ok = True
        except DshError:
            modern_ok = False

        if modern_ok:
            protocol = PROTOCOL_MODERN
        else:
            legacy_ok = False
            try:
                await self._legacy_rpc(base_url, "session.list", {}, timeout=10)
                legacy_ok = True
            except DshError:
                legacy_ok = False
            protocol = PROTOCOL_LEGACY if legacy_ok or kind != "modern" else PROTOCOL_MODERN

        self._protocol = protocol
        self._protocol_base = base_url
        self.logger.info(
            f"[DSH Bridge] DSH 协议：{'旧版兼容模式' if protocol == PROTOCOL_LEGACY else '新版 0.2.x'}"
        )
        return protocol

    # -------------------------------------------------------------- 端口发现

    async def discover(
        self, force: bool = False, progress: Callable[[str], Awaitable[None]] | None = None
    ) -> str:
        """返回形如 http://127.0.0.1:53833 的基地址。未找到抛 DshError。"""
        if not force and self._base_url and (time.monotonic() - self._discovered_at) < self.cache_ttl:
            return self._base_url
        async with self._discovery_lock:
            if not force and self._base_url and (time.monotonic() - self._discovered_at) < self.cache_ttl:
                return self._base_url
            # 缓存仍有效：只要指纹还在就算可用（不因认证失败丢弃缓存）
            if self._base_url:
                kind = await self._probe(self._base_url)
                if kind:
                    self._probe_kinds[self._base_url] = kind
                    self._discovered_at = time.monotonic()
                    return self._base_url
            self._base_url = None
            self._auth = None
            self._protocol = None
            self._protocol_base = None
            found = await self._scan(progress=progress)
            if found is None:
                raise DshError(self._not_found_hint())
            self._base_url = found
            self._discovered_at = time.monotonic()
            self.logger.info(
                f"[DSH Bridge] 已发现 DSH: {found}"
                f"（指纹：{self._probe_kinds.get(found, 'unknown')}）"
            )
            return found

    def _not_found_hint(self) -> str:
        raw_ranges = str(self._cfg("port_ranges", DEFAULT_PORT_RANGES) or "默认")
        hint = (
            f"未能在本机发现 DeepSeek Harness（“本机”指运行 AstrBot 的机器："
            f"{platform.node()} / {sys.platform}）。请确认："
            "1) DSH Desktop / dsh web 正在运行；"
            f"2) 当前配置：dsh_host={self.host}，dsh_port={self.port_override}，"
            f"dsh_url={self._cfg('dsh_url', '') or '（空）'}，"
            f"扫描区间={raw_ranges}，DSH数据目录={self.dsh_home}；"
            "3) 不确定端口时，把 DSH 窗口地址栏或 `dsh web` 输出里的完整 URL"
            "（形如 http://127.0.0.1:<端口>/?token=...）填入 dsh_url；"
            "4) DSH Desktop 0.2.x 只监听 127.0.0.1，若 AstrBot 与 DSH 不在同一台机器，"
            "需要 SSH 隧道/反向代理，或改用 `dsh web --host <局域网IP> --trusted-host <来源>`。"
        )
        return hint

    async def _probe(self, base_url: str) -> str | None:
        """指纹探测：返回 'modern'（401 认证提示）/ 'legacy'（200 页面标记）/ None。"""
        try:
            session = await self._session()
            async with session.get(
                base_url + "/",
                timeout=aiohttp.ClientTimeout(total=2.5),
            ) as resp:
                if resp.status == 401:
                    text = (await resp.text(errors="ignore"))[:4000]
                    if AUTH_REQUIRED_MARKER in text:
                        return "modern"
                    return None
                if resp.status != 200:
                    return None
                head = (await resp.text(errors="ignore"))[:20000]
                if FINGERPRINT_TITLE in head or FINGERPRINT_MARKER in head:
                    return "legacy"
                return None
        except Exception:
            return None

    async def _api_works(self, base_url: str) -> bool:
        """校验候选地址的 /api 是否真的返回 JSON-RPC 结果。

        200 页面指纹并不等于 DSH API：前端静态页、开发服务器或反向代理的
        SPA fallback 也会返回带 DSH 标记的 HTML，但 /api/* 返回 HTML，导致
        session.create 报“不是合法 JSON”。这里用一次轻量 RPC 排除这类假候选。
        """
        try:
            await self._modern_rpc(
                base_url, "session/list", {"_request": {}}, timeout=6, allow_unauth=True
            )
            self._protocol = PROTOCOL_MODERN
            self._protocol_base = base_url
            return True
        except DshError:
            pass
        try:
            await self._legacy_rpc(base_url, "session.list", {}, timeout=6)
            self._protocol = PROTOCOL_LEGACY
            self._protocol_base = base_url
            return True
        except DshError:
            return False

    async def _port_open(self, port: int) -> bool:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, port),
                timeout=self.connect_timeout,
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return True
        except Exception:
            return False

    def _candidate_ports(self) -> list[int]:
        candidates: list[int] = []
        if self.port_override > 0:
            candidates.append(self.port_override)
        env_url = os.environ.get("DSH_WEB_URL", "").strip()
        if env_url:
            try:
                env_port = urlsplit(env_url).port
            except ValueError:
                env_port = None
            if env_port:
                candidates.append(int(env_port))
        candidates.append(DSH_DEFAULT_PORT)
        for lo, hi in self.port_ranges:
            candidates.extend(range(lo, hi + 1))
        seen: set[int] = set()
        ordered: list[int] = []
        for port in candidates:
            if 1 <= port <= 65535 and port not in seen:
                seen.add(port)
                ordered.append(port)
        return ordered

    async def _scan(
        self, progress: Callable[[str], Awaitable[None]] | None = None
    ) -> str | None:
        """扫描候选地址，返回首个指纹命中的基地址（新旧版都接受）。"""
        explicit = self._explicit_base()
        if explicit:
            kind = await self._probe(explicit)
            if kind:
                self._probe_kinds[explicit] = kind
                return explicit

        ordered: list[tuple[int | None, str]] = []
        if not explicit:
            env_url = os.environ.get("DSH_WEB_URL", "").strip()
            if env_url:
                env_parts = urlsplit(env_url)
                if env_parts.scheme and env_parts.netloc:
                    ordered.append((None, f"{env_parts.scheme}://{env_parts.netloc}"))
        for port in self._candidate_ports():
            ordered.append((port, f"http://{self.host}:{port}"))

        if progress:
            await progress(
                f"🔍 开始扫描 {self.host}（共 {len(ordered)} 个候选端口，优先显式地址/默认端口）…"
            )

        fallback_page: str | None = None
        fallback_lock = asyncio.Lock()

        async def try_candidate(port: int | None, base: str) -> str | None:
            nonlocal fallback_page
            if port is not None and not await self._port_open(port):
                return None
            kind = await self._probe(base)
            if not kind:
                return None
            self._probe_kinds[base] = kind
            if kind == "modern":
                # 401 认证提示是 DSH 0.2.x API 的强特征，无需认证即可确认
                return base
            # 200 页面可能只是前端/代理的 SPA fallback，需校验 /api 真的可调用
            if await self._api_works(base):
                return base
            async with fallback_lock:
                if fallback_page is None:
                    fallback_page = base
                    self.logger.warning(
                        f"[DSH Bridge] {base} 页面像 DSH，但 /api 不是 JSON-RPC（可能是前端/代理页面），"
                        "继续扫描其他端口。"
                    )
            return None

        priority = ordered[:8]
        if priority:
            results = await asyncio.gather(
                *(try_candidate(port, base) for port, base in priority)
            )
            for base in results:
                if base is not None:
                    return base

        rest = ordered[8:]
        total = len(rest)
        sem = asyncio.Semaphore(self.scan_concurrency)
        scanned = 0
        batch_size = max(128, self.scan_concurrency * 4)

        async def try_one(port: int | None, base: str) -> str | None:
            async with sem:
                return await try_candidate(port, base)

        for index, start in enumerate(range(0, total, batch_size)):
            batch = rest[start : start + batch_size]
            results = await asyncio.gather(*(try_one(port, base) for port, base in batch))
            for base in results:
                if base is not None:
                    return base
            scanned += len(batch)
            if progress and index % 4 == 0:
                await progress(f"🔍 扫描中… 已检查 {scanned}/{total} 个端口")

        # 没有找到可调用 API 的地址时，仍返回页面候选（dsh_ping 会显示已连接，
        # dsh_ask 的报错会带上 HTTP 状态/Content-Type/响应片段，便于定位）。
        return fallback_page

    # -------------------------------------------------------------- 会话操作

    async def ping(self) -> dict:
        """连通性 + 协议 + 会话概览。发现成功即视为已连接（API 失败只记警告）。"""
        base = await self.discover()
        protocol = await self._ensure_protocol(base)
        info: dict[str, Any] = {
            "base": base,
            "protocol": protocol,
            "auth": (self._auth or {}).get("mode") or ("none" if protocol == PROTOCOL_LEGACY else None),
        }
        if protocol == PROTOCOL_LEGACY:
            try:
                value = await self._legacy_rpc(base, "session.list", {}, timeout=15)
                items = value.get("items") if isinstance(value, dict) else None
                items = items or []
                info["sessions"] = len(items)
                info["running"] = len([s for s in items if s.get("running")])
            except DshError as exc:
                info["sessions_error"] = str(exc)
            try:
                desc = await self._legacy_rpc(base, "host.describe", {}, timeout=10)
                if isinstance(desc, dict):
                    info["host"] = desc
                    version = desc.get("version")
                    if version:
                        info["version"] = str(version)
            except DshError:
                pass
        else:
            try:
                value = await self._modern_rpc(
                    base, "session/list", {"_request": {}}, timeout=15
                )
                items = value.get("items") if isinstance(value, dict) else None
                items = items or []
                info["sessions"] = len(items)
                info["running"] = len([s for s in items if s.get("running")])
            except DshError as exc:
                info["sessions_error"] = str(exc)
            version = await self._release_version_of(base)
            if version:
                info["version"] = version
        return info

    async def run_prompt(
        self,
        prompt: str,
        cwd: str | None = None,
        session_id: str | None = None,
        timeout: float = 900.0,
        poll_interval: float = 2.0,
        max_result_chars: int = 6000,
        progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict:
        """投递 prompt 并等待 DSH 智能体完成，返回最终回复。"""
        base = await self.discover()
        protocol = await self._ensure_protocol(base)
        if protocol == PROTOCOL_LEGACY:
            return await self._run_prompt_legacy(
                base,
                prompt,
                cwd=cwd,
                session_id=session_id,
                timeout=timeout,
                poll_interval=poll_interval,
                max_result_chars=max_result_chars,
                progress=progress,
            )
        return await self._run_prompt_modern(
            base,
            prompt,
            cwd=cwd,
            session_id=session_id,
            timeout=timeout,
            poll_interval=poll_interval,
            max_result_chars=max_result_chars,
            progress=progress,
        )

    async def _run_prompt_modern(
        self,
        base: str,
        prompt: str,
        cwd: str | None = None,
        session_id: str | None = None,
        timeout: float = 900.0,
        poll_interval: float = 2.0,
        max_result_chars: int = 6000,
        progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict:
        start = time.monotonic()
        own_session = session_id is None
        if own_session:
            create_args: dict[str, Any] = {}
            if cwd:
                create_args["cwd"] = cwd
            created = await self._modern_rpc(
                base, "session/create", {"request": create_args}, timeout=60
            )
            session_id = (created or {}).get("sessionId") if isinstance(created, dict) else None
            if not session_id:
                raise DshError("DSH 未返回会话 ID")
        if progress:
            await progress(f"📡 已接入 DSH 会话 {session_id}，任务已投递，等待 DSH 智能体执行…")

        baseline = await self._session_cursor(base, session_id)

        request_id = str(uuid.uuid4())
        try:
            await self._modern_rpc(
                base,
                "session/prompt",
                {
                    "request": {
                        "requestId": request_id,
                        "sessionId": session_id,
                        "mode": "queue",
                        "content": [{"type": "text", "text": prompt}],
                        "clientTimeZone": self._tz_name(),
                    }
                },
                timeout=60,
            )
        except DshError:
            if own_session:
                await self._safe_cancel(session_id)
            raise

        deadline = start + timeout
        records: list[dict] = []
        reason: str | None = None
        truncated = False
        waits = 0
        pending_input = False
        retry_sleep = max(IDLE_RETRY_MIN_SECONDS, min(IDLE_RETRY_MAX_SECONDS, poll_interval / 2))
        stalled_since = time.monotonic()
        stall_limit = 120.0
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0.5:
                    break
                segment_ms = int(max(1.0, min(WAIT_SEGMENT_SECONDS, remaining)) * 1000)
                waits += 1
                try:
                    value = await self._modern_rpc(
                        base,
                        "session/wait",
                        {
                            "request": {
                                "sessionId": session_id,
                                "afterSeq": baseline,
                                "timeoutMs": segment_ms,
                                "maxMessages": 200,
                            }
                        },
                        timeout=segment_ms / 1000 + 45,
                    )
                except DshError:
                    if own_session:
                        await self._safe_cancel(session_id)
                    raise
                if not isinstance(value, dict):
                    raise DshError("DSH session/wait 返回格式异常")
                reason = value.get("reason")
                state = value.get("state") or {}
                output = value.get("output") or {}
                got = output.get("records") or []
                if got:
                    records = got
                    truncated = bool(output.get("truncated"))
                cursor = state.get("cursor", -1)
                if records or cursor > baseline or state.get("running"):
                    stalled_since = time.monotonic()
                if reason == "idle":
                    if records:
                        break
                    if self._queue_has(state, request_id) or cursor <= baseline:
                        if time.monotonic() - stalled_since > stall_limit:
                            await self._safe_cancel(session_id)
                            raise DshError(
                                f"DSH 会话 {session_id} 未开始执行任务（已等待 {int(stall_limit)} 秒）。"
                                "请确认 DSH 智能体空闲、该会话未被其他任务占用，或改用 session_mode=new。"
                            )
                        await asyncio.sleep(retry_sleep)
                        continue
                    break
                if reason == "input-required":
                    pending_input = True
                    break
        except asyncio.CancelledError:
            await self._safe_cancel(session_id)
            raise

        duration_ms = int((time.monotonic() - start) * 1000)
        if pending_input:
            text = self._collect_assistant_text(records)
            note = "（DSH 正在等待用户确认或回答，请到 DSH 窗口处理后重试。）"
            text = f"{text}\n\n{note}" if text else note
            return {
                "text": text,
                "sessionId": session_id,
                "duration_ms": duration_ms,
                "waits": waits,
                "truncated": truncated,
                "pending_input": True,
                "reason": reason,
            }
        if reason != "idle":
            await self._safe_cancel(session_id)
            raise DshError(
                f"DSH 智能体在 {int(timeout)} 秒内未完成任务（会话 {session_id} 已发送取消）。"
                "可尝试在插件配置中调大 request_timeout_seconds。"
            )

        text = self._collect_assistant_text(records)
        if max_result_chars > 0 and len(text) > max_result_chars:
            text = text[:max_result_chars] + "\n…(结果过长已截断)"
            truncated = True
        if not text:
            text = "（DSH 智能体未返回可见文本）"
        return {
            "text": text,
            "sessionId": session_id,
            "duration_ms": duration_ms,
            "waits": waits,
            "truncated": truncated,
            "pending_input": False,
            "reason": reason,
        }

    async def _run_prompt_legacy(
        self,
        base: str,
        prompt: str,
        cwd: str | None = None,
        session_id: str | None = None,
        timeout: float = 900.0,
        poll_interval: float = 2.0,
        max_result_chars: int = 6000,
        progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict:
        """旧版 DSH：点号方法 + session.history 轮询到 turn/end。"""
        start = time.monotonic()
        own_session = session_id is None
        if own_session:
            create_payload: dict[str, Any] = {}
            if cwd:
                create_payload["cwd"] = cwd
            created = await self._legacy_rpc(base, "session.create", create_payload, timeout=60)
            session_id = (created or {}).get("sessionId") if isinstance(created, dict) else None
            if not session_id:
                raise DshError("DSH 未返回会话 ID")
        if progress:
            await progress(f"📡 已接入 DSH 会话 {session_id}，任务已投递，等待 DSH 智能体执行…")

        try:
            await self._legacy_rpc(
                base,
                "session.prompt",
                {
                    "sessionId": session_id,
                    "mode": "queue",
                    "content": [{"type": "text", "text": prompt}],
                    "clientTimeZone": self._tz_name(),
                },
                timeout=60,
            )
        except DshError:
            if own_session:
                await self._safe_cancel(session_id)
            raise

        deadline = start + timeout
        texts: list[str] = []
        done = False
        polls = 0
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(max(1.0, poll_interval))
                polls += 1
                history = await self._legacy_rpc(
                    base,
                    "session.history",
                    {"sessionId": session_id, "maxMessages": 300},
                    timeout=30,
                )
                events = history.get("events") if isinstance(history, dict) else None
                texts, done = self._extract_legacy_history(events or [], texts)
                if done and texts:
                    break
        except asyncio.CancelledError:
            await self._safe_cancel(session_id)
            raise
        except DshError:
            if own_session:
                await self._safe_cancel(session_id)
            raise

        duration_ms = int((time.monotonic() - start) * 1000)
        if not done:
            await self._safe_cancel(session_id)
            raise DshError(
                f"DSH 智能体在 {int(timeout)} 秒内未完成任务（会话 {session_id} 已发送取消）。"
                "可尝试在插件配置中调大 request_timeout_seconds。"
            )
        text = "\n\n".join(t for t in texts if t).strip()
        truncated = False
        if max_result_chars > 0 and len(text) > max_result_chars:
            text = text[:max_result_chars] + "\n…(结果过长已截断)"
            truncated = True
        if not text:
            text = "（DSH 智能体未返回可见文本）"
        return {
            "text": text,
            "sessionId": session_id,
            "duration_ms": duration_ms,
            "waits": polls,
            "truncated": truncated,
            "pending_input": False,
            "reason": "turn-end",
        }

    @staticmethod
    def _extract_legacy_history(events: list, texts: list[str]) -> tuple[list[str], bool]:
        """从旧版 session.history 事件列表提取 assistant 文本与 turn/end 标记。"""
        done = False
        for entry in events:
            if not isinstance(entry, dict):
                continue
            event = entry.get("event") or entry
            if not isinstance(event, dict):
                continue
            event_type = event.get("type") or ""
            if event_type == "assistant/message":
                text = DshClient._extract_text(event.get("data") or {})
                if text and (not texts or texts[-1] != text):
                    texts.append(text)
            elif event_type == "turn/end":
                done = True
        return texts, done

    async def _session_cursor(self, base: str, session_id: str) -> int:
        value = await self._modern_rpc(
            base, "session/state", {"request": {"sessionId": session_id}}, timeout=30
        )
        if isinstance(value, dict) and isinstance(value.get("cursor"), int):
            return int(value["cursor"])
        return -1

    @staticmethod
    def _queue_has(state: dict, request_id: str) -> bool:
        for item in state.get("queue") or []:
            if isinstance(item, dict) and item.get("rpcId") == request_id:
                return True
        return False

    @staticmethod
    def _extract_text(data: dict) -> str:
        message = data.get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        parts: list[str] = []
        for block in content or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts).strip()

    @classmethod
    def _collect_assistant_text(cls, records: list[dict]) -> str:
        texts: list[str] = []
        for entry in reversed(records or []):
            if not isinstance(entry, dict):
                continue
            event = entry.get("event") or entry
            if not isinstance(event, dict) or event.get("type") != "assistant/message":
                continue
            text = cls._extract_text(event.get("data") or {})
            if text and (not texts or texts[-1] != text):
                texts.append(text)
        return "\n\n".join(texts).strip()

    @staticmethod
    def _tz_name() -> str:
        try:
            import datetime

            tzinfo = datetime.datetime.now().astimezone().tzinfo
            key = getattr(tzinfo, "key", None)
            if isinstance(key, str) and key:
                return key
        except Exception:
            pass
        return "UTC"

    async def _safe_cancel(self, session_id: str) -> None:
        try:
            base = await self.discover()
            protocol = await self._ensure_protocol(base)
            if protocol == PROTOCOL_LEGACY:
                await self._legacy_rpc(
                    base, "session.cancel", {"sessionId": session_id}, timeout=10
                )
            else:
                await self._modern_rpc(
                    base,
                    "session/cancel",
                    {"request": {"sessionId": session_id}},
                    timeout=10,
                )
        except Exception as exc:  # noqa: BLE001 - 取消失败不影响主流程
            self.logger.warning(f"[DSH Bridge] 取消会话失败: {exc}")

    # -------------------------------------------------------------- 后台预热

    def start_warmup(self) -> None:
        """插件加载后后台预热端口发现，让首次调用无需等待全量扫描。"""
        if self._warm_task and not self._warm_task.done():
            return
        self._warm_task = asyncio.create_task(self._warm())

    async def _warm(self) -> None:
        try:
            await asyncio.sleep(1.0)
            base = await self.discover()
            await self._ensure_protocol(base)
        except DshError as exc:
            self.logger.warning(f"[DSH Bridge] 后台预热未发现 DSH：{exc}")
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"[DSH Bridge] 后台预热异常: {exc}")
