"""QQ 官方机器人底层接入。

AstrBot 的 qq_official 适配器基于 qq-botpy，而 qq-botpy 1.2.1
没有订阅群成员事件（GROUP_MEMBER_EVENT，1<<24），也没有
GROUP_JOIN_REQUEST / GROUP_MEMBER_ADD / GROUP_MEMBER_REMOVE 的解析器。

本模块在插件加载时（平台适配器实例化之前）对 botpy 做最小补丁：
1. 构造 Client 时追加群成员事件 intent；
2. 为 ConnectionState 增加上述三个事件的解析器；
3. 拦截 ws_dispatch，把事件转交给插件回调。

补丁只在缺少对应能力时生效，若未来 AstrBot/qq-botpy 原生支持这些事件，
插件会自动让位给原生实现。
"""

from __future__ import annotations

import asyncio
import random
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from astrbot.api import logger

try:
    from botpy.client import Client
    from botpy.connection import ConnectionState
    from botpy.http import Route

except Exception as exc:  # pragma: no cover - 缺少 botpy 时降级
    logger.error("[QQJoinManager] 无法加载 qq-botpy，入群事件监听不可用：%s", exc)
    Client = None  # type: ignore[assignment,misc]
    ConnectionState = None  # type: ignore[assignment,misc]
    Route = None  # type: ignore[assignment,misc]


GROUP_MEMBER_INTENT = 1 << 24
"""GROUP_MEMBER_EVENT：群成员加入/退出、入群申请事件。"""

_EVENT_HANDLERS: dict[str, Callable[[Any, Any], Awaitable[None]]] = {}


def _parse_group_join_request(self: Any, payload: dict[str, Any]) -> None:
    """解析 GROUP_JOIN_REQUEST 并通过 ws_dispatch 分发。"""
    data = payload.get("d") if isinstance(payload, dict) else None
    event = SimpleNamespace(**(data if isinstance(data, dict) else {}))
    self._dispatch("group_join_request", event)


def _parse_group_member_add(self: Any, payload: dict[str, Any]) -> None:
    """解析 GROUP_MEMBER_ADD 并通过 ws_dispatch 分发。"""
    data = payload.get("d") if isinstance(payload, dict) else None
    event = SimpleNamespace(**(data if isinstance(data, dict) else {}))
    self._dispatch("group_member_add", event)


def _parse_group_member_remove(self: Any, payload: dict[str, Any]) -> None:
    """解析 GROUP_MEMBER_REMOVE 并通过 ws_dispatch 分发。"""
    data = payload.get("d") if isinstance(payload, dict) else None
    event = SimpleNamespace(**(data if isinstance(data, dict) else {}))
    self._dispatch("group_member_remove", event)


async def _run_event_handler(
    handler: Callable[[Any, Any], Awaitable[None]],
    client: Any,
    event_name: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    """在独立任务中执行插件事件回调，避免阻塞网关心跳。"""
    payload = args[0] if args else kwargs.get("payload")
    try:
        await handler(client, payload)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("[QQJoinManager] 处理 %s 事件失败", event_name)


def install_patches() -> bool:
    """安装 botpy 补丁。

    Returns:
        是否成功安装（botpy 不可用时返回 False）。
    """
    if Client is None or ConnectionState is None:
        return False

    if not hasattr(ConnectionState, "parse_group_join_request"):
        ConnectionState.parse_group_join_request = _parse_group_join_request  # type: ignore[attr-defined]
    if not hasattr(ConnectionState, "parse_group_member_add"):
        ConnectionState.parse_group_member_add = _parse_group_member_add  # type: ignore[attr-defined]
    if not hasattr(ConnectionState, "parse_group_member_remove"):
        ConnectionState.parse_group_member_remove = _parse_group_member_remove  # type: ignore[attr-defined]

    original_init = Client.__init__
    if not getattr(original_init, "_qq_joinmanager_patched", False):

        def _patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
            intents = kwargs.get("intents")
            if intents is None and args:
                intents = args[0]
            if hasattr(intents, "value"):
                try:
                    intents.value = int(intents.value) | GROUP_MEMBER_INTENT
                except (TypeError, ValueError):
                    pass
            original_init(self, *args, **kwargs)

        _patched_init._qq_joinmanager_patched = True  # type: ignore[attr-defined]
        Client.__init__ = _patched_init  # type: ignore[method-assign]

    original_dispatch = Client.ws_dispatch
    if not getattr(original_dispatch, "_qq_joinmanager_patched", False):

        def _patched_ws_dispatch(
            self: Any, event: str, *args: Any, **kwargs: Any
        ) -> None:
            handler = _EVENT_HANDLERS.get(event)
            # 未来 botpy/AstrBot 若原生实现该事件，则交还原生分发。
            if handler is None or hasattr(self, f"on_{event}"):
                return original_dispatch(self, event, *args, **kwargs)
            asyncio.create_task(
                _run_event_handler(handler, self, event, args, kwargs),
            )

        _patched_ws_dispatch._qq_joinmanager_patched = True  # type: ignore[attr-defined]
        Client.ws_dispatch = _patched_ws_dispatch  # type: ignore[method-assign]

    return True


def available() -> bool:
    """botpy 是否可用。"""
    return Client is not None and ConnectionState is not None and Route is not None


def set_event_handler(
    event_name: str,
    handler: Callable[[Any, Any], Awaitable[None]],
) -> None:
    """注册事件回调。"""
    _EVENT_HANDLERS[event_name] = handler


def clear_event_handler(
    event_name: str,
    handler: Callable[[Any, Any], Awaitable[None]],
) -> None:
    """注销事件回调，仅当当前回调仍属于该插件实例时。"""
    if _EVENT_HANDLERS.get(event_name) == handler:
        _EVENT_HANDLERS.pop(event_name, None)


def needs_reload(client: Any) -> bool:
    """判断已连接的客户端是否需要重载才能收到入群事件。

    Args:
        client: AstrBot qq_official 适配器持有的 botpy Client。

    Returns:
        True 表示当前连接建立时补丁尚未生效，需要重载平台。
    """
    connection = getattr(client, "_connection", None)
    if connection is None:
        # 尚未开始连接，补丁会在后续连接时生效。
        return False
    parsers = getattr(getattr(connection, "state", None), "parsers", {}) or {}
    intents = int(getattr(client, "intents", 0) or 0)
    return "group_join_request" not in parsers or not (intents & GROUP_MEMBER_INTENT)


async def _request(
    client: Any,
    method: str,
    path: str,
    path_params: dict[str, str],
    **kwargs: Any,
) -> Any:
    """调用 QQ 官方 REST 接口。

    Args:
        client: botpy Client，其 http 会话已由适配器完成鉴权。
        method: HTTP 方法。
        path: 带 {占位符} 的接口路径。
        path_params: 路径占位符参数。
        **kwargs: 传给 aiohttp 的请求参数（json / params 等）。

    Returns:
        接口返回的 JSON 数据。
    """
    if Route is None:
        raise RuntimeError("qq-botpy 未安装，无法调用 QQ 官方接口。")
    route = Route(method, path, **path_params)
    return await client.http.request(route, **kwargs)


async def approve_join_request(
    client: Any,
    group_openid: str,
    member_openid: str,
    join_request_id: str = "",
) -> Any:
    """通过入群申请。"""
    payload: dict[str, Any] = {"op": "approve"}
    if join_request_id:
        payload["join_request_id"] = join_request_id
    return await _request(
        client,
        "POST",
        "/v2/groups/{group_openid}/approval_join_request/{member_openid}",
        {"group_openid": group_openid, "member_openid": member_openid},
        json=payload,
    )


async def decline_join_request(
    client: Any,
    group_openid: str,
    member_openid: str,
    join_request_id: str = "",
    reason: str = "",
    add_to_blacklist: bool = False,
) -> Any:
    """拒绝入群申请，可选加入群黑名单。"""
    payload: dict[str, Any] = {
        "op": "decline",
        "add_to_member_blacklist": bool(add_to_blacklist),
    }
    if join_request_id:
        payload["join_request_id"] = join_request_id
    if reason:
        payload["reject_reason"] = reason
    return await _request(
        client,
        "POST",
        "/v2/groups/{group_openid}/approval_join_request/{member_openid}",
        {"group_openid": group_openid, "member_openid": member_openid},
        json=payload,
    )


async def list_join_requests(
    client: Any,
    group_openid: str,
    limit: int = 20,
    cursor: str = "",
) -> dict[str, Any]:
    """拉取入群申请列表。"""
    data = await _request(
        client,
        "GET",
        "/v2/groups/{group_openid}/join_request_list",
        {"group_openid": group_openid},
        params={"cursor": cursor, "limit": limit},
    )
    return data if isinstance(data, dict) else {}


async def get_group_info(client: Any, group_openid: str) -> dict[str, Any]:
    """获取群基本信息（该接口仅部分机器人有权限）。"""
    data = await _request(
        client,
        "GET",
        "/v2/groups/{group_openid}/info",
        {"group_openid": group_openid},
    )
    return data if isinstance(data, dict) else {}


async def send_group_text(client: Any, group_openid: str, content: str) -> Any:
    """向群聊发送纯文本主动消息。"""
    if not content or not content.strip():
        return None
    return await client.api.post_group_message(
        group_openid=group_openid,
        msg_type=0,
        content=content,
        msg_seq=random.randint(1, 10000),
    )
