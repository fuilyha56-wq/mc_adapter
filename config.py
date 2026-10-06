"""MC Adapter 配置定义。

配置分四个节：
- plugin：适配器启用开关与配置版本。
- bot：Bot 在 MC 中的身份信息（用户名/昵称），用于核心 bot_info 与消息 sender。
- bridge：与 Node.js bridge 进程的 WebSocket 连接信息。
- features：v1 行为开关，后续可扩展（如系统消息是否注入到聊天流）。

工具开关已迁移到独立插件 ``mc_tools`` 的配置（``config/plugins/mc_tools/``），
本插件仅保留 adapter + service 两个核心组件，不再持有任何 BaseTool 子类。
``MCBridgeService`` 的具名方法（send_chat/get_status/follow_player 等）始终可
被代码直接调用，bridge 端 RPC 也总是注册——开关仅决定是否以 BaseTool 形式
暴露给 LLM（由 mc_tools 插件负责）。
"""

from __future__ import annotations

from typing import ClassVar

from src.core.components.base.config import BaseConfig, Field, SectionBase, config_section


class MCAdapterConfig(BaseConfig):
    """Minecraft 适配器配置。"""

    name: ClassVar[str] = "config"
    description: ClassVar[str] = "Minecraft mineflayer bridge 适配器配置"

    @config_section("plugin", title="插件设置", tag="plugin")
    class PluginSection(SectionBase):
        """插件基本配置。"""

        enabled: bool = Field(
            default=True,
            description="是否启用 MC 适配器",
            label="启用适配器",
            tag="plugin",
        )
        config_version: str = Field(
            default="1.0.0",
            description="配置文件版本",
            label="配置版本",
            disabled=True,
            tag="general",
        )

    @config_section("bot", title="Bot 配置", tag="user")
    class BotSection(SectionBase):
        """Bot 身份配置。

        username 应与 bridge 端 config.minecraft.username 保持一致，
        否则核心 bot_info 与实际进服账号不符。
        """

        username: str = Field(
            default="MoFox_Bot",
            description="Bot 在 MC 中的用户名（须与 bridge 配置一致）",
            label="Bot 用户名",
            placeholder="MoFox_Bot",
            tag="user",
        )
        nickname: str = Field(
            default="MoFox_Bot",
            description="Bot 的展示昵称，用于消息 sender_name",
            label="Bot 昵称",
            placeholder="MoFox_Bot",
            tag="user",
        )

    @config_section("bridge", title="Bridge 连接", tag="network")
    class BridgeSection(SectionBase):
        """与 Node.js bridge 的 WebSocket 连接配置。"""

        host: str = Field(
            default="127.0.0.1",
            description="bridge WebSocket 监听地址",
            label="Bridge 地址",
            placeholder="127.0.0.1",
            tag="network",
        )
        port: int = Field(
            default=8097,
            description="bridge WebSocket 监听端口",
            label="Bridge 端口",
            ge=1,
            le=65535,
            tag="network",
        )
        path: str = Field(
            default="/mc",
            description="bridge WebSocket 路径（须与 bridge 配置一致，留空则不限制）",
            label="WebSocket 路径",
            placeholder="/mc",
            tag="network",
        )
        secret: str = Field(
            default="",
            description="bridge 鉴权令牌（可选，留空表示不鉴权）",
            label="鉴权令牌",
            input_type="password",
            placeholder="可选，留空表示不鉴权",
            tag="security",
        )
        reconnect_interval: float = Field(
            default=5.0,
            description="WebSocket 断线重连间隔（秒）",
            label="重连间隔",
            ge=1.0,
            le=60.0,
            step=1.0,
            input_type="slider",
            tag="network",
        )
        max_reconnect_attempts: int = Field(
            default=0,
            description=(
                "WebSocket 最大重连次数，0 表示无限重连。"
                "bridge 进程被外部重启后，适配器会主动重新拉起连接"
            ),
            label="最大重连次数",
            ge=0,
            le=100,
            step=1,
            tag="network",
        )
        process_dir: str = Field(
            default="",
            description=(
                "mc-bridge 项目目录（含 package.json / dist）。"
                "留空时自动探测插件目录下的 server/ 或兄弟目录 mc-bridge"
            ),
            label="Bridge 项目目录",
            placeholder="留空自动探测",
            tag="network",
        )
        rpc_timeout: float = Field(
            default=30.0,
            description="调用 bridge RPC 的默认超时时间（秒）",
            label="RPC 超时",
            ge=1.0,
            le=120.0,
            step=1.0,
            input_type="slider",
            tag="network",
        )

    @config_section("features", title="功能特性", tag="general")
    class FeaturesSection(SectionBase):
        """v1 功能开关。"""

        forward_system_messages: bool = Field(
            default=True,
            description="是否将玩家加入/离开等系统消息作为聊天上下文注入核心",
            label="转发系统消息",
            tag="general",
        )
        forward_whisper_as_chat: bool = Field(
            default=True,
            description="是否将私聊（whisper）作为普通聊天处理",
            label="私聊作为聊天",
            tag="general",
        )
        forward_death_messages: bool = Field(
            default=True,
            description=(
                "是否将 bot 死亡事件转化为文字（如 '你被xxx杀死了'）注入聊天流，"
                "让核心/AI 感知 bot 死亡；关闭后仅触发事件，不注入聊天流"
            ),
            label="转发死亡消息",
            tag="general",
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    bot: BotSection = Field(default_factory=BotSection)
    bridge: BridgeSection = Field(default_factory=BridgeSection)
    features: FeaturesSection = Field(default_factory=FeaturesSection)

