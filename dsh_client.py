"""DSH Bridge - DeepSeek Harness 本地 API 客户端（适配 DSH 0.2.2-rc.1+）。

本模块封装 DSH（DeepSeek Harness）宿主进程暴露在环回地址/局域网上的 HTTP API，
让 AstrBot 插件无需在 DSH 侧安装任何组件，即可把任务投递给 DSH 智能体并取回结果。

协议说明（依据 @deepseek-ai/dsh 0.2.2-rc.1 源码与本机实测）:

    1) 发现
       GET  /                       未认证时返回 401 文本
                                    "dsh web authentication required; ..."；
                                    认证后返回 SPA index.html，含 __DSH_BOOT__ 标记与
                                    release.version。DSH Desktop 以 --port 0 启动，
                                    端口通常落在系统临时端口段，因此默认扫描 32768-65535。

    2) 认证（DSH 0.2.x 起 /api 必须认证）
       方式 A（本机，默认）：读取 DSH 数据目录下的 .credentials.yaml，
         取 records["client-connection/browser-session"].payload.secret（HMAC 签名密钥，
         32 字节 base64url），本地签发 authority 绑定的浏览器会话 Cookie：
             Cookie 名  dsh-auth-<base64url(sha256(authority))>
             Cookie 值  v1.<base64url(JSON payload)>.<base64url(HMAC-SHA256(secret, body))>
             payload    {"version":1,"authority":"127.0.0.1:<port>",
                         "issuedAt":<ms>,"expiresAt":<ms>}
       方式 B（跨设备/无凭据文件）：把 `dsh web` 启动输出中带 ?token= 的完整 URL
         填入 dsh_url（或把 token 填入 dsh_token），用
             GET /connect?token=<token>&interactions=none
         换取 API Cookie（v2）。DSH 端需 --trusted-host 放行该 authority。

    3) RPC
       POST /api/<method>
       请求  {"type":"client-request","rpcId":"<uuid>","method":"<name>","payload":{"args":{...}}}
       响应  {"type":"server-response","rpcId":"<uuid>","result":{"ok":true,"value":{...}}}
             失败时 {"ok":false,"error":{"code","message","details"}}

       方法名使用斜杠命名（0.2.x），参数以命名参数对象传递：
           session/create  {"request": {"cwd"?, "workspaceId"?, "sessionId"?, "agentPreset"?}}
           session/list    {"_request": {}}
           session/state   {"request": {"sessionId"}}
           session/events  {"request": {"address": {"kind":"session","sessionId"},
                                        "throughSeq"?, "beforeSeq"?, "maxMessages"?}}
           session/prompt  {"request": {"requestId", "sessionId", "mode", "content": [...],
                                        "clientTimeZone"}}
           session/wait    {"request": {"sessionId", "afterSeq"?, "timeoutMs"?, "maxMessages"?}}
           session/cancel  {"request": {"sessionId"}}

    4) 完成判定
       session/prompt 只表示消息已入队；用 session/wait 等待会话空闲/需要用户输入/超时。
       session/wait 的 output.records 为倒序（最新在前）的 assistant/message 事件，
       最终文本位于 event.data.message.content[] 中 type=="text" 的文本块。

端口发现策略:
    1. 配置了 dsh_url 时直接使用（可含 ?token=）；
    2. 配置了 dsh_port > 0 时优先尝试该端口；
    3. 尝试 DSH 历史默认端口 59934 与 DSH_WEB_URL 环境变量提示的地址；
    4. 按配置的端口区间（默认 32768-65535）并发扫描，用 TCP 连通性预筛 + HTTP 指纹确认；
    5. 对指纹命中的候选依次做认证校验（session/list），优先返回可用的那个。
    结果缓存在内存中（cache_ttl_seconds），过期或失效后自动重扫。

跨设备适配:
    - 仅依赖 Python 标准库 + aiohttp（AstrBot 核心自带），Windows / Linux / macOS / Docker 通用；
    - AstrBot 与 DSH 不在同一台机器时，用 dsh_url/dsh_token 方式认证，
      同时 DSH 启动参数需加 --trusted-host 允许该来源地址；
    - 扫描并发与超时均可配置，弱机/容器环境可调低。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, quote, urlsplit

import aiohttp

# DSH 历史默认监听端口（0.1.x 桌面壳；0.2.x Desktop 改为 --port 0 随机端口）
DSH_DEFAULT_PORT = 59934
# 默认扫描区间：Windows 临时端口段 49152-65535 与 Linux/macOS 临时端口段 32768-60999 的并集
DEFAULT_PORT_RANGES = "32768-65535"
# 指纹标记：未认证时 401 文本、认证后 HTML 标题与 boot 标记任一命中即视为 DSH
FINGERPRINT_TITLE = "DeepSeek Harness"
FINGERPRINT_MARKER = "__DSH_BOOT__"
AUTH_REQUIRED_MARKER = "dsh web authentication required"
# 凭据文件中的浏览器会话签名密钥记录
CREDENTIAL_RECORD = "client-connection/browser-session"
# 自签 Cookie 的有效期：1 天（DSH 默认 cookieMaxAgeDays=30，且最小允许 1 天，取 1 天最稳）
COOKIE_MAX_AGE_MS = 24 * 60 * 60 * 1000
# session/wait 单次请求的等待窗口（秒）；到期后继续等待，直到整体超时
WAIT_SEGMENT_SECONDS = 20.0
# 轮询兜底：判空后重试的最小/最大间隔（秒）
IDLE_RETRY_MIN_SECONDS = 0.2
IDLE_RETRY_MAX_SECONDS = 1.0


def _b64url(data: bytes) -> str:
    """标准库实现 base64url（无 padding），与 Node Buffer.toString("base64url") 对齐。"""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    """解码 base64url（容忍缺失 padding）。"""
    padding = "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _split_host_port(base_url: str) -> tuple[str, int | None, str]:
    """拆出 (hostname, port, netloc)；hostname 为 WHATWG 形式（IPv6 不带方括号）。"""
    parts = urlsplit(base_url)
    host = (parts.hostname or "").strip().lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = parts.netloc.lower()
    return host, port, netloc


def _is_loopback_host(host: str) -> bool:
    """判断主机名是否为回环地址（与 DSH 的 isLoopbackHostname 保持一致）。"""
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
    """DSH API 客户端：发现、认证、RPC 调用、投递任务并等待结果。"""

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
        raw = str(self._cfg("port_ranges", DEFAULT_PORT_RANGES) or "")
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
            return max(8, min(1024, int(self._cfg("scan_concurrency", 512) or 512)))
        except (TypeError, ValueError):
            return 512

    @property
    def connect_timeout(self) -> float:
        try:
            return max(0.05, min(2.0, float(self._cfg("port_probe_timeout_seconds", 0.3) or 0.3)))
        except (TypeError, ValueError):
            return 0.3

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
            # dsh_url 可能只在扫描时才被解析；这里主动解析一次以拿到 token
            self._explicit_base()
        return self._url_token

    # -------------------------------------------------------------- HTTP 层

    async def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30, connect=5),
                headers={"User-Agent": "astrbot-plugin-dsh-bridge/1.1"},
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
        """从 .credentials.yaml 文本中提取浏览器会话签名密钥（base64url 文本）。

        优先用 PyYAML（AstrBot 核心自带）；不可用时用缩进感知的简易解析兜底。
        """
        try:
            import yaml  # type: ignore

            data = yaml.safe_load(text)
            record = ((data or {}).get("records") or {}).get(CREDENTIAL_RECORD)
            secret = ((record or {}).get("payload") or {}).get("secret")
            if isinstance(secret, str) and secret.strip():
                return secret.strip()
        except Exception:  # noqa: BLE001 - YAML 不可用/格式异常时走兜底解析
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
        """确保已获得 base_url 对应的认证 Cookie，返回 Cookie 请求头。

        优先使用本机凭据文件（DSH 重启后仍有效）；不可用时回退到启动令牌。
        收到 401 强制刷新时，优先换用另一种认证方式，避免一种凭据失效后无法自愈。
        """
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
        """认证后读取 DSH 版本（来自 index.html 的 __DSH_BOOT__.release.version）。"""
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
        """调用一个 DSH Remote 方法，返回 result.value（ok 时）。失败抛 DshError。

        payload 为命名参数对象（等价于信封中的 payload.args），例如
        session/create 传 {"request": {...}}，session/list 传 {"_request": {}}。
        """
        base = await self.discover()
        return await self._rpc(base, method, payload or {}, timeout=timeout)

    async def _rpc(self, base_url: str, method: str, args: dict, timeout: float = 30.0) -> Any:
        body = {
            "type": "client-request",
            "rpcId": str(uuid.uuid4()),
            "method": method,
            "payload": {"args": args or {}},
        }
        session = await self._session()
        last_error: DshError | None = None
        for attempt in range(2):
            cookie = await self._ensure_auth(base_url, force=attempt > 0)
            try:
                async with session.post(
                    f"{base_url}/api/{method}",
                    json=body,
                    headers={"Cookie": cookie, "Accept": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status == 401:
                        last_error = DshError(
                            "DSH 拒绝了认证（HTTP 401）。"
                            "请确认 dsh_home 指向正在运行的 DSH 实例的数据目录，"
                            "或重新获取 `dsh web` 输出中的启动令牌。"
                        )
                        self._auth = None
                        continue
                    if resp.status == 403:
                        raise DshError(self._trust_hint())
                    if resp.status == 404:
                        raise DshError(
                            f"DSH 不存在方法 {method}（HTTP 404）。本插件适配 DSH 0.2.2-rc.1+，"
                            "请升级 DSH。"
                        )
                    if resp.status != 200:
                        detail = (await resp.text(errors="ignore"))[:200].strip()
                        raise DshError(f"DSH API 返回 HTTP {resp.status}（{method}）：{detail}")
                    data = await resp.json(content_type=None)
            except asyncio.TimeoutError as exc:
                raise DshError(f"DSH API 调用超时（{method}）") from exc
            except aiohttp.ClientError as exc:
                raise DshError(f"无法连接 DSH（{method}）：{exc}") from exc
            except json.JSONDecodeError as exc:
                raise DshError(f"DSH API 响应不是合法 JSON（{method}）") from exc

            result = data.get("result") if isinstance(data, dict) else None
            if not isinstance(result, dict):
                raise DshError(f"DSH API 响应格式异常（{method}）")
            if not result.get("ok"):
                error = result.get("error") or {}
                code = str(error.get("code") or "unknown")
                message = str(error.get("message") or error.get("msg") or code)
                raise DshError(f"DSH 拒绝了 {method}（{code}）：{message}")
            value = result.get("value")
            if value is None:
                return {}
            return value

        raise last_error or DshError(f"DSH 认证失败（{method}）")

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
            # 快速路径：缓存仍新鲜但先做一次廉价活性 + 认证校验
            if self._base_url and await self._probe_url(self._base_url) and await self._verify(self._base_url):
                self._discovered_at = time.monotonic()
                return self._base_url
            self._base_url = None
            self._auth = None
            found = await self._scan(progress=progress)
            if found is None:
                raise DshError(self._not_found_hint())
            self._base_url = found
            self._discovered_at = time.monotonic()
            self.logger.info(f"[DSH Bridge] 已发现 DSH: {found}（认证方式：{(self._auth or {}).get('mode')}）")
            return found

    def _not_found_hint(self) -> str:
        hint = (
            "未能在本机发现 DeepSeek Harness。请确认："
            "1) DSH Desktop / dsh web 正在运行；"
            "2) 若 DSH 端口不确定，可把 DSH 窗口地址栏或 `dsh web` 输出里的完整 URL"
            "（形如 http://127.0.0.1:<端口>/?token=...）填入插件配置 dsh_url，这样无需扫描；"
            "3) AstrBot 与 DSH 不在同一台设备时，把 dsh_host 配为 DSH 所在设备的局域网 IP，"
            "并配置 dsh_token / dsh_url 与 DSH 的 --trusted-host。"
        )
        if self.host != "127.0.0.1":
            hint = f"无法通过 {self.host} 连接 DSH。" + hint
        return hint

    async def _probe_url(self, base_url: str) -> bool:
        """未认证指纹探测：401 认证提示文本，或 200 页面中的 DSH 标记。"""
        try:
            session = await self._session()
            async with session.get(
                base_url + "/",
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=1.5),
            ) as resp:
                if resp.status == 401:
                    text = (await resp.text(errors="ignore"))[:2000]
                    return AUTH_REQUIRED_MARKER in text
                if resp.status != 200:
                    return False
                head = (await resp.text(errors="ignore"))[:20000]
                return FINGERPRINT_TITLE in head or FINGERPRINT_MARKER in head
        except Exception:
            return False

    async def _verify(self, base_url: str) -> bool:
        """认证校验：能成功调用 session/list 才认为该地址可用。"""
        if not self._auth_available(base_url):
            return False
        try:
            await self._rpc(base_url, "session/list", {"_request": {}}, timeout=10)
            return True
        except DshError as exc:
            self.logger.debug(f"[DSH Bridge] 候选地址认证校验失败 {base_url}: {exc}")
            return False
        except Exception:  # noqa: BLE001
            return False

    def _auth_available(self, base_url: str) -> bool:
        """是否具备可尝试的认证材料（本机凭据文件或启动令牌）。"""
        _host, _port, _netloc = _split_host_port(base_url)
        local = _is_loopback_host(_host or self.host)
        if local and self._load_secret() is not None:
            return True
        return self.token is not None

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
        """扫描候选地址并认证校验，返回首个可用基地址。"""
        explicit = self._explicit_base()
        # 显式地址是用户指定的权威入口：能连通就是它，认证问题留给调用方明确报错，
        # 不再花时间扫描其他端口。
        if explicit:
            if await self._probe_url(explicit):
                if self._auth_available(explicit):
                    await self._verify(explicit)
                return explicit

        ordered: list[tuple[int | None, str]] = []
        # DSH_WEB_URL 环境变量作为提示候选（仅当显式 URL 未配置时）
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

        fallback: str | None = None
        fallback_lock = asyncio.Lock()

        async def consider(base: str) -> str | None:
            nonlocal fallback
            if not await self._probe_url(base):
                return None
            # 没有任何认证材料时无法做认证校验，指纹命中即可返回，
            # 让后续调用给出明确的「如何配置认证」提示。
            if not self._auth_available(base):
                self.logger.warning(
                    f"[DSH Bridge] 在 {base} 发现疑似 DSH，但未找到认证材料（凭据文件/启动令牌），"
                    "调用时可能返回认证失败提示。"
                )
                return base
            if await self._verify(base):
                return base
            async with fallback_lock:
                if fallback is None:
                    fallback = base
                    self.logger.warning(
                        f"[DSH Bridge] {base} 疑似 DSH，但认证校验未通过，继续扫描其他端口。"
                    )
            return None

        async def try_candidate(port: int | None, base: str) -> str | None:
            # 端口候选先做 TCP 连通预筛，避免对未开放端口发出 HTTP 请求白等超时
            if port is not None and not await self._port_open(port):
                return None
            return await consider(base)

        # 1) 显式地址与高优先级端口：并发直探
        priority = ordered[:8]
        if priority:
            results = await asyncio.gather(
                *(try_candidate(port, base) for port, base in priority)
            )
            for base in results:
                if base is not None:
                    return base

        # 2) 其余端口：TCP 预筛 + HTTP 指纹 + 认证校验（分批并发）
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

        if fallback is not None:
            return fallback
        return None

    # -------------------------------------------------------------- 会话操作

    async def ping(self) -> dict:
        """连通性 + 会话概览 + DSH 版本。认证失败会直接抛出 DshError。"""
        base = await self.discover()
        value = await self.call("session/list", {"_request": {}}, timeout=15)
        items = value.get("items") if isinstance(value, dict) else None
        items = items or []
        info: dict[str, Any] = {
            "base": base,
            "auth": (self._auth or {}).get("mode"),
            "sessions": len(items),
            "running": len([s for s in items if s.get("running")]),
        }
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
        """向 DSH 投递一条 prompt，等待 agent 完成后返回最终回复。

        返回 {"text", "sessionId", "duration_ms", "waits", "truncated",
              "pending_input", "reason"}；超时/失败抛 DshError。
        """
        start = time.monotonic()
        await self.discover()

        # 1. 确定会话
        own_session = session_id is None
        if own_session:
            create_args: dict[str, Any] = {}
            if cwd:
                create_args["cwd"] = cwd
            created = await self.call("session/create", {"request": create_args}, timeout=60)
            session_id = (created or {}).get("sessionId") if isinstance(created, dict) else None
            if not session_id:
                raise DshError("DSH 未返回会话 ID")
        if progress:
            await progress(f"📡 已接入 DSH 会话 {session_id}，任务已投递，等待 DSH 智能体执行…")

        # 2. 记录投递前的会话游标，作为等待输出的下界
        baseline = await self._session_cursor(session_id)

        # 3. 投递 prompt
        request_id = str(uuid.uuid4())
        try:
            await self.call(
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

        # 4. 等待完成：session/wait 阻塞直到空闲 / 需要用户输入 / 分段超时
        deadline = start + timeout
        records: list[dict] = []
        reason: str | None = None
        truncated = False
        waits = 0
        pending_input = False
        retry_sleep = max(IDLE_RETRY_MIN_SECONDS, min(IDLE_RETRY_MAX_SECONDS, poll_interval / 2))
        # 任务迟迟不开始（游标不前进且不运行）时，超过该时限直接报错，避免空转到整体超时
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
                    value = await self.call(
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
                    # 防抖：任务可能尚未真正开始（会话被误判为空闲）
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
                # reason == "timeout" 或其他：继续下一段等待，直到整体超时
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

    async def _session_cursor(self, session_id: str) -> int:
        """读取会话当前事件游标。"""
        value = await self.call("session/state", {"request": {"sessionId": session_id}}, timeout=30)
        if isinstance(value, dict) and isinstance(value.get("cursor"), int):
            return int(value["cursor"])
        return -1

    @staticmethod
    def _queue_has(state: dict, request_id: str) -> bool:
        """判断 prompt 是否仍停留在会话队列中（尚未被处理）。"""
        for item in state.get("queue") or []:
            if isinstance(item, dict) and item.get("rpcId") == request_id:
                return True
        return False

    @staticmethod
    def _extract_text(data: dict) -> str:
        """从 assistant/message 事件的 data 中提取文本块。"""
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
        """从 session/wait 返回的记录（倒序，最新在前）中按时间顺序拼接文本。"""
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
        """返回 IANA 时区名（DSH 只接受 UTC 或 IANA Area/Location 名）。

        优先取系统本地时区的 tzinfo.key（ZoneInfo 提供 IANA 名），
        不可用时回退到 UTC，避免把「中国标准时间」这类本地化名称发给 DSH。
        """
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
            await self.call("session/cancel", {"request": {"sessionId": session_id}}, timeout=10)
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
            await self.discover()
        except DshError as exc:
            self.logger.info(f"[DSH Bridge] 后台预热未发现 DSH（首次使用时将重新扫描）：{exc}")
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"[DSH Bridge] 后台预热异常: {exc}")
