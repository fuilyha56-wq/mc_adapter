# MC Adapter (客户端 / Neo-MoFox 插件)

Neo-MoFox 平台适配器，通过 WebSocket 连接 [mineflayer bridge](../server/README.md)
进程，将 Minecraft 聊天桥接进 Neo-MoFox 核心，并暴露 `MCBridgeService` 供
[mc_tools 插件](../tools_plugin/README.md) 调用。

## 架构

```
┌──────────────────────┐   WebSocket    ┌──────────────────┐   MC 协议   ┌─────────────┐
│  Neo-MoFox 核心      │ <───────────>  │  mc_bridge       │ <────────> │  MC 服务器  │
│  └ mc_adapter 插件   │   JSON 文本帧  │  (Node.js+mineflayer) │        │             │
└──────────────────────┘                └──────────────────┘            └─────────────┘
```

## 提供的组件

| 类型     | 名称          | 作用                                                         |
| -------- | ------------- | ------------------------------------------------------------ |
| adapter  | `mc_adapter`  | Minecraft 平台适配器，桥接 chat/whisper/system_message 事件  |
| service  | `mc_bridge`   | RPC 服务，封装 `send_adapter_command` 通道，对外暴露稳定方法 |

工具组件（FollowPlayerTool / GetStatusTool 等）已迁移到独立插件
[mc_tools](../tools_plugin/README.md)，本插件不再注册任何 BaseTool 子类。

## 安装

本插件通过符号链接方式部署到 Neo-MoFox 插件目录：

```bash
# 从 Neo-MoFox 根目录
ln -s /path/to/mc_adapter/client plugins/mc_adapter
```

插件依赖 `orjson`（已在 Neo-MoFox 主依赖中），无需额外安装。

## 配置

配置文件位于 `config/plugins/mc_adapter/config.toml`，首次加载时自动生成。
关键配置项：

```toml
[plugin]
enabled = true

[bot]
username = "MoFox_Bot"     # 须与 bridge config.minecraft.username 一致
nickname = "MoFox_Bot"

[bridge]
host = "127.0.0.1"          # bridge WebSocket 地址
port = 8097                  # bridge WebSocket 端口
path = "/mc"                 # bridge WebSocket 路径
secret = ""                  # 鉴权令牌（须与 bridge 一致，留空不鉴权）
reconnect_interval = 5.0     # WebSocket 重连间隔（秒）
rpc_timeout = 30.0           # adapter_command 调用默认超时（秒）

[features]
forward_system_messages = true   # 转发玩家加入/离开为系统消息
forward_whisper_as_chat = true   # 私聊作为普通聊天处理
forward_death_messages = true    # 转发 bot 死亡事件为聊天流
```

工具开关已迁移到 `mc_tools` 插件配置（`config/plugins/mc_tools/config.toml`）。

## 调用 bridge RPC

### 方式 1：通过 MCBridgeService（推荐，类型友好）

```python
from src.app.plugin_system.api import service_api

service = service_api.get_service("mc_adapter:service:mc_bridge")
result = await service.send_chat("Hello from Neo-MoFox!")
status = await service.get_status()
```

### 方式 2：通过 adapter_api.send_adapter_command（框架通用通道）

```python
from src.app.plugin_system.api import adapter_api

result = await adapter_api.send_adapter_command(
    "mc_adapter:adapter:mc_adapter",
    "send_chat",
    {"message": "Hello!"},
    timeout=20.0,
)
# result: {"status": "ok", "data": {"sent": true}, "message": ""}
```

### Bridge 暴露的 RPC 方法

| method            | args                          | 返回 result                                          |
| ----------------- | ----------------------------- | ---------------------------------------------------- |
| `ping`            | `{}`                          | `{pong: true, username, online}`                     |
| `get_status`      | `{}`                          | `{online, username, uuid, host, port, health, ...}` |
| `send_chat`       | `{message: string}`           | `{sent: true}`                                       |
| `follow_player`   | `{action, username?, range?}` | `{following, target, range, ...}`                   |
| `view_inventory`  | `{}`                          | `{held_item, armor, inventory, free_slots, ...}`    |
| `get_surroundings`| `{radius?, block_filter?, entity_filter?}` | `{center, radius, blocks[], entities[]}` |
| `break_block`     | `{action, x?, y?, z?, force_look?}` | `{broken, position, name, dig_time_ms}`       |
| `goto_position`   | `{action, x?, y?, z?, range?, mode?}` | `{going, target, range, mode, ...}`          |
| `craft_item`      | `{action, item?, count?, crafting_table?, radius?}` | `{crafted, item_id, count, used_table, ...}` |

新增方法：在 `server/src/tools/<name>.ts` 中实现处理器，在
`server/src/tools/index.ts` 的 `TOOL_HANDLERS` 表中登记，无需改 Python 侧。

