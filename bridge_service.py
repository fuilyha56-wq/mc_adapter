"""MC Bridge Service - 对外暴露 bridge 的 RPC 能力。

Service 设计目的：
- 将 bridge 的 RPC 调用包装为稳定的 Python 方法，供其他插件/组件直接调用。
- 为未来工具调用（Tool/Action）提供统一入口：Tool 只需调用本 Service 的方法，
  无需关心 WebSocket 与 RPC 协议细节。

调用路径：
- 外部插件 → MCBridgeService.send_chat/get_status/ping/call_rpc
- → adapter_api.send_adapter_command(adapter_sign, action, params, timeout)
- → AdapterManager 构造 adapter_command 信封 → adapter._send_platform_message
- → MCAdapter._handle_adapter_command → bridge rpc → rpc_response
- → adapter_response 信封回送核心 → 解除 send_adapter_command 的 Future

返回值遵循 AdapterManager 的响应字典约定：
- ``{"status": "ok", "data": ..., "message": ""}``
- ``{"status": "failed", "message": err}``（业务失败）
- ``{"status": "error", "message": err}``（异常/超时）

设计约定：
- Service 不缓存 adapter 引用（ServiceManager 每次返回新实例），
  而是在调用时通过 adapter_api.send_adapter_command 按签名查找当前活跃的 adapter。
- 若 adapter 未启动，AdapterManager 会返回 ``{"status": "error", ...}``，
  Service 不再做额外校验，直接把结果交给调用方。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api import adapter_api
from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base import BaseService

if TYPE_CHECKING:
    from .plugin import MCAdapterPlugin

logger = get_logger("mc_adapter")

#: 本插件 adapter 的组件签名，用于在 adapter_api 中查找实例
_ADAPTER_SIGNATURE = "mc_adapter:adapter:mc_adapter"


class MCBridgeService(BaseService):
    """Minecraft bridge RPC 服务。

    通过 ``adapter_api.send_adapter_command`` 调用当前活跃的 MCAdapter，
    进而调用 Node.js bridge 暴露的 RPC 方法。
    所有方法均为 async，返回 AdapterManager 风格的响应字典。
    """

    name = "mc_bridge"
    description = (
        "Minecraft bridge RPC 服务，封装 send_adapter_command 通道，"
        "暴露 send_chat/get_status/ping/call_rpc 等方法供其他组件调用。"
    )
    version = "1.0.0"

    def __init__(self, plugin: "MCAdapterPlugin") -> None:
        """初始化服务。

        Args:
            plugin: 所属插件实例。
        """
        super().__init__(plugin)

    async def send_chat(self, message: str, timeout: float = 20.0) -> dict[str, Any]:
        """通过 adapter_command 通道发送公共聊天。

        与适配器普通出站路径（_send_chat_envelope）等价，但走显式 RPC 并
        等待 bridge 确认，适用于 Tool/Action 主动触发聊天且需要结果反馈的场景。

        Args:
            message: 要发送的聊天文本。
            timeout: 超时时间（秒）。

        Returns:
            AdapterManager 风格响应字典，成功时 ``data`` 通常为 ``{"sent": true}``。
        """
        return await adapter_api.send_adapter_command(
            _ADAPTER_SIGNATURE,
            "send_chat",
            {"message": message},
            timeout=timeout,
        )

    async def get_status(self, timeout: float = 20.0) -> dict[str, Any]:
        """获取 bot 当前状态。

        Returns:
            响应字典，成功时 ``data`` 包含 online/username/uuid/host/port/
            health/food/saturation/level/position/dimension/game_mode。
        """
        return await adapter_api.send_adapter_command(
            _ADAPTER_SIGNATURE,
            "get_status",
            {},
            timeout=timeout,
        )

    async def ping(self, timeout: float = 10.0) -> dict[str, Any]:
        """检查 bridge 连通性。

        Returns:
            响应字典，成功时 ``data`` 通常为
            ``{"pong": true, "username": ..., "online": ...}``。
        """
        return await adapter_api.send_adapter_command(
            _ADAPTER_SIGNATURE,
            "ping",
            {},
            timeout=timeout,
        )

    async def call_rpc(
        self,
        method: str,
        args: dict[str, Any] | None = None,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        """通用 RPC 调用逃生舱。

        当未来 bridge 新增 RPC 方法但本 Service 尚未封装时，可直通调用。
        新方法稳定后建议在 Service 中补一个具名方法，便于类型与文档约束。

        Args:
            method: bridge HANDLERS 中注册的方法名。
            args: 调用参数。
            timeout: 超时时间（秒）。

        Returns:
            AdapterManager 风格响应字典。
        """
        return await adapter_api.send_adapter_command(
            _ADAPTER_SIGNATURE,
            method,
            args or {},
            timeout=timeout,
        )

    async def follow_player(
        self,
        args: dict[str, Any] | None = None,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        """控制 bot 跟随指定玩家。

        调用 bridge 端 ``follow_player`` RPC（由 ``server/src/tools/follow_player.ts``
        实现，底层为 mineflayer-pathfinder 的 ``GoalFollow`` 动态目标）。

        bridge 端 RPC 总是注册，本方法直接转发调用，不受 ``tools`` 配置开关
        影响——开关仅控制是否以 BaseTool 形式暴露给 LLM。BaseTool 子类
        ``FollowPlayerTool`` 已迁移到独立插件 ``mc_tools``，调用本方法时
        无需依赖 mc_tools 是否加载。

        Args:
            args: 操作参数字典，结构为::

                {"action": "start" | "stop" | "status",
                 "username": str,  # action='start' 时必需
                 "range":    int}  # action='start' 时可选，默认 1，范围 [1, 8]

            timeout: 超时时间（秒），start 通常立即返回，留 20s 余量。

        Returns:
            AdapterManager 风格响应字典，``data`` 形如::

                # start
                {"following": true, "target": "<username>", "range": <n>}
                # stop
                {"following": false, "target": "<username|null>", "reason": "<str>"}
                # status
                {"following": <bool>, "target": "<username|null>",
                 "range": <n>, "last_reason": "<str>", "updated_at": <ms>}
        """
        return await adapter_api.send_adapter_command(
            _ADAPTER_SIGNATURE,
            "follow_player",
            args or {},
            timeout=timeout,
        )

