"""MC Adapter 插件入口。

注册 MCAdapter（适配器组件）与 MCBridgeService（服务组件）：

- MCAdapter：通过 WebSocket 连接 Node.js bridge，将 MC 聊天桥接进核心。
- MCBridgeService：封装 adapter_command 通道，对外暴露 send_chat/get_status/
  ping/call_rpc/follow_player，为 Tool/Action 调用 bridge 能力提供稳定接口。

工具组件（FollowPlayerTool 等）已迁移到独立插件 ``mc_tools``，
本插件仅保留 adapter + service 两个核心组件。
``MCBridgeService`` 的具名方法始终可被代码直接调用，不受工具开关影响。
"""

from __future__ import annotations

from typing import cast

from src.app.plugin_system.base import BasePlugin, register_plugin
from src.kernel.logger import get_logger

from .adapter import MCAdapter
from .bridge_manager import MCBridgeManager, set_bridge_manager
from .bridge_service import MCBridgeService
from .config import MCAdapterConfig

logger = get_logger("mc_adapter")


@register_plugin
class MCAdapterPlugin(BasePlugin):
    """Minecraft mineflayer bridge 适配器插件。"""

    plugin_name: str = "mc_adapter"
    plugin_description: str = (
        "基于 mineflayer bridge 的 Minecraft 平台适配器，支持加入游戏与聊天收发，"
        "并通过 MCBridgeService 暴露 RPC 通道供工具插件（如 mc_tools）调用。"
    )
    plugin_version: str = "1.0.0"
    plugin_author: str = "MoFox Team"

    configs: list[type] = [MCAdapterConfig]
    dependent_components: list[str] = []

    def get_components(self) -> list[type]:
        """返回插件组件类。

        - 适配器与服务受 ``plugin.enabled`` 控制：禁用时整个插件不提供组件。
        - 工具（FollowPlayerTool 等）已迁移到独立插件 ``mc_tools``，
          本插件不再注册任何 BaseTool 子类。
        - ``MCBridgeService`` 的具名方法（send_chat/get_status/follow_player 等）
          始终可被代码直接调用，bridge 端 RPC 也总是注册——开关仅决定是否
          以 BaseTool 形式暴露给 LLM（由 mc_tools 插件负责）。
        """
        components: list[type] = []
        config = cast(MCAdapterConfig, self.config) if self.config else None
        if config is None or config.plugin.enabled:
            components.append(MCAdapter)
            components.append(MCBridgeService)
        return components

    async def on_plugin_loaded(self) -> None:
        """插件加载完成。

        初始化全局 MCBridgeManager 单例，供 mc_tools 的 MCSessionTool
        跨插件调用（join/leave/status 进出服动作）。
        """
        set_bridge_manager(MCBridgeManager(self))
        logger.info("mc_adapter 插件已加载（adapter=mc_adapter, service=mc_bridge, bridge_manager=已初始化）")

    async def on_plugin_unloaded(self) -> None:
        """插件卸载。

        清理全局 bridge 管理器单例，避免引用已卸载的插件实例。
        """
        # 若 bridge 进程仍由本管理器持有，尝试停止
        try:
            from .bridge_manager import get_bridge_manager

            manager = get_bridge_manager()
            if manager.is_process_running():
                await manager.stop_bridge()
        except Exception:  # noqa: BLE001
            pass
        set_bridge_manager(None)
        logger.info("mc_adapter 插件已卸载")

