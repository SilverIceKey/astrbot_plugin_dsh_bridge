"""DSH Bridge - DeepSeek Harness 本地 API 客户端。

本模块封装了 DSH（DeepSeek Harness）宿主进程暴露在环回地址上的 HTTP/JSON API，
让 AstrBot 插件无需安装任何 DSH 侧组件即可把任务投递给 DSH 智能体。

协议说明（依据 @deepseek-ai/dsh 源码与实测确认，无需鉴权）:
    GET  /                        -> SPA index.html；标题含 "DeepSeek Harness"，
                                     正文含 "__DSH_BOOT__" 标记（用于端口指纹识别）。
    POST /api/<method>            -> JSON-RPC 风格信封：
        请求  {"type":"client-request","rpcId":"<uuid>","method":"<name>","payload":{...}}
        响应  {"type":"server-response","rpcId":"<同uuid>","result":{"ok":true,"value":{...}}}
              失败时 result.ok == false。

    常用方法:
        session.create  {"cwd"?: str, "workspaceId"?: str}          -> {"sessionId": "session-..."}
        session.prompt  {"sessionId", "mode":"queue"|"steer",
                         "content":[{"type":"text","text":str}, ...]} -> {"accepted": true}
        session.history {"sessionId", "maxMessages"?: int}          -> {"events":[{event:{type,data}}], "hasMore"}
        session.list    {}                                          -> {"items":[{sessionId,running,blank,cwd,...}]}
        session.cancel  {"sessionId"}                               -> {"accepted": true}
        host.describe   {}                                          -> 宿主信息

    完成判定: 事件流中出现 "turn/end" 事件；最终文本位于 "assistant/message" 事件的
    data.message.content[].text 中。

端口发现策略:
    1. 配置了 dsh_port > 0 时优先尝试该端口；
    2. 尝试 DSH 默认端口 59934；
    3. 按配置的端口区间（默认先扫 59900-60100，再扫系统临时端口段）并发扫描 127.0.0.1，
       用 TCP 连通性预筛 + HTTP 指纹确认。
    结果缓存在内存中（cache_ttl_seconds），过期或失效后自动重扫。

跨设备适配:
    - 仅依赖 Python 标准库 + aiohttp（AstrBot 核心自带），无平台特定调用；
    - AstrBot 与 DSH 不在同一台机器时，可将 dsh_host 配为局域网地址；
    - 扫描并发与超时均可配置，弱机/容器环境可调低。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Awaitable, Callable

import aiohttp

# DSH 默认监听端口（桌面壳 reservePort 首选；被占用时回退到系统随机端口）
DSH_DEFAULT_PORT = 59934
# 默认扫描区间：优先 DSH 常用区间，其次 Windows 临时端口段，其次 Linux/macOS 临时端口段
DEFAULT_PORT_RANGES = "59900-60100,49152-60000,32768-60999"
# 指纹标记：标题与 HTML 标记任一命中即视为 DSH
FINGERPRINT_TITLE = "DeepSeek Harness"
FINGERPRINT_MARKER = "__DSH_BOOT__"
# 单次轮询间隔
DEFAULT_POLL_INTERVAL = 2.0


class DshError(Exception):
    """DSH 桥接层错误（带面向用户的友好信息）。"""


class DshClient:
    """DSH API 客户端：发现、RPC 调用、投递任务并等待结果。"""

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
            return max(8, min(1024, int(self._cfg("scan_concurrency", 256) or 256)))
        except (TypeError, ValueError):
            return 256

    @property
    def connect_timeout(self) -> float:
        try:
            return max(0.05, min(2.0, float(self._cfg("port_probe_timeout_seconds", 0.4) or 0.4)))
        except (TypeError, ValueError):
            return 0.4

    # -------------------------------------------------------------- HTTP 层

    async def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30, connect=5),
                headers={"User-Agent": "astrbot-plugin-dsh-bridge/1.0"},
            )
        return self._http

    async def close(self) -> None:
        if self._warm_task and not self._warm_task.done():
            self._warm_task.cancel()
        if self._http and not self._http.closed:
            await self._http.close()
        self._http = None

    async def call(
        self,
        method: str,
        payload: dict | None = None,
        timeout: float = 30.0,
    ) -> dict:
        """调用一个 DSH RPC 方法，返回 result.value（ok 时）。失败抛 DshError。"""
        base = await self.discover()
        body = {
            "type": "client-request",
            "rpcId": str(uuid.uuid4()),
            "method": method,
            "payload": payload or {},
        }
        session = await self._session()
        try:
            async with session.post(
                f"{base}/api/{method}",
                json=body,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    raise DshError(f"DSH API 返回 HTTP {resp.status}（{method}）")
                data = await resp.json(content_type=None)
        except asyncio.TimeoutError as exc:
            raise DshError(f"DSH API 调用超时（{method}）") from exc
        except aiohttp.ClientError as exc:
            raise DshError(f"无法连接 DSH（{method}）：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise DshError(f"DSH API 响应不是合法 JSON（{method}）") from exc

        result = data.get("result") or {}
        if not result.get("ok"):
            code = result.get("code") or result.get("error") or "unknown"
            message = result.get("message") or result.get("msg") or str(code)
            raise DshError(f"DSH 拒绝了 {method}：{message}")
        value = result.get("value")
        return value if isinstance(value, dict) else ({"value": value} if value is not None else {})

    # -------------------------------------------------------------- 端口发现

    async def discover(self, force: bool = False, progress: Callable[[str], Awaitable[None]] | None = None) -> str:
        """返回形如 http://127.0.0.1:59934 的基地址。未找到抛 DshError。"""
        if not force and self._base_url and (time.monotonic() - self._discovered_at) < self.cache_ttl:
            return self._base_url
        async with self._discovery_lock:
            if not force and self._base_url and (time.monotonic() - self._discovered_at) < self.cache_ttl:
                return self._base_url
            # 快速路径：缓存仍新鲜但先做一次廉价活性探测
            if self._base_url and await self._probe_url(self._base_url):
                self._discovered_at = time.monotonic()
                return self._base_url
            self._base_url = None
            found = await self._scan(progress=progress)
            if found is None:
                raise DshError(self._not_found_hint())
            self._base_url = found
            self._discovered_at = time.monotonic()
            self.logger.info(f"[DSH Bridge] 已发现 DSH: {found}")
            return found

    def _not_found_hint(self) -> str:
        hint = (
            "未能在本机发现 DeepSeek Harness。请确认："
            "1) DSH Desktop / dsh web 正在运行；"
            "2) 若 DSH 端口不是默认的 59934，可在插件配置 dsh_port 中手动指定"
            "（DSH 窗口地址栏 http://127.0.0.1:<端口> 中的端口号）；"
            "3) AstrBot 与 DSH 不在同一台设备时，把 dsh_host 配为 DSH 所在设备的局域网 IP。"
        )
        if self.host != "127.0.0.1":
            hint = f"无法通过 {self.host} 连接 DSH。" + hint
        return hint

    async def _probe_url(self, base: str) -> bool:
        try:
            session = await self._session()
            async with session.get(base + "/", timeout=aiohttp.ClientTimeout(total=2.0)) as resp:
                if resp.status != 200:
                    return False
                head = (await resp.text(errors="ignore"))[:20000]
                return FINGERPRINT_TITLE in head or FINGERPRINT_MARKER in head
        except Exception:
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

    async def _scan(self, progress: Callable[[str], Awaitable[None]] | None = None) -> str | None:
        """按优先级扫描候选端口并做指纹确认。"""
        candidates: list[int] = []
        seen: set[int] = set()
        if self.port_override > 0:
            candidates.append(self.port_override)
        candidates.append(DSH_DEFAULT_PORT)
        for lo, hi in self.port_ranges:
            candidates.extend(range(lo, hi + 1))
        ordered = [p for p in candidates if not (p in seen or seen.add(p))]

        if progress:
            await progress(f"🔍 开始扫描 {self.host}（共 {len(ordered)} 个端口，默认端口优先）…")

        # 先快速直探高优先级端口（无需 TCP 预筛）
        for port in ordered[:8]:
            if await self._probe_url(f"http://{self.host}:{port}"):
                return f"http://{self.host}:{port}"

        sem = asyncio.Semaphore(self.scan_concurrency)
        scanned = 0
        total = len(ordered) - 8
        batch_size = max(64, self.scan_concurrency * 2)

        async def try_one(port: int) -> int | None:
            nonlocal scanned
            async with sem:
                if not await self._port_open(port):
                    return None
                if await self._probe_url(f"http://{self.host}:{port}"):
                    return port
                return None

        for start in range(8, len(ordered), batch_size):
            batch = ordered[start : start + batch_size]
            results = await asyncio.gather(*(try_one(p) for p in batch))
            for port in results:
                if port is not None:
                    return f"http://{self.host}:{port}"
            scanned += len(batch)
            if progress and scanned % max(2048, batch_size * 4) < batch_size:
                await progress(f"🔍 扫描中… 已检查 {scanned}/{total} 个端口")
        return None

    # -------------------------------------------------------------- 会话操作

    async def ping(self) -> dict:
        """连通性 + 宿主信息。"""
        base = await self.discover()
        info: dict = {"base": base}
        try:
            describe = await self.call("host.describe", {}, timeout=10)
            info["host"] = describe
        except DshError as exc:
            info["host_error"] = str(exc)
        try:
            sessions = await self.call("session.list", {}, timeout=10)
            items = sessions.get("items") or []
            info["sessions"] = len(items)
            running = [s for s in items if s.get("running")]
            info["running"] = len(running)
        except DshError as exc:
            info["sessions_error"] = str(exc)
        return info

    async def run_prompt(
        self,
        prompt: str,
        cwd: str | None = None,
        session_id: str | None = None,
        timeout: float = 900.0,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        max_result_chars: int = 6000,
        progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict:
        """向 DSH 投递一条 prompt，轮询直到 agent 完成，返回最终回复。

        返回 {"text", "sessionId", "duration_ms", "events", "truncated"}
        超时/失败抛 DshError。
        """
        start = time.monotonic()
        await self.discover()

        # 1. 确定会话
        own_session = session_id is None
        if own_session:
            create_payload: dict = {}
            if cwd:
                create_payload["cwd"] = cwd
            created = await self.call("session.create", create_payload, timeout=30)
            session_id = created.get("sessionId")
            if not session_id:
                raise DshError("DSH 未返回会话 ID")
        if progress:
            await progress(f"📡 已接入 DSH 会话 {session_id}，任务已投递，等待 DSH 智能体执行…")

        # 2. 投递 prompt
        try:
            await self.call(
                "session.prompt",
                {
                    "sessionId": session_id,
                    "mode": "queue",
                    "content": [{"type": "text", "text": prompt}],
                    "clientTimeZone": self._tz_name(),
                },
                timeout=30,
            )
        except DshError:
            if own_session:
                await self._safe_cancel(session_id)
            raise

        # 3. 轮询等待完成（turn/end 事件）
        anchor = prompt.strip().replace("\n", " ")[:80]
        polled = 0
        texts: list[str] = []
        done = False
        try:
            while time.monotonic() - start < timeout:
                await asyncio.sleep(poll_interval)
                polled += 1
                history = await self.call(
                    "session.history",
                    {"sessionId": session_id, "maxMessages": 300},
                    timeout=30,
                )
                events = history.get("events") or []
                for entry in events:
                    ev = entry.get("event") or entry
                    ev_type = ev.get("type") or ""
                    data = ev.get("data") or {}
                    if ev_type == "assistant/message":
                        text = self._extract_text(data)
                        if text and (not texts or texts[-1] != text):
                            texts.append(text)
                    elif ev_type == "user/message":
                        ut = self._extract_text(data)
                        if ut and anchor and anchor in ut.replace("\n", " "):
                            pass  # 锚点确认该轮已经开始，无需额外动作
                    elif ev_type == "turn/end":
                        done = True
                if done and texts:
                    break
        except asyncio.CancelledError:
            await self._safe_cancel(session_id)
            raise
        except DshError as exc:
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
            "polled": polled,
            "truncated": truncated,
        }

    @staticmethod
    def _extract_text(data: dict) -> str:
        message = data.get("message") or {}
        content = message.get("content") or []
        if isinstance(content, str):
            return content
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts).strip()

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
            await self.call("session.cancel", {"sessionId": session_id}, timeout=10)
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
