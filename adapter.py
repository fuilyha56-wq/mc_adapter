"""MC Adapter - Minecraft 平台适配器。

适配器作为 WebSocket 客户端连接到 Node.js bridge 进程，负责：
1. 入站：将 bridge 推送的 JSON 消息（chat/whisper/system_message/self_died 等）
   转换为 mofox-wire 的 MessageEnvelope，并交给 core_sink。
2. 出站（普通聊天）：将核心下发的 MessageEnvelope 中的文本段转换为 bridge 的
   send_chat 指令，通过 WebSocket 发回 bridge。
3. 出站（适配器命令）：识别核心下发的 adapter_command 段，转发为 bridge RPC
   调用，等待 bridge rpc_response 后构造 adapter_response 信封回送核心。
   这条路径与 AdapterManager.send_adapter_command 对接，供 MCBridgeService
   或其他插件通过 adapter_api.send_adapter_command 调用。
4. 事件系统：每条 bridge 入站消息（即一次"API 回传"）都会按其 type 派发同名
   事件到全局事件总线（事件名前缀 ``mc_adapter.``），供外部组件订阅。例如
   bot 死亡时除转化为文字注入聊天流外，还会发布 ``mc_adapter.self_died`` 事件。

v1 仅支持纯文本聊天；后续可通过在 bridge HANDLERS 中新增方法 + 在本适配器
的 _handle_adapter_command 中登记 action 派发，扩展更多能力（移动/交互等），
无需改动协议层。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any, cast

import orjson
from mofox_wire import (
    CoreSink,
    MessageBuilder,
    MessageEnvelope,
    WebSocketAdapterOptions,
)
from mofox_wire.types import UserRole

from src.app.plugin_system.api.log_api import get_logger
from src.core.config.core_config import get_core_config
from src.core.components.base import BaseAdapter
from src.kernel.event import get_event_bus

from .config import MCAdapterConfig
from .protocol import (
    INCOMING_TYPE_CHAT,
    INCOMING_TYPE_ERROR,
    INCOMING_TYPE_RPC_RESPONSE,
    INCOMING_TYPE_SELF_CONNECTED,
    INCOMING_TYPE_SELF_DIED,
    INCOMING_TYPE_SELF_DISCONNECTED,
    INCOMING_TYPE_SELF_RESPAWNED,
    INCOMING_TYPE_SYSTEM_MESSAGE,
    INCOMING_TYPE_WHISPER,
    OUTGOING_TYPE_SEND_CHAT,
)

if TYPE_CHECKING:
    from .plugin import MCAdapterPlugin

logger = get_logger("mc_adapter")

#: 平台标识，用于 MessageEnvelope.message_info.platform 与核心路由
PLATFORM = "minecraft"

#: adapter_command 段的 type 标识（由 AdapterManager.send_adapter_command 构造）
SEG_TYPE_ADAPTER_COMMAND = "adapter_command"
#: adapter_response 段的 type 标识（适配器回送给核心，由 message receiver 解析）
SEG_TYPE_ADAPTER_RESPONSE = "adapter_response"

#: 事件总线前缀：所有 MC 适配器事件均以此前缀发布到全局事件总线，
#: 事件名为 ``EVENT_PREFIX + <bridge incoming type>``，如 ``mc_adapter.self_died``。
EVENT_PREFIX = "mc_adapter."

#: 死亡原因（MC ``translate`` key）到中文描述的映射。
#: 仅覆盖无 killer 的环境死亡；有 killer 时统一格式化为 "你被{killer}杀死了"。
_DEATH_CAUSE_MESSAGES: dict[str, str] = {
    "death.attack.fall": "你摔死了",
    "death.attack.lava": "你被岩浆烧死了",
    "death.attack.inFire": "你被火烧死了",
    "death.attack.onFire": "你被烧死了",
    "death.attack.drown": "你淹死了",
    "death.attack.starve": "你饿死了",
    "death.attack.cactus": "你被仙人掌扎死了",
    "death.attack.lightningBolt": "你被雷劈死了",
    "death.attack.wither": "你被凋零杀死了",
    "death.attack.magic": "你被魔法杀死了",
    "death.attack.explosion": "你被炸死了",
    "death.attack.explosion.player": "你被炸死了",
    "death.attack.outOfWorld": "你掉出了世界",
    "death.attack.flyIntoWall": "你撞墙死了",
    "death.attack.hotFloor": "你被烫死了",
    "death.attack.generic": "你死亡了",
    "death.attack.anvil": "你被铁砧砸死了",
    "death.attack.fallingBlock": "你被掉落的方块砸死了",
    "death.attack.inWall": "你被憋死了",
    "death.attack.fireworks": "你被烟花炸死了",
}


class MCAdapter(BaseAdapter):
    """Minecraft 平台适配器。

    通过 WebSocket 连接 mineflayer bridge，将 MC 聊天事件桥接进 Neo-MoFox 核心，
    并支持通过 adapter_command 通道调用 bridge 暴露的 RPC 方法。

    事件系统：
        每条 bridge 入站消息（即一次"API 回传"）都会按其 ``type`` 字段派发同名
        事件到全局事件总线（``src.kernel.event.get_event_bus``），事件名形如
        ``mc_adapter.<type>``（如 ``mc_adapter.self_died`` / ``mc_adapter.chat``）。
        事件参数为 ``{"type": ..., "raw": <原始 payload>, "adapter": <签名>,
        "platform": "minecraft"}``。外部组件可通过 ``get_event_bus().subscribe``
        订阅这些事件，无需修改适配器内部逻辑即可响应 bridge 事件。

    死亡消息处理：
        bridge 的 ``self_died`` 仅携带 ``username``/``uuid``/``timestamp`` 基础
        信息，以及（若成功捕获）``killer``/``death_message``/``cause``。
        本适配器将其转译为中文文字信封注入聊天流：
        - 有 killer 时格式化为 ``你被{killer}杀死了``
        - 无 killer 时按 ``cause`` 查 ``_DEATH_CAUSE_MESSAGES``，未命中则兜底
          ``你死亡了``
        - 末尾追加"将自动重生"提示，告知 AI 无需手动重生
        受 ``features.forward_death_messages`` 配置项控制；关闭后仅派发事件，
        不注入聊天流。
    """

    name = "mc_adapter"
    adapter_version = "1.0.0"
    adapter_author = "MoFox Team"
    description = "基于 mineflayer bridge 的 Minecraft 平台适配器"
    platform = PLATFORM

    run_in_subprocess = False

    def __init__(
        self,
        core_sink: CoreSink,
        plugin: "MCAdapterPlugin | None" = None,
        **kwargs: Any,
    ) -> None:
        """初始化 MC 适配器。

        Args:
            core_sink: 核心消息接收器。
            plugin: 所属插件实例。
            **kwargs: 透传给 BaseAdapter 的其他参数。
        """
        if plugin and plugin.config:
            config = cast(MCAdapterConfig, plugin.config)
            br = config.bridge
            ws_url = f"ws://{br.host}:{br.port}{br.path}"
            headers: dict[str, str] | None = None
            if br.secret:
                headers = {"Authorization": f"Bearer {br.secret}"}
            # max_reconnect_attempts=0 表示无限重连（与基类 None 语义一致）
            max_attempts = br.max_reconnect_attempts if br.max_reconnect_attempts > 0 else None
            transport = WebSocketAdapterOptions(
                mode="client",
                url=ws_url,
                headers=headers,
                reconnect_interval=br.reconnect_interval,
                max_reconnect_attempts=max_attempts,
            )
        else:
            transport = WebSocketAdapterOptions(
                mode="client",
                url="ws://127.0.0.1:8097/mc",
            )

        super().__init__(core_sink, plugin=plugin, transport=transport, **kwargs)

        # bridge RPC 响应池：rpc id -> Future。
        # 仅在 adapter_command 处理路径内部使用，外部调用应走
        # adapter_api.send_adapter_command 或 MCBridgeService。
        self._rpc_pool: dict[str, asyncio.Future[Any]] = {}
        self._rpc_default_timeout: float = 30.0
        if plugin and plugin.config:
            self._rpc_default_timeout = cast(MCAdapterConfig, plugin.config).bridge.rpc_timeout

        # Bot 在线状态缓存（由 self_connected/self_disconnected 维护）
        self._bot_online: bool = False
        self._bot_uuid: str = ""
        # MC 服务器地址缓存（由 self_connected 维护），
        # 用于将公共聊天标记为群聊消息（服务器即"群"）。
        self._server_host: str = ""
        self._server_port: int = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def on_adapter_loaded(self) -> None:
        """适配器加载完成。"""
        if not self.plugin or not self.plugin.config:
            raise RuntimeError("MC 适配器启动失败：缺少插件配置")
        config = cast(MCAdapterConfig, self.plugin.config)
        if not config.bot.username.strip():
            raise ValueError("配置项 bot.username 无效：必须为非空字符串")
        logger.info(
            f"MC 适配器正在启动，目标 bridge: {config.bridge.host}:{config.bridge.port}"
        )

    async def on_adapter_unloaded(self) -> None:
        """适配器卸载，清理 RPC 响应池。"""
        logger.info("MC 适配器正在关闭...")
        for future in self._rpc_pool.values():
            if not future.done():
                future.cancel()
        self._rpc_pool.clear()
        self._bot_online = False
        logger.info("MC 适配器已关闭")

    # ------------------------------------------------------------------
    # 入站：bridge JSON -> MessageEnvelope
    # ------------------------------------------------------------------

    async def from_platform_message(self, raw: Any) -> MessageEnvelope | None:  # type: ignore[override]
        """将 bridge 推送的 JSON 消息转换为 MessageEnvelope。

        Args:
            raw: bridge 推送的 JSON 字典。

        Returns:
            MessageEnvelope | None：可识别的聊天/系统/死亡消息返回信封；
            生命周期事件与 RPC 响应返回 None（已在内部处理）。

        Note:
            每条入站消息（即一次"API 回传"）都会先按其 ``type`` 派发同名事件
            到全局事件总线（事件名前缀 ``mc_adapter.``），再走适配器自身的
            消息处理流程。事件派发失败不影响后续处理。
        """
        if not isinstance(raw, dict):
            logger.warning(f"收到非 dict 消息: {raw!r}")
            return None

        msg_type = raw.get("type")

        # 事件派发：每个 bridge 入站消息都触发一次同名事件
        if isinstance(msg_type, str):
            await self._publish_bridge_event(msg_type, raw)

        # RPC 响应：匹配响应池中的 Future
        if msg_type == INCOMING_TYPE_RPC_RESPONSE:
            self._resolve_rpc(raw)
            return None

        # 生命周期事件：维护在线状态，不注入聊天流
        if msg_type == INCOMING_TYPE_SELF_CONNECTED:
            self._bot_online = True
            self._bot_uuid = str(raw.get("uuid", ""))
            self._server_host = str(raw.get("host", "")) or self._server_host
            port_raw = raw.get("port")
            if isinstance(port_raw, int) and port_raw > 0:
                self._server_port = port_raw
            logger.info(
                f"Bot 已加入游戏: {raw.get('username', '')} (uuid={self._bot_uuid or 'unknown'})"
            )
            return None

        if msg_type == INCOMING_TYPE_SELF_DISCONNECTED:
            self._bot_online = False
            logger.warning(f"Bot 已断开: {raw.get('reason', 'unknown')}")
            return None

        if msg_type == INCOMING_TYPE_SELF_DIED:
            # bot 死亡但仍连接在服务器，不视为断线；
            # 按 features.forward_death_messages 决定是否转化为文字注入聊天流。
            username = str(raw.get("username", ""))
            uuid_str = str(raw.get("uuid", "")) or "unknown"
            killer = str(raw.get("killer", "") or "")
            cause = str(raw.get("cause", "") or "")
            logger.warning(
                f"Bot 死亡: {username} (uuid={uuid_str})"
                + (f" killer={killer}" if killer else "")
                + (f" cause={cause}" if cause else "")
            )
            config = self._get_config()
            if config and not config.features.forward_death_messages:
                return None
            return self._build_death_envelope(raw)

        if msg_type == INCOMING_TYPE_SELF_RESPAWNED:
            # 死亡后重生，状态从死亡恢复到正常在线；_bot_online 保持 True。
            logger.info(
                f"Bot 已重生: {raw.get('username', '')} (uuid={raw.get('uuid', '') or 'unknown'})"
            )
            return None

        if msg_type == INCOMING_TYPE_ERROR:
            logger.error(
                f"Bridge 错误: code={raw.get('code', '')} message={raw.get('message', '')}"
            )
            return None

        if msg_type == INCOMING_TYPE_CHAT:
            return self._build_chat_envelope(raw, whisper=False)

        if msg_type == INCOMING_TYPE_WHISPER:
            config = self._get_config()
            if config and not config.features.forward_whisper_as_chat:
                logger.debug(f"私聊消息被配置忽略: {raw!r}")
                return None
            return self._build_chat_envelope(raw, whisper=True)

        if msg_type == INCOMING_TYPE_SYSTEM_MESSAGE:
            config = self._get_config()
            if config and not config.features.forward_system_messages:
                return None
            return self._build_system_envelope(raw)

        logger.warning(f"未知 bridge 消息类型: {msg_type}")
        return None

    def _build_chat_envelope(self, raw: dict[str, Any], *, whisper: bool) -> MessageEnvelope:
        """构造聊天/私聊消息信封。

        无论公共聊天还是私聊（whisper）都归入同一群聊流：MC 服务器即"群"，
        所有消息都标记 group_info。whisper 仅以 ``[私聊] `` 文本前缀作区分，
        便于核心/插件在统一聊天流中识别来源。
        """
        if whisper:
            username = str(raw.get("from", ""))
            message = str(raw.get("message", ""))
        else:
            username = str(raw.get("username", ""))
            message = str(raw.get("message", ""))
        uuid_str = str(raw.get("uuid", "")) or username
        timestamp_ms = int(raw.get("timestamp") or 0) or None

        builder = MessageBuilder()
        builder.direction("incoming").platform(PLATFORM).message_id(
            f"mc_{uuid.uuid4().hex}"
        )
        if timestamp_ms:
            builder.timestamp_ms(timestamp_ms)
        else:
            builder.timestamp_ms()
        builder.from_user(
            user_id=uuid_str,
            platform=PLATFORM,
            nickname=username,
            role=UserRole.MEMBER,
        )
        # 所有聊天（含 whisper）统一归入服务器群聊流；
        # group_id 取 self_connected 时缓存的 MC 服务器地址。
        group_id, group_name = self._server_group_info()
        if group_id:
            builder.from_group(
                group_id=group_id,
                platform=PLATFORM,
                name=group_name,
            )
        prefix = "[私聊] " if whisper else ""
        builder.seg("text", f"{prefix}{message}")
        builder.format_info(content_format=["text"], accept_format=["text"])
        envelope = builder.build()
        envelope["raw_message"] = raw
        return envelope

    def _build_system_envelope(self, raw: dict[str, Any]) -> MessageEnvelope:
        """构造系统消息信封（玩家加入/离开等）。"""
        message = str(raw.get("message", ""))
        builder = MessageBuilder()
        builder.direction("incoming").platform(PLATFORM).message_id(
            f"mc_sys_{uuid.uuid4().hex}"
        )
        builder.timestamp_ms(int(raw.get("timestamp") or 0) or None)
        builder.from_user(
            user_id="system",
            platform=PLATFORM,
            nickname="系统消息",
            role=UserRole.OTHER,
        )
        # 系统消息同样归入服务器群聊流，与聊天/私聊统一上下文。
        group_id, group_name = self._server_group_info()
        if group_id:
            builder.from_group(
                group_id=group_id,
                platform=PLATFORM,
                name=group_name,
            )
        builder.seg("text", message)
        builder.format_info(content_format=["text"], accept_format=["text"])
        envelope = builder.build()
        envelope["raw_message"] = raw
        return envelope

    def _build_death_envelope(self, raw: dict[str, Any]) -> MessageEnvelope:
        """构造 bot 死亡事件信封（转化为文字注入聊天流）。

        bridge 的 ``self_died`` 携带 ``username``/``uuid``/``timestamp`` 及
        （若成功捕获）``killer``/``death_message``/``cause``。本方法将其转译为
        中文文字信封，与 system_message 同流注入核心聊天上下文，使 AI 能感知
        bot 死亡事件并据此回应。
        """
        text = self._format_death_text(raw)
        builder = MessageBuilder()
        builder.direction("incoming").platform(PLATFORM).message_id(
            f"mc_death_{uuid.uuid4().hex}"
        )
        builder.timestamp_ms(int(raw.get("timestamp") or 0) or None)
        builder.from_user(
            user_id="system",
            platform=PLATFORM,
            nickname="系统消息",
            role=UserRole.OTHER,
        )
        # 死亡事件同样归入服务器群聊流，便于 AI 在同一上下文中感知。
        group_id, group_name = self._server_group_info()
        if group_id:
            builder.from_group(
                group_id=group_id,
                platform=PLATFORM,
                name=group_name,
            )
        builder.seg("text", text)
        builder.format_info(content_format=["text"], accept_format=["text"])
        envelope = builder.build()
        envelope["raw_message"] = raw
        return envelope

    @staticmethod
    def _format_death_text(raw: dict[str, Any]) -> str:
        """将 self_died payload 转译为中文死亡描述文字。

        - 有 ``killer``（玩家/生物击杀）：格式化为 ``你被{killer}杀死了``
        - 无 killer 但有 ``cause``：查 ``_DEATH_CAUSE_MESSAGES``，未命中兜底
          ``你死亡了``
        - 二者均无：兜底 ``你死亡了``
        - 末尾追加"将自动重生"提示，告知 AI bot 会自动复活，无需调用重生工具。
        """
        nickname = get_core_config().personality.nickname
        killer = str(raw.get("killer", "") or "").strip()
        if killer:
            text = f"你({nickname})被{killer}杀死了"
        else:
            cause = str(raw.get("cause", "") or "").strip()
            if cause and cause in _DEATH_CAUSE_MESSAGES:
                text = _DEATH_CAUSE_MESSAGES[cause].replace("你", f"你({nickname})")
            else:
                text = f"你({nickname})死亡了"
        return f"{text}（将自动重生，无需手动重生）"

    async def _publish_bridge_event(self, msg_type: str, raw: dict[str, Any]) -> None:
        """将 bridge 入站消息作为事件发布到全局事件总线。

        每条入站消息（即一次"API 回传"）都按 ``EVENT_PREFIX + msg_type``
        派发同名事件，事件参数包含原始 payload 与适配器签名等元信息，
        供外部组件订阅响应。

        事件参数 key 集合固定为 ``{"type", "raw", "adapter", "platform"}``，
        订阅者若返回的参数 key 集合不一致，其修改会被事件总线丢弃（PASS），
        但不影响后续订阅者或适配器自身处理流程。
        """
        event_name = f"{EVENT_PREFIX}{msg_type}"
        params = {
            "type": msg_type,
            "raw": raw,
            "adapter": self.get_signature() or self.name,
            "platform": self.platform,
        }
        try:
            await get_event_bus().publish(event_name, params)
        except Exception as e:
            logger.error(
                f"发布事件 '{event_name}' 失败: {e}",
                exc_info=e,
            )

    # ------------------------------------------------------------------
    # 出站：MessageEnvelope -> bridge
    # ------------------------------------------------------------------

    async def _send_platform_message(self, envelope: MessageEnvelope) -> None:  # type: ignore[override]
        """将核心下发的消息转译为 bridge 协议并发送。

        分两条路径：
        1. ``adapter_command`` 段：核心通过 AdapterManager.send_adapter_command
           下发的 RPC 调用。转译为 bridge rpc 请求，等待响应后构造
           ``adapter_response`` 信封回送核心。
        2. 其他段（text/seglist）：普通聊天回复，提取文本后下发 send_chat。
        """
        segment = envelope.get("message_segment")
        seg_type = segment.get("type") if isinstance(segment, dict) else None

        if seg_type == SEG_TYPE_ADAPTER_COMMAND:
            await self._handle_adapter_command(envelope, segment)  # type: ignore[arg-type]
            return

        await self._send_chat_envelope(envelope)

    async def _send_chat_envelope(self, envelope: MessageEnvelope) -> None:
        """普通聊天出站：从 envelope 提取文本并通过 send_chat 下发。

        v1 仅支持纯文本：从 message_segment 中提取所有 text 段拼接后下发。
        后续若需支持富格式（图片/命令），可在此分支处理。
        """
        text = self._extract_text(envelope)
        if not text:
            logger.warning("MC 适配器暂不支持非文本消息，跳过发送")
            return

        if not self._ws:
            logger.warning("WebSocket 未连接，无法发送消息")
            return

        payload = orjson.dumps(
            {"type": OUTGOING_TYPE_SEND_CHAT, "message": text}
        ).decode()
        try:
            await self._ws.send(payload)
            logger.debug(f"已发送聊天到 bridge: {text[:200]}")
        except Exception as e:
            logger.error(f"发送聊天到 bridge 失败: {e}")

    async def _handle_adapter_command(
        self,
        envelope: MessageEnvelope,
        segment: dict[str, Any],
    ) -> None:
        """处理核心下发的 adapter_command 段。

        将 ``action`` / ``params`` 转译为 bridge rpc 调用，等待响应后
        构造 ``adapter_response`` 信封通过 core_sink 回送核心。
        核心的 message receiver 会按 request_id 派发给等待中的
        AdapterManager.send_adapter_command 调用方。

        响应字典遵循 AdapterManager 文档约定：
        - 成功: ``{"status": "ok", "data": ..., "message": ""}``
        - 失败: ``{"status": "failed", "message": err}``
        - 错误: ``{"status": "error", "message": err}``
        """
        raw_data = segment.get("data")
        seg_data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
        request_id = str(seg_data.get("request_id") or "")
        action = str(seg_data.get("action") or "")
        params = seg_data.get("params") or {}
        timeout = float(seg_data.get("timeout") or self._rpc_default_timeout)

        if not request_id:
            logger.error("adapter_command 缺少 request_id，无法回送响应")
            return
        if not action:
            await self._send_adapter_response(request_id, {
                "status": "failed",
                "message": "adapter_command 缺少 action 参数",
                "data": None,
            })
            return

        try:
            result = await self._call_bridge_rpc(action, params, timeout=timeout)
            await self._send_adapter_response(request_id, {
                "status": "ok",
                "data": result,
                "message": "",
            })
        except asyncio.TimeoutError:
            logger.error(f"adapter_command 超时: action={action} request_id={request_id}")
            await self._send_adapter_response(request_id, {
                "status": "error",
                "message": f"bridge rpc 超时（{timeout}s）: action={action}",
                "data": None,
            })
        except Exception as e:
            logger.error(
                f"adapter_command 执行失败: action={action} request_id={request_id} err={e}"
            )
            await self._send_adapter_response(request_id, {
                "status": "error",
                "message": f"bridge rpc 失败: {e}",
                "data": None,
            })

    async def _send_adapter_response(self, request_id: str, response: dict[str, Any]) -> None:
        """构造 adapter_response 信封并推回核心。

        核心的 MessageReceiver 检测到 ``adapter_response`` 段后会调用
        ``_set_adapter_response`` 解除 ``AdapterManager.send_adapter_command``
        调用方的 Future 等待。
        """
        if not self.core_sink:
            logger.warning("core_sink 不可用，无法回送 adapter_response")
            return
        response_envelope: MessageEnvelope = {
            "direction": "incoming",  # type: ignore[typeddict-item]
            "message_info": {
                "message_id": request_id,
                "platform": self.platform,
                "time": 0,
            },
            "message_segment": {  # type: ignore[typeddict-item]
                "type": SEG_TYPE_ADAPTER_RESPONSE,
                "data": {
                    "request_id": request_id,
                    "response": response,
                },
            },
        }
        try:
            await self.core_sink.send(response_envelope)
        except Exception as e:
            logger.error(f"回送 adapter_response 失败: {e}")

    @staticmethod
    def _extract_text(envelope: MessageEnvelope) -> str:
        """从 envelope.message_segment 中提取纯文本。"""
        segment = envelope.get("message_segment")
        if segment is None:
            return ""
        if isinstance(segment, list):
            parts: list[str] = []
            for seg in segment:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    data = seg.get("data", "")
                    if isinstance(data, str):
                        parts.append(data)
            return "".join(parts)
        if isinstance(segment, dict):
            if segment.get("type") == "text":
                data = segment.get("data", "")
                if isinstance(data, str):
                    return data
        return ""

    # ------------------------------------------------------------------
    # bridge RPC（内部）：供 adapter_command 路径调用
    # ------------------------------------------------------------------

    async def _call_bridge_rpc(
        self,
        method: str,
        args: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """调用 bridge 暴露的 RPC 方法并等待响应。

        内部使用：通过 adapter_command 段触发。外部插件应使用
        ``adapter_api.send_adapter_command`` 或 ``MCBridgeService``。

        Args:
            method: RPC 方法名（如 ping/get_status/send_chat）。
            args: 调用参数。
            timeout: 超时时间（秒），留空则使用配置默认值。

        Returns:
            bridge 返回的 result 字段。

        Raises:
            RuntimeError: WebSocket 未连接。
            asyncio.TimeoutError: 超时未收到响应。
            Exception: bridge 返回 success=false 时携带 error.message。
        """
        if not self._ws:
            raise RuntimeError("WebSocket 未连接，无法调用 bridge RPC")

        rpc_id = uuid.uuid4().hex
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._rpc_pool[rpc_id] = future

        payload = orjson.dumps(
            {
                "type": "rpc",
                "id": rpc_id,
                "method": method,
                "args": args or {},
            }
        ).decode()

        wait_timeout = timeout if timeout is not None else self._rpc_default_timeout
        try:
            await asyncio.wait_for(self._ws.send(payload), timeout=wait_timeout)
            response = await asyncio.wait_for(future, timeout=wait_timeout)
            return response
        except asyncio.TimeoutError:
            logger.error(f"RPC 调用超时: method={method} id={rpc_id}")
            raise
        finally:
            self._rpc_pool.pop(rpc_id, None)

    def _resolve_rpc(self, raw: dict[str, Any]) -> None:
        """将 bridge 的 rpc_response 派发给等待中的 Future。"""
        rpc_id = str(raw.get("id", ""))
        if not rpc_id or rpc_id not in self._rpc_pool:
            logger.warning(f"收到未匹配的 rpc_response: id={rpc_id}")
            return
        future = self._rpc_pool[rpc_id]
        if future.done():
            return
        success = bool(raw.get("success", False))
        if success:
            future.set_result(raw.get("result"))
        else:
            err = raw.get("error") or {}
            future.set_exception(
                RuntimeError(
                    f"bridge rpc 失败: code={err.get('code', 'unknown')} "
                    f"message={err.get('message', '')}"
                )
            )

    def is_bot_online(self) -> bool:
        """Bot 是否已加入游戏（用于 health_check 与状态查询）。"""
        return self._bot_online

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def _get_config(self) -> MCAdapterConfig | None:
        """获取插件配置实例。"""
        if not self.plugin or not self.plugin.config:
            return None
        return cast(MCAdapterConfig, self.plugin.config)

    def _server_group_info(self) -> tuple[str, str]:
        """返回 MC 服务器对应的群聊标识。

        将整服视作一个"群"：group_id 形如 ``mc_<host>_<port>``，
        group_name 形如 ``<host>:<port>``。若 self_connected 尚未提供
        服务器地址，则返回空串以提示调用方不要设置 group_info。
        """
        if not self._server_host:
            return "", ""
        port = self._server_port or 25565
        group_id = f"mc_{self._server_host}_{port}"
        group_name = f"{self._server_host}:{port}"
        return group_id, group_name

    async def get_bot_info(self) -> dict[str, Any]:  # type: ignore[override]
        """获取 Bot 信息。"""
        config = self._get_config()
        username = config.bot.username if config else "MCBot"
        return {
            "bot_id": username,
            "bot_name": config.bot.nickname if config else username,
            "platform": self.platform,
        }

    async def health_check(self) -> bool:  # type: ignore[override]
        """健康检查：WebSocket 连接存活且 bot 在线。"""
        return self.is_connected() and self._bot_online
