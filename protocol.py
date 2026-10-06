"""MC Bridge 协议常量与辅助函数。

定义 Python 适配器与 Node.js bridge 之间 JSON 文本帧的消息类型常量，
并提供构造入站/出站消息字典的辅助函数。

协议类型层级：
- 出站（Python -> Bridge）：SendChat / RpcRequest
- 入站（Bridge -> Python）：SelfConnected / SelfDisconnected / SelfDied /
  SelfRespawned / Chat / Whisper / SystemMessage / Error / RpcResponse

新增字段或消息类型时，优先在常量与 TypedDict 中同步更新，
避免散落在适配器代码中。
"""

from __future__ import annotations

from typing import Any, NotRequired, Required, TypedDict

# ---- 出站消息类型常量 ----

OUTGOING_TYPE_SEND_CHAT = "send_chat"
OUTGOING_TYPE_RPC = "rpc"


# ---- 入站消息类型常量 ----

INCOMING_TYPE_SELF_CONNECTED = "self_connected"
INCOMING_TYPE_SELF_DISCONNECTED = "self_disconnected"
INCOMING_TYPE_SELF_DIED = "self_died"
INCOMING_TYPE_SELF_RESPAWNED = "self_respawned"
INCOMING_TYPE_CHAT = "chat"
INCOMING_TYPE_WHISPER = "whisper"
INCOMING_TYPE_SYSTEM_MESSAGE = "system_message"
INCOMING_TYPE_ERROR = "error"
INCOMING_TYPE_RPC_RESPONSE = "rpc_response"


class SendChatPayload(TypedDict, total=False):
    """Python 下发：发送公共聊天。"""

    type: Required[str]
    message: Required[str]


class RpcRequestPayload(TypedDict, total=False):
    """Python 下发：调用 bridge 暴露的 RPC 方法。

    id 由调用方生成（建议 uuid4），bridge 会以相同 id 回送 RpcResponse。
    """

    type: Required[str]
    id: Required[str]
    method: Required[str]
    args: NotRequired[dict[str, Any]]


class SelfConnectedPayload(TypedDict, total=False):
    """Bridge -> Python：bot 成功加入游戏（spawn）。"""

    type: Required[str]
    username: Required[str]
    uuid: Required[str]
    host: NotRequired[str]
    port: NotRequired[int]
    timestamp: NotRequired[int]


class SelfDisconnectedPayload(TypedDict, total=False):
    """Bridge -> Python：bot 断开。"""

    type: Required[str]
    reason: Required[str]
    timestamp: NotRequired[int]


class SelfDiedPayload(TypedDict, total=False):
    """Bridge -> Python：bot 死亡。

    bot 仍连接在 MC 服务器，只是处于死亡状态，等待重生或处于死亡屏幕。
    不应将此事件视为断线。

    ``killer`` / ``death_message`` / ``cause`` 由 bridge 通过监听 MC 服务器的
    死亡消息（``translate`` 前缀为 ``death.attack.*`` 的系统消息）填充。
    若服务器未发送死亡消息，或在超时窗口内未收到，这三个字段为空字符串。
    """

    type: Required[str]
    username: Required[str]
    uuid: Required[str]
    timestamp: NotRequired[int]
    killer: NotRequired[str]
    death_message: NotRequired[str]
    cause: NotRequired[str]


class SelfRespawnedPayload(TypedDict, total=False):
    """Bridge -> Python：bot 死亡后重生。

    mineflayer 的 spawn 事件在首次加入与重生时都会触发；
    bridge 区分两者，重生走此 payload 而非 self_connected。
    """

    type: Required[str]
    username: Required[str]
    uuid: Required[str]
    timestamp: NotRequired[int]


class ChatPayload(TypedDict, total=False):
    """Bridge -> Python：玩家公共聊天。"""

    type: Required[str]
    username: Required[str]
    uuid: Required[str]
    message: Required[str]
    timestamp: NotRequired[int]


class WhisperPayload(TypedDict, total=False):
    """Bridge -> Python：玩家私聊（whisper）给 Bot。"""

    type: Required[str]
    from_: Required[str]
    to: Required[str]
    uuid: Required[str]
    message: Required[str]
    timestamp: NotRequired[int]


class SystemMessagePayload(TypedDict, total=False):
    """Bridge -> Python：系统消息（玩家加入/离开等）。"""

    type: Required[str]
    message: Required[str]
    raw_kind: NotRequired[str]
    raw: NotRequired[dict[str, Any]]
    timestamp: NotRequired[int]


class ErrorPayload(TypedDict, total=False):
    """Bridge -> Python：bridge 级错误。"""

    type: Required[str]
    code: Required[str]
    message: Required[str]
    timestamp: NotRequired[int]
    loggedIn: NotRequired[bool]


class RpcResponsePayload(TypedDict, total=False):
    """Bridge -> Python：RPC 响应。"""

    type: Required[str]
    id: Required[str]
    success: Required[bool]
    result: NotRequired[Any]
    error: NotRequired[dict[str, Any]]


def build_send_chat(message: str) -> SendChatPayload:
    """构造发送聊天消息的出站 payload。"""
    return {"type": OUTGOING_TYPE_SEND_CHAT, "message": message}


def build_rpc_request(rpc_id: str, method: str, args: dict[str, Any] | None = None) -> RpcRequestPayload:
    """构造 RPC 请求 payload。"""
    payload: RpcRequestPayload = {"type": OUTGOING_TYPE_RPC, "id": rpc_id, "method": method}
    if args:
        payload["args"] = args
    return payload
