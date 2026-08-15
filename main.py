"""AstrBot 插件：DSH Bridge —— 让 AstrBot 智能体驱动 DeepSeek Harness 智能体。

功能:
    1. 自动发现 DSH: 扫描 127.0.0.1 上的端口（默认 59934 优先），用 HTTP 指纹识别
       DeepSeek Harness Web 服务；支持手动指定端口 / 局域网主机。
    2. LLM 工具注入: 向 AstrBot 主智能体注册 dsh_ask / dsh_ping 两个函数工具，
       AstrBot 智能体（即 AstrBot 的 agent）可直接调用，把任务投递给 DSH 智能体
       并把 DSH 的最终回复带回来。
    3. Prompt 注入: 通过 on_llm_request 钩子向每次 LLM 请求的系统提示词追加使用
       说明，让主智能体知道何时、如何调用 DSH。
    4. 管理员命令: /dsh ping / /dsh scan / /dsh ask <任务> / /dsh session <id> <任务>
       / /dsh status / /dsh help。

适配性:
    - 纯 Python + aiohttp（AstrBot 核心自带），Windows / Linux / macOS / Docker 通用；
    - AstrBot 与 DSH 可在不同设备（dsh_host 配置局域网 IP）。
"""

from __future__ import annotations

import re

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.message.message_event_result import MessageChain

from .dsh_client import DshClient, DshError

DEFAULT_INJECT_PROMPT = (
    "\n\n[DSH Bridge 系统提示]\n"
    "你拥有两个与 DeepSeek Harness（DSH，运行在用户电脑上的智能体）联动的工具：\n"
    "- dsh_ask：把任务委托给 DSH 智能体执行，并返回它的最终回复；\n"
    "- dsh_ping：检测 DSH 是否在线。\n"
    "使用时机：任务需要在用户电脑上完成（运行命令、读写文件、访问本地数据、长时间后台工作等），"
    "或用户明确要求「交给电脑上的智能体 / DSH」时。\n"
    "注意事项：\n"
    "1) 调用 dsh_ask 时，把用户意图完整、自包含地写成任务描述，不要省略上下文；\n"
    "2) dsh_ask 返回的是 DSH 智能体的最终回复，请如实转述关键内容，不要编造其未提供的信息；\n"
    "3) 若 dsh_ping 显示 DSH 未连接，请告知用户 DSH 未运行或未被发现，并建议执行 /dsh scan 重新扫描。\n"
)

USAGE = (
    "DSH Bridge 用法：\n"
    "  /dsh help            显示本帮助\n"
    "  /dsh ping            检测 DSH 连接状态\n"
    "  /dsh status          连接状态 + 当前配置摘要\n"
    "  /dsh scan            强制重新扫描 DSH 端口\n"
    "  /dsh ask <任务描述>   把任务交给 DSH 智能体执行并返回结果\n"
    "  /dsh session <会话ID> <任务描述>  在指定 DSH 会话中执行任务\n"
    "主智能体（对话模式）可通过内置工具 dsh_ask / dsh_ping 自动调用 DSH。"
)


class Main(Star):
    """DSH Bridge：AstrBot ↔ DeepSeek Harness 双向桥接。

    安装后在消息平台使用 /dsh 系列命令，或直接让 AstrBot 智能体通过
    dsh_ask 工具调用本机 DSH 智能体。
    """

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config = config or {}
        self.client = DshClient(self.config, logger=self.logger)

    async def initialize(self) -> None:
        """插件激活时后台预热端口发现，避免首次调用等待全量扫描。"""
        self.client.start_warmup()
        self.logger.info("[DSH Bridge] 插件已激活，后台端口发现已启动。")

    async def terminate(self) -> None:
        await self.client.close()
        self.logger.info("[DSH Bridge] 插件已停用。")

    # ------------------------------------------------------------ 内部工具

    def _cfg(self, key: str, default):
        return self.config.get(key, default)

    async def _progress(self, event: AstrMessageEvent, text: str) -> None:
        """发送一条进度消息（非最终结果）。"""
        try:
            await event.send(MessageChain().message(text))
        except Exception as exc:  # noqa: BLE001 - 进度消息失败不影响主流程
            self.logger.debug(f"[DSH Bridge] 进度消息发送失败: {exc}")

    def _parse_command(self, event: AstrMessageEvent) -> tuple[str, list[str]]:
        """从消息中解析 /dsh <action> <args...>，兼容 @提及 / 唤醒前缀。"""
        raw = event.get_message_str().strip()
        tokens = [t for t in re.split(r"\s+", raw) if t]
        idx = next(
            (
                i
                for i, t in enumerate(tokens)
                if t.lower().lstrip("/@").rstrip(":，,。") == "dsh"
            ),
            None,
        )
        if idx is None:
            return "help", []
        rest = tokens[idx + 1 :]
        action = rest[0].lower() if rest else "help"
        return action, rest[1:]

    @staticmethod
    def _fmt_duration(ms: int) -> str:
        if ms < 1000:
            return f"{ms}ms"
        return f"{ms / 1000:.1f}s"

    # ------------------------------------------------------------ 命令

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("dsh")
    async def dsh(self, event: AstrMessageEvent) -> None:
        """DSH Bridge 主命令：/dsh help|ping|scan|status|ask|session"""
        action, args = self._parse_command(event)

        if action in ("help", "?", "-h"):
            yield event.plain_result(USAGE)
            return

        if action == "ping":
            try:
                info = await self.client.ping()
            except DshError as exc:
                yield event.plain_result(f"❌ {exc}")
                return
            lines = [f"✅ DSH 已连接：{info.get('base')}"]
            host_info = info.get("host") or {}
            if isinstance(host_info, dict):
                for key in ("name", "version", "app", "appName", "hostname"):
                    if host_info.get(key):
                        lines.append(f"   {key}: {host_info[key]}")
            if info.get("sessions") is not None:
                lines.append(f"   会话总数: {info['sessions']}，运行中: {info.get('running', 0)}")
            if info.get("sessions_error"):
                lines.append(f"   ⚠️ 会话查询失败: {info['sessions_error']}")
            yield event.plain_result("\n".join(lines))
            return

        if action == "scan":
            async def progress(text: str):
                await self._progress(event, text)

            try:
                found = await self.client.discover(force=True, progress=progress)
            except DshError as exc:
                yield event.plain_result(f"❌ {exc}")
                return
            yield event.plain_result(f"✅ 扫描完成，DSH 位于 {found}（已缓存，可配置 dsh_port 固定端口）")
            return

        if action == "status":
            try:
                info = await self.client.ping()
            except DshError as exc:
                info = {"base": "未连接"}
                yield event.plain_result(f"❌ {exc}")
            lines = [f"🌐 DSH: {info.get('base')}"]
            cfg_lines = [
                ("host", self.client.host),
                ("port(0=自动)", self.client.port_override),
                ("区间", self._cfg("port_ranges", "默认")),
                ("缓存TTL(s)", self.client.cache_ttl),
                ("任务超时(s)", self._cfg("request_timeout_seconds", 900)),
                ("默认cwd", self._cfg("default_cwd", "") or "（DSH 默认）"),
                ("会话模式", self._cfg("session_mode", "new")),
                ("注入prompt", "开" if self._cfg("inject_prompt_enabled", True) else "关"),
            ]
            lines.append("⚙️ 配置:")
            lines.extend(f"   {k}: {v}" for k, v in cfg_lines)
            yield event.plain_result("\n".join(lines))
            return

        if action in ("ask", "session"):
            prompt = " ".join(args).strip()
            session_id = None
            if action == "session":
                if len(args) < 2:
                    yield event.plain_result("用法: /dsh session <会话ID> <任务描述>")
                    return
                session_id = args[0]
                prompt = " ".join(args[1:]).strip()
            if not prompt:
                yield event.plain_result("任务描述不能为空。用法: /dsh ask <任务描述>")
                return

            # 会话模式：new（每次新建，默认）/ persistent（复用固定会话）
            if session_id is None and self._cfg("session_mode", "new") == "persistent":
                session_id = str(self._cfg("persistent_session_id", "") or "").strip() or None

            cwd = str(self._cfg("default_cwd", "") or "").strip() or None
            timeout = max(30, int(self._cfg("request_timeout_seconds", 900) or 900))
            poll = max(1.0, float(self._cfg("poll_interval_seconds", 2.0) or 2.0))
            max_chars = max(200, int(self._cfg("max_result_chars", 6000) or 6000))

            async def progress(text: str):
                await self._progress(event, text)

            try:
                result = await self.client.run_prompt(
                    prompt,
                    cwd=cwd,
                    session_id=session_id,
                    timeout=timeout,
                    poll_interval=poll,
                    max_result_chars=max_chars,
                    progress=progress,
                )
            except DshError as exc:
                yield event.plain_result(f"❌ {exc}")
                return
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("[DSH Bridge] ask 命令异常")
                yield event.plain_result(f"❌ 插件内部错误: {exc}")
                return

            head = (
                f"✅ DSH 执行完成（会话 {result['sessionId']}，用时 {self._fmt_duration(result['duration_ms'])}）:\n\n"
            )
            yield event.plain_result(head + result["text"])
            return

        yield event.plain_result(USAGE)

    # ------------------------------------------------------------ LLM 工具

    @filter.llm_tool("dsh_ask")
    async def dsh_ask(self, event: AstrMessageEvent, prompt: str):
        """把任务委托给同一台设备/局域网上的 DeepSeek Harness（DSH）桌面智能体执行，并返回它的最终回复。适合需要在用户电脑上运行命令、读写文件、联网搜索或做长耗时工作的任务，或用户要求「交给电脑上的智能体」时。

        Args:
            prompt(string): 要交给 DSH 智能体执行的完整任务描述，尽量具体、自包含。
        """
        timeout = max(30, int(self._cfg("request_timeout_seconds", 900) or 900))
        poll = max(1.0, float(self._cfg("poll_interval_seconds", 2.0) or 2.0))
        max_chars = max(200, int(self._cfg("max_result_chars", 6000) or 6000))
        cwd = str(self._cfg("default_cwd", "") or "").strip() or None
        session_id = None
        if self._cfg("session_mode", "new") == "persistent":
            session_id = str(self._cfg("persistent_session_id", "") or "").strip() or None
        try:
            result = await self.client.run_prompt(
                prompt,
                cwd=cwd,
                session_id=session_id,
                timeout=timeout,
                poll_interval=poll,
                max_result_chars=max_chars,
            )
        except DshError as exc:
            return f"[DSH Bridge] 调用失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("[DSH Bridge] dsh_ask 工具异常")
            return f"[DSH Bridge] 插件内部错误：{exc}"
        return (
            f"[DSH Bridge] DSH 智能体执行结果（会话 {result['sessionId']}，"
            f"用时 {self._fmt_duration(result['duration_ms'])}）：\n{result['text']}"
        )

    @filter.llm_tool("dsh_ping")
    async def dsh_ping(self, event: AstrMessageEvent):
        """检测本机 DeepSeek Harness（DSH）智能体是否在线可用，返回连接地址与状态。

        Args:
        """
        try:
            info = await self.client.ping()
        except DshError as exc:
            return f"[DSH Bridge] DSH 未连接：{exc}"
        lines = [f"[DSH Bridge] DSH 在线：{info.get('base')}"]
        if info.get("sessions") is not None:
            lines.append(f"会话总数 {info['sessions']}，运行中 {info.get('running', 0)}")
        return "\n".join(lines)

    # ------------------------------------------------------------ Prompt 注入

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """向主智能体的每次 LLM 请求注入 DSH 工具使用说明。"""
        if not self._cfg("inject_prompt_enabled", True):
            return
        try:
            text = str(self._cfg("inject_prompt_text", DEFAULT_INJECT_PROMPT) or "")
        except Exception:
            text = DEFAULT_INJECT_PROMPT
        if text and text not in (req.system_prompt or ""):
            req.system_prompt = (req.system_prompt or "") + text
