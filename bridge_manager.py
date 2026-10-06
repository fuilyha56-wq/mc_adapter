"""mc-bridge 进程管理器。

负责 mc-bridge（Node.js + mineflayer）进程的启动、停止与状态检测。

背景：mc_adapter 插件作为 WebSocket 客户端连接 bridge，但 bridge 本身是
独立进程（``npm start``）。本模块让 bot 在聊天里说「跟我一起玩 MC」时，
能自动拉起 bridge 进程、等待 bot 进服；说「不玩了」时优雅停止。

设计：
- 进程以 detached + 新进程组方式启动（Windows），后台常驻，不阻塞事件循环。
- 启动后通过 ``ping`` RPC 轮询确认 bridge 已就绪，再通过 ``get_status``
  确认 bot 已进服（online=true）。
- 停止时优先让 bot 退出（bridge 的 quit 语义），再终止进程树。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api import adapter_api
from src.app.plugin_system.api.log_api import get_logger

if TYPE_CHECKING:
    from .plugin import MCAdapterPlugin

logger = get_logger("mc_bridge_manager")

# Windows 进程标志：脱离父进程控制台 + 新进程组（可独立终止）
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200

#: 本插件 adapter 的组件签名
_ADAPTER_SIGNATURE = "mc_adapter:adapter:mc_adapter"


class MCBridgeManager:
    """mc-bridge 进程生命周期管理。

    Attributes:
        _plugin: 所属插件实例（用于读取配置）。
        _process: 当前 bridge 子进程（None 表示未启动/已停止）。
    """

    def __init__(self, plugin: "MCAdapterPlugin") -> None:
        """初始化进程管理器。"""
        self._plugin = plugin
        self._process: asyncio.subprocess.Process | None = None

    # ------------------------------------------------------------------
    # 路径解析
    # ------------------------------------------------------------------

    def _bridge_dir(self) -> Path:
        """解析 mc-bridge 项目目录（含 package.json / dist）。

        优先取配置 bridge.process_dir；未配置时按约定探测：
        1. 插件目录下的 server/（当前适配器结构）
        2. 插件仓库目录的兄弟目录 mc-bridge

        Returns:
            bridge 项目根目录路径。
        """
        config = self._plugin.config
        configured = getattr(getattr(config, "bridge", None), "process_dir", "")
        if configured:
            p = Path(configured).expanduser()
            if p.is_dir():
                return p

        # 当前适配器结构：plugins/mofox_mc_adapter/server/
        plugin_dir = Path(__file__).resolve().parent.parent
        candidates = [
            plugin_dir / "server",  # 内置 server 目录
            plugin_dir.parent / "mc-bridge",  # 兄弟目录 mc-bridge
        ]
        for cand in candidates:
            if (cand / "package.json").is_file():
                return cand
        return candidates[0]

    # ------------------------------------------------------------------
    # 状态检测
    # ------------------------------------------------------------------

    def is_process_running(self) -> bool:
        """检查 bridge 子进程是否还在运行。"""
        return self._process is not None and self._process.returncode is None

    async def ping_bridge(self) -> bool:
        """通过 bridge RPC ping 检查服务是否就绪。

        Returns:
            bridge 是否响应 ping 且 pong=true。
        """
        try:
            result = await adapter_api.send_adapter_command(
                _ADAPTER_SIGNATURE,
                "ping",
                {},
                timeout=5.0,
            )
            return result.get("status") == "ok" and bool(
                result.get("data", {}).get("pong")
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"bridge ping 失败: {exc}")
            return False

    async def get_bridge_status(self) -> dict[str, Any]:
        """获取 bridge 完整状态（进程 + bot 在线情况）。

        Returns:
            状态字典，含 bridge_process_running / bridge_responding /
            bot_online / username / server / position / health 等字段。
        """
        proc_running = self.is_process_running()
        try:
            ping_result = await adapter_api.send_adapter_command(
                _ADAPTER_SIGNATURE,
                "ping",
                {},
                timeout=5.0,
            )
            ping_ok = ping_result.get("status") == "ok"
        except Exception:  # noqa: BLE001
            ping_ok = False

        status: dict[str, Any] = {
            "bridge_process_running": proc_running,
            "bridge_responding": ping_ok,
        }
        if ping_ok:
            try:
                sresult = await adapter_api.send_adapter_command(
                    _ADAPTER_SIGNATURE,
                    "get_status",
                    {},
                    timeout=5.0,
                )
                if sresult.get("status") == "ok" and isinstance(
                    sresult.get("data"), dict
                ):
                    data = sresult["data"]
                    status["bot_online"] = bool(data.get("online"))
                    status["username"] = data.get("username", "")
                    status["server"] = (
                        f"{data.get('host')}:{data.get('port')}"
                        if data.get("host")
                        else ""
                    )
                    status["position"] = data.get("position")
                    status["health"] = data.get("health")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"获取 bridge 状态失败: {exc}")
        else:
            status["bot_online"] = False

        return status

    # ------------------------------------------------------------------
    # 启动 / 停止
    # ------------------------------------------------------------------

    async def start_bridge(self, wait_seconds: float = 30.0) -> tuple[bool, str]:
        """启动 mc-bridge 进程并等待就绪。

        Args:
            wait_seconds: 等待 bridge 就绪的最长时间（秒）。

        Returns:
            (是否成功, 状态消息)。
        """
        if self.is_process_running():
            # 进程在跑，检查是否已就绪
            if await self.ping_bridge():
                return True, "mc-bridge 已在运行"
            return False, "mc-bridge 进程存在但未就绪（可能正在启动），请稍候重试"

        # 检查 bridge 是否已由外部启动（进程在跑但不由本管理器管理）
        if await self.ping_bridge():
            return True, "mc-bridge 已由外部启动并就绪"

        bridge_dir = self._bridge_dir()
        if not (bridge_dir / "package.json").is_file():
            return False, f"未找到 mc-bridge 项目目录: {bridge_dir}"
        if not (bridge_dir / "dist" / "index.js").is_file():
            return False, (
                f"mc-bridge 未构建（缺少 dist/index.js）。请先在 {bridge_dir} 运行 "
                "npm install && npm run build"
            )

        # 启动命令：npm start（Windows 用 npm.cmd）
        npm = "npm.cmd" if sys.platform == "win32" else "npm"
        creationflags = 0
        if sys.platform == "win32":
            creationflags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP

        logger.info(f"启动 mc-bridge: {npm} start (cwd={bridge_dir})")
        try:
            self._process = await asyncio.create_subprocess_exec(
                npm,
                "start",
                cwd=str(bridge_dir),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=creationflags,
                env={**os.environ, "NODE_ENV": "production"},
            )
        except FileNotFoundError:
            return False, f"找不到 npm 可执行文件: {npm}（请确认已安装 Node.js）"
        except Exception as exc:  # noqa: BLE001
            return False, f"启动 mc-bridge 失败: {exc}"

        # 进程立即退出 = 启动失败
        try:
            await asyncio.wait_for(self._process.wait(), timeout=1.5)
            return False, f"mc-bridge 启动后立即退出，退出码: {self._process.returncode}"
        except asyncio.TimeoutError:
            pass  # 1.5s 后仍在跑 = 正常

        # 主动重启适配器，重新建立 WebSocket 连接
        # （传输层重连有上限，bridge 重新启动后需手动拉起连接）
        try:
            await adapter_api.restart_adapter(_ADAPTER_SIGNATURE)
            logger.info("已主动重启 mc_adapter，等待重新连接 bridge")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"重启 mc_adapter 失败: {exc}")

        # 轮询等待 bridge 就绪（ping 通）
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while asyncio.get_running_loop().time() < deadline:
            if await self.ping_bridge():
                return True, "mc-bridge 已启动并连接成功"
            await asyncio.sleep(2.0)

        return False, "mc-bridge 进程已启动，但 30 秒内未就绪（检查 bridge 日志 / MC 服务器配置）"

    async def wait_bot_online(self, wait_seconds: float = 30.0) -> tuple[bool, str]:
        """等待 bot 进服（get_status.online=true）。

        Args:
            wait_seconds: 等待进服的最长时间（秒）。

        Returns:
            (是否在线, 状态消息)。
        """
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while asyncio.get_running_loop().time() < deadline:
            status = await self.get_bridge_status()
            if status.get("bot_online"):
                server = status.get("server", "")
                username = status.get("username", "")
                return True, (
                    f"已进入游戏: {username}"
                    + (f" @ {server}" if server else "")
                )
            await asyncio.sleep(2.0)
        return False, "等待 bot 进服超时（30 秒），请检查 MC 服务器是否可连接、认证是否通过"

    async def stop_bridge(self) -> tuple[bool, str]:
        """停止 mc-bridge 进程。

        先尝试让 bot 优雅退出（bridge 的 quit 语义），再终止进程树。

        Returns:
            (是否成功, 状态消息)。
        """
        # 先尝试通过 RPC 让 bot 退出游戏（bridge 会自检退出）
        try:
            await adapter_api.send_adapter_command(
                _ADAPTER_SIGNATURE,
                "quit",
                {},
                timeout=3.0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"通过 RPC 让 bridge 退出失败: {exc}")

        # 终止进程树
        proc = self._process
        if proc and proc.returncode is None:
            try:
                if sys.platform == "win32":
                    # taskkill /T 终止进程树（含 npm 子进程 node）
                    kill = await asyncio.create_subprocess_exec(
                        "taskkill", "/F", "/T", "/PID", str(proc.pid),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await kill.wait()
                else:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
            except Exception as exc:  # noqa: BLE001
                return False, f"停止 mc-bridge 进程失败: {exc}"

        self._process = None
        return True, "mc-bridge 已停止"


# 全局单例（按插件实例维度管理）
_global_bridge_manager: MCBridgeManager | None = None


def get_bridge_manager() -> MCBridgeManager:
    """获取全局 bridge 进程管理器（由插件加载时初始化）。

    Raises:
        RuntimeError: 管理器尚未初始化（mc_adapter 插件未加载）。
    """
    if _global_bridge_manager is None:
        raise RuntimeError("MCBridgeManager 尚未初始化（mc_adapter 插件未加载？）")
    return _global_bridge_manager


def set_bridge_manager(manager: MCBridgeManager | None) -> None:
    """设置全局 bridge 进程管理器（插件加载时调用）。

    Args:
        manager: 要注册的全局管理器实例；传 None 用于插件卸载时清理。
    """
    global _global_bridge_manager
    _global_bridge_manager = manager
