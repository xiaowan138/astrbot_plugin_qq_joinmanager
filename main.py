"""QQ 官机入群管理器。

基于 QQ 官方机器人 API 的入群申请自动审核插件：
- 按验证消息关键词自动通过 / 拒绝；
- 支持分群规则、群黑白名单、拒绝理由、拒绝拉黑；
- 自动通过、手动审核、退群时发送提示；
- 记录入群来源分类并提供 /入群统计；
- 提供 /入群审核 列表 / 通过 / 拒绝 人工处理入口。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from . import qq_hook

DEFAULT_GROUP_KEY = "default"
PLUGIN_DATA_NAME = "astrbot_plugin_qq_joinmanager"

DEFAULT_TEMPLATES = {
    "welcome_msg": "🎉 欢迎 %user_name% 加入本群！",
    "increase_msg": "🎉 欢迎 %user_name% 加入本群！",
    "decrease_msg": "👋 %user_name% 已退出群聊。",
    "reject_reason": "未通过自动审核：验证消息触发了关键词「%key%」。",
    "notice_msg": (
        "【入群申请】%result%\n"
        "群：%group_name%（%group_id%）\n"
        "用户：%user_name%（%user_id%）\n"
        "验证消息：%comment%"
    ),
}

_PLACEHOLDER_RE = re.compile(r"%[A-Za-z_]+%")


def _field(obj: Any, name: str, default: Any = "") -> Any:
    """兼容 dict / SimpleNamespace 的事件字段读取。"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _short_time(value: Any) -> str:
    """把 RFC3339 时间压缩为便于展示的格式。"""
    text = str(value or "")
    if "T" in text:
        return text.replace("T", " ")[:16]
    return text[:16]


class QQJoinManager(Star):
    """QQ 官机入群请求管理器。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config

        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_DATA_NAME))
        self.records_file = self.data_dir / "join_records.json"
        self.records = self._load_records()

        self.delay = max(0.0, min(5.0, self._as_float(self.config.get("delay", 0.5))))

        control = self.config.get("control", {})
        if not isinstance(control, dict):
            control = {}
        self.control_method = str(control.get("block_method", "blacklist")).strip()
        self.control_list = {
            self._norm_key(item)
            for item in (control.get("control_list") or [])
            if str(item).strip()
        }

        self.accept_rules, self.accept_groups = self._load_accept_rules()
        self.reject_rules, self.reject_groups = self._load_reject_rules()
        self.templates = self._load_templates()

        notice = self.config.get("notice", {})
        if not isinstance(notice, dict):
            notice = {}
        self.notice_targets: dict[str, list[str]] = {
            key_name: [
                str(item).strip()
                for item in (notice.get(key_name) or [])
                if str(item).strip()
            ]
            for key_name in ("accept", "reject", "pending", "increase", "decrease")
        }

        self.admin_openids = {
            str(item).strip().upper()
            for item in (self.config.get("admin_openids") or [])
            if str(item).strip()
        }
        self.reject_blacklist = bool(self.config.get("reject_blacklist", False))
        self.record_limit = max(50, self._as_int(self.config.get("record_limit", 500)))

        # 运行时缓存
        self._seen_requests: dict[str, float] = {}
        self._recent_approvals: dict[tuple[str, str], float] = {}
        self._pending_cache: dict[str, dict[str, dict[str, Any]]] = {}
        self._group_name_cache: dict[str, dict[str, Any]] = {}
        self._group_name_refreshing: set[str] = set()
        self._bound_handlers: dict[str, Any] = {}
        self._reload_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # 插件生命周期
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """注册底层事件回调。"""
        if not qq_hook.install_patches():
            logger.error(
                "[QQJoinManager] qq-botpy 不可用，无法监听入群事件。"
                "请确认使用 qq_official（WebSocket）平台适配器。",
            )
            return

        self._bound_handlers = {
            "group_join_request": self._handle_group_join_request,
            "group_member_add": self._handle_group_member_add,
            "group_member_remove": self._handle_group_member_remove,
        }
        for event_name, handler in self._bound_handlers.items():
            qq_hook.set_event_handler(event_name, handler)

        # 启动时插件先于平台加载，通常无需重载；热启用插件时可能需要。
        self._reload_task = asyncio.create_task(self._reload_running_platforms())
        logger.info("[QQJoinManager] 入群事件监听已就绪。")

    async def terminate(self) -> None:
        """注销事件回调。"""
        for event_name, handler in self._bound_handlers.items():
            qq_hook.clear_event_handler(event_name, handler)
        self._bound_handlers.clear()

        if self._reload_task and not self._reload_task.done():
            self._reload_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reload_task

    async def _reload_running_platforms(self) -> None:
        """热启用插件时，重载已连接但缺少群成员 intent 的 QQ 官方平台。"""
        await asyncio.sleep(1.5)
        try:
            platforms = list(self.context.platform_manager.get_insts())
        except Exception:
            return

        for inst in platforms:
            try:
                if inst.meta().name != "qq_official":
                    continue
                client = getattr(inst, "client", None)
                if client is None or not qq_hook.needs_reload(client):
                    continue
                logger.info(
                    "[QQJoinManager] QQ 官方平台已连接但缺少群成员事件订阅，"
                    "正在重载平台以使入群事件生效。",
                )
                await self.context.platform_manager.reload(inst.config)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[QQJoinManager] 重载 QQ 官方平台失败")

    # ------------------------------------------------------------------
    # 配置解析
    # ------------------------------------------------------------------

    @staticmethod
    def _as_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.5

    @staticmethod
    def _as_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 500

    @staticmethod
    def _norm_key(value: Any) -> str:
        return str(value or "").strip().lower()

    def _group_keys_from_item(self, item: dict[str, Any]) -> set[str]:
        raw = item.get("group_ids")
        if raw is None:
            raw = [DEFAULT_GROUP_KEY]
        if not isinstance(raw, list):
            raw = [raw]
        keys = {self._norm_key(value) for value in raw if str(value).strip()}
        return keys or {DEFAULT_GROUP_KEY}

    @staticmethod
    def _split_keywords(value: Any) -> list[str]:
        if isinstance(value, str):
            raw = value.replace("，", ",").split(",")
        elif isinstance(value, list):
            raw = value
        else:
            return []
        return [str(item).strip() for item in raw if str(item).strip()]

    def _load_accept_rules(self) -> tuple[dict[str, dict[str, list[str]]], set[str]]:
        rules: dict[str, dict[str, list[str]]] = {}
        configured_groups: set[str] = set()
        for item in self.config.get("accept_rules") or []:
            if not isinstance(item, dict):
                continue
            group_keys = self._group_keys_from_item(item)
            configured_groups.update(group_keys)
            if not item.get("enabled", True):
                continue
            category = str(item.get("category", "")).strip()
            keywords = self._split_keywords(item.get("keywords"))
            if not category or not keywords:
                continue
            for group_key in group_keys:
                bucket = rules.setdefault(group_key, {})
                category_keywords = bucket.setdefault(category, [])
                for keyword in keywords:
                    if keyword not in category_keywords:
                        category_keywords.append(keyword)
        return rules, configured_groups

    def _load_reject_rules(self) -> tuple[dict[str, list[str]], set[str]]:
        rules: dict[str, list[str]] = {}
        configured_groups: set[str] = set()
        for item in self.config.get("reject_rules") or []:
            if not isinstance(item, dict):
                continue
            group_keys = self._group_keys_from_item(item)
            configured_groups.update(group_keys)
            if not item.get("enabled", True):
                continue
            keywords = self._split_keywords(item.get("keywords"))
            if not keywords:
                continue
            for group_key in group_keys:
                bucket = rules.setdefault(group_key, [])
                for keyword in keywords:
                    if keyword not in bucket:
                        bucket.append(keyword)
        return rules, configured_groups

    def _load_templates(self) -> dict[str, dict[str, str]]:
        raw = self.config.get("message_templates", {})
        if not isinstance(raw, dict):
            raw = {}
        templates: dict[str, dict[str, str]] = {}
        for key_name in DEFAULT_TEMPLATES:
            mapping: dict[str, str] = {}
            for item in raw.get(key_name) or []:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text", ""))
                if not text:
                    continue
                for group_key in self._group_keys_from_item(item):
                    mapping[group_key] = text
            templates[key_name] = mapping
        return templates

    # ------------------------------------------------------------------
    # 群标识与规则查询
    # ------------------------------------------------------------------

    def _group_aliases(self, group_id: str) -> set[str]:
        aliases = {self._norm_key(group_id)}
        cached = self._group_name_cache.get(group_id)
        if cached and cached.get("name"):
            aliases.add(self._norm_key(cached["name"]))
        return aliases

    def _get_accept_rules(self, group_id: str) -> dict[str, list[str]]:
        aliases = self._group_aliases(group_id)
        matched = aliases & self.accept_groups
        if not matched:
            return self.accept_rules.get(DEFAULT_GROUP_KEY, {})

        merged: dict[str, list[str]] = {}
        for alias in matched:
            for category, keywords in self.accept_rules.get(alias, {}).items():
                bucket = merged.setdefault(category, [])
                for keyword in keywords:
                    if keyword not in bucket:
                        bucket.append(keyword)
        return merged

    def _get_reject_keywords(self, group_id: str) -> list[str]:
        aliases = self._group_aliases(group_id)
        matched = aliases & self.reject_groups
        if not matched:
            return self.reject_rules.get(DEFAULT_GROUP_KEY, [])

        merged: list[str] = []
        for alias in matched:
            for keyword in self.reject_rules.get(alias, []):
                if keyword not in merged:
                    merged.append(keyword)
        return merged

    def _get_template(self, key_name: str, group_id: str) -> str:
        mapping = self.templates.get(key_name, {})
        cached = self._group_name_cache.get(group_id)
        aliases: list[str] = []
        if cached and cached.get("name"):
            aliases.append(self._norm_key(cached["name"]))
        aliases.append(self._norm_key(group_id))
        for alias in aliases:
            if alias in mapping:
                return mapping[alias]
        return mapping.get(DEFAULT_GROUP_KEY) or DEFAULT_TEMPLATES[key_name]

    def _is_group_enabled(self, group_id: str) -> bool:
        aliases = self._group_aliases(group_id)
        if self.control_method == "whitelist":
            if not self.control_list:
                return False
            return bool(aliases & self.control_list)
        return not bool(aliases & self.control_list)

    def _format(self, template: str, **values: Any) -> str:
        """替换模板占位符，且不会再扫描用户输入中的占位符。"""

        def replace(match: re.Match[str]) -> str:
            key = match.group(0)
            return str(values.get(key, key))

        return _PLACEHOLDER_RE.sub(replace, str(template or ""))

    def _template_values(
        self,
        group_id: str,
        group_name: str,
        user_id: str,
        user_name: str,
        comment: str,
        *,
        category: str = "",
        keyword: str = "",
        source: str = "",
        invited_by: str = "",
        risk_tips: str = "",
        result: str = "",
        reason: str = "",
    ) -> dict[str, str]:
        return {
            "%group_id%": group_id,
            "%group_name%": group_name or group_id,
            "%user_id%": user_id,
            "%user_name%": user_name or user_id or "新成员",
            "%comment%": comment or "无",
            "%category%": category,
            "%key%": keyword,
            "%apply_source%": source,
            "%invited_by%": invited_by,
            "%risk_tips%": risk_tips,
            "%result%": result,
            "%reason%": reason,
        }

    # ------------------------------------------------------------------
    # 群名称
    # ------------------------------------------------------------------

    def _get_cached_group_name(self, group_id: str) -> str:
        cached = self._group_name_cache.get(group_id)
        if cached and cached.get("expires", 0) > time.time():
            return str(cached.get("name", ""))
        return ""

    def _maybe_refresh_group_name(self, client: Any, group_id: str) -> None:
        """事件处理不阻塞等待群信息，只在后台刷新一次。"""
        cached = self._group_name_cache.get(group_id)
        if cached and cached.get("expires", 0) > time.time():
            return
        if group_id in self._group_name_refreshing:
            return
        asyncio.create_task(self._refresh_group_name(client, group_id))

    async def _refresh_group_name(self, client: Any, group_id: str) -> None:
        self._group_name_refreshing.add(group_id)
        try:
            await self._get_group_name(client, group_id)
        finally:
            self._group_name_refreshing.discard(group_id)

    async def _get_group_name(self, client: Any, group_id: str) -> str:
        cached = self._group_name_cache.get(group_id)
        now = time.time()
        if cached and cached.get("expires", 0) > now:
            return str(cached.get("name", ""))

        name = ""
        try:
            data = await asyncio.wait_for(
                qq_hook.get_group_info(client, group_id),
                timeout=3,
            )
            name = str(data.get("group_name") or "")
        except Exception as exc:
            logger.debug("[QQJoinManager] 获取群名称失败(%s)：%s", group_id, exc)

        # 有名称缓存 6 小时；无权限或失败时 30 分钟后再试。
        self._group_name_cache[group_id] = {
            "name": name,
            "expires": now + (6 * 3600 if name else 1800),
        }
        return name

    def _remember_group_name(self, group_id: str, name: str) -> None:
        if not group_id or not name:
            return
        self._group_name_cache[group_id] = {
            "name": name,
            "expires": time.time() + 6 * 3600,
        }

    def _remember_group_from_event(self, event: AstrMessageEvent) -> None:
        group = getattr(getattr(event, "message_obj", None), "group", None)
        name = getattr(group, "group_name", None) if group else None
        self._remember_group_name(event.get_group_id(), str(name or ""))

    # ------------------------------------------------------------------
    # 数据存储
    # ------------------------------------------------------------------

    def _load_records(self) -> dict[str, Any]:
        if not self.records_file.exists():
            return {}
        try:
            data = json.loads(self.records_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("[QQJoinManager] 读取入群记录失败：%s", exc)
            return {}
        if not isinstance(data, dict):
            return {}
        for bucket in data.values():
            if not isinstance(bucket, dict):
                continue
            bucket.setdefault("total", 0)
            bucket.setdefault("categories", {})
            bucket.setdefault("members", {})
            bucket.setdefault("days", {})
        return data

    def _save_records(self) -> None:
        try:
            temp_file = self.records_file.with_suffix(".tmp")
            temp_file.write_text(
                json.dumps(self.records, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp_file.replace(self.records_file)
        except Exception as exc:
            logger.error("[QQJoinManager] 保存入群记录失败：%s", exc)

    def _record_join(
        self,
        group_id: str,
        member_id: str,
        user_name: str,
        category: str,
        source: str,
    ) -> None:
        bucket = self.records.setdefault(
            group_id,
            {"total": 0, "categories": {}, "members": {}, "days": {}},
        )
        bucket["total"] = int(bucket.get("total", 0)) + 1
        categories = bucket.setdefault("categories", {})
        categories[category] = int(categories.get(category, 0)) + 1

        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        days = bucket.setdefault("days", {})
        days[today] = int(days.get(today, 0)) + 1
        if len(days) > 90:
            for key in sorted(days)[:-90]:
                days.pop(key, None)

        members = bucket.setdefault("members", {})
        members[member_id] = {
            "name": user_name,
            "time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "category": category,
            "source": source,
        }
        if len(members) > self.record_limit:
            ordered = sorted(
                members.items(),
                key=lambda item: (
                    str(item[1].get("time", "")) if isinstance(item[1], dict) else ""
                ),
            )
            for member_key, _ in ordered[: len(members) - self.record_limit]:
                members.pop(member_key, None)

        self._save_records()

    def _remove_member_record(
        self, group_id: str, member_id: str
    ) -> dict[str, Any] | None:
        bucket = self.records.get(group_id)
        if not isinstance(bucket, dict):
            return None
        members = bucket.get("members")
        if not isinstance(members, dict):
            return None
        info = members.pop(member_id, None)
        if info is not None:
            self._save_records()
        return info if isinstance(info, dict) else None

    # ------------------------------------------------------------------
    # 去重与缓存
    # ------------------------------------------------------------------

    def _is_duplicate_request(self, request_id: str) -> bool:
        now = time.time()
        seen = self._seen_requests
        if request_id in seen and now - seen[request_id] < 600:
            return True
        seen[request_id] = now
        if len(seen) > 2000:
            for key in [key for key, ts in seen.items() if now - ts > 600]:
                seen.pop(key, None)
        return False

    def _mark_recent_approval(self, group_id: str, member_id: str) -> None:
        self._recent_approvals[(group_id, member_id)] = time.time()

    def _was_recently_approved(self, group_id: str, member_id: str) -> bool:
        ts = self._recent_approvals.get((group_id, member_id))
        if ts is None:
            return False
        if time.time() - ts > 300:
            self._recent_approvals.pop((group_id, member_id), None)
            return False
        return True

    def _cache_pending(
        self,
        group_id: str,
        member_id: str,
        info: dict[str, Any],
    ) -> None:
        bucket = self._pending_cache.setdefault(group_id, {})
        bucket[member_id] = info
        if len(bucket) > 200:
            ordered = sorted(bucket.items(), key=lambda item: item[1].get("at", 0))
            for member_key, _ in ordered[: len(bucket) - 200]:
                bucket.pop(member_key, None)

    def _extract_verify_comment(self, verify_info: Any) -> str:
        if not verify_info:
            return ""
        if isinstance(verify_info, dict):
            method = verify_info.get("method")
            parts: list[str] = []
            message = verify_info.get("verify_message")
            if message:
                parts.append(str(message))
            qa_list = verify_info.get("review_qa_list") or []
            if isinstance(qa_list, list):
                for item in qa_list:
                    if not isinstance(item, dict):
                        continue
                    question = str(item.get("question") or "问题")
                    answer = str(item.get("answer") or "")
                    parts.append(f"{question}：{answer}")
            if not parts and method == "admin_review_qa":
                return "问答验证"
            return "；".join(parts)
        return ""

    @staticmethod
    def _source_text(apply_source: Any) -> str:
        value = str(apply_source or "")
        if value == "self_apply":
            return "主动申请"
        if value == "invited":
            return "被邀请"
        return value

    # ------------------------------------------------------------------
    # 发送与通知
    # ------------------------------------------------------------------

    async def _send_group_message(self, client: Any, group_id: str, text: str) -> None:
        if not text:
            return
        try:
            await qq_hook.send_group_text(client, group_id, text)
        except Exception as exc:
            logger.error(
                "[QQJoinManager] 发送群消息失败(%s)：%s",
                group_id,
                exc,
            )

    async def _notify(
        self,
        client: Any,
        kind: str,
        group_id: str,
        group_name: str,
        user_id: str,
        user_name: str,
        comment: str,
        *,
        category: str = "",
        keyword: str = "",
        source: str = "",
        invited_by: str = "",
        risk_tips: str = "",
        result: str = "",
        reason: str = "",
    ) -> None:
        targets = self.notice_targets.get(kind) or []
        if not targets:
            return

        template = self._get_template("notice_msg", group_id)
        text = self._format(
            template,
            **self._template_values(
                group_id,
                group_name,
                user_id,
                user_name,
                comment,
                category=category,
                keyword=keyword,
                source=source,
                invited_by=invited_by,
                risk_tips=risk_tips,
                result=result,
                reason=reason,
            ),
        )
        for target in targets:
            target_group = group_id if target.lower() == "origin" else target
            try:
                await qq_hook.send_group_text(client, target_group, text)
            except Exception as exc:
                logger.error(
                    "[QQJoinManager] 发送通知到 %s 失败：%s",
                    target_group,
                    exc,
                )
            await asyncio.sleep(self.delay)

    # ------------------------------------------------------------------
    # 入群申请事件
    # ------------------------------------------------------------------

    async def _handle_group_join_request(self, client: Any, event: Any) -> None:
        group_id = str(_field(event, "group_openid"))
        member_id = str(_field(event, "member_openid"))
        if not group_id or not member_id:
            return
        if not self._is_group_enabled(group_id):
            return

        request_id = str(_field(event, "join_request_id"))
        if request_id and self._is_duplicate_request(request_id):
            return

        user_name = str(_field(event, "username")) or member_id
        comment = self._extract_verify_comment(_field(event, "verify_info", None))
        apply_source = self._source_text(_field(event, "apply_source"))
        invited_by = str(_field(event, "invited_by"))
        risk_tips = str(_field(event, "risk_tips"))
        is_bot = bool(_field(event, "bot", False))
        apply_at = str(_field(event, "apply_at"))

        group_name = self._get_cached_group_name(group_id)
        self._maybe_refresh_group_name(client, group_id)
        self._cache_pending(
            group_id,
            member_id,
            {
                "username": user_name,
                "comment": comment,
                "join_request_id": request_id,
                "at": time.time(),
                "apply_at": apply_at,
                "apply_source": apply_source,
                "invited_by": invited_by,
                "risk_tips": risk_tips,
                "bot": is_bot,
            },
        )
        logger.info(
            "[QQJoinManager] 收到入群申请 | 群:%s | 用户:%s(%s) | 验证:%s",
            group_id,
            user_name,
            member_id,
            comment,
        )

        # 已由 QQ 原生自动审批策略通过，不再重复调用审批接口。
        if _field(event, "auto_approved", None):
            logger.info(
                "[QQJoinManager] 申请已由 QQ 自动审批策略通过 | 群:%s | 用户:%s",
                group_id,
                user_name,
            )
            self._record_join(
                group_id,
                member_id,
                user_name,
                "QQ自动审批",
                "自动审批策略",
            )
            self._mark_recent_approval(group_id, member_id)
            welcome = self._format(
                self._get_template("welcome_msg", group_id),
                **self._template_values(
                    group_id,
                    group_name,
                    member_id,
                    user_name,
                    comment,
                    category="QQ自动审批",
                    source=apply_source,
                    invited_by=invited_by,
                    risk_tips=risk_tips,
                ),
            )
            await self._send_group_message(client, group_id, welcome)
            await self._notify(
                client,
                "accept",
                group_id,
                group_name,
                member_id,
                user_name,
                comment,
                category="QQ自动审批",
                source=apply_source,
                invited_by=invited_by,
                risk_tips=risk_tips,
                result="QQ自动审批策略通过",
            )
            return

        lowered = comment.lower()
        matched_reject: str | None = None
        for keyword in self._get_reject_keywords(group_id):
            if keyword.lower() in lowered:
                matched_reject = keyword
                break

        if matched_reject:
            reason = self._format(
                self._get_template("reject_reason", group_id),
                **self._template_values(
                    group_id,
                    group_name,
                    member_id,
                    user_name,
                    comment,
                    keyword=matched_reject,
                    source=apply_source,
                    invited_by=invited_by,
                    risk_tips=risk_tips,
                ),
            )
            try:
                await qq_hook.decline_join_request(
                    client,
                    group_id,
                    member_id,
                    request_id,
                    reason,
                    self.reject_blacklist,
                )
                logger.info(
                    "[QQJoinManager] 已拒绝 %s(%s)，关键词：%s",
                    user_name,
                    member_id,
                    matched_reject,
                )
            except Exception as exc:
                logger.error("[QQJoinManager] 拒绝入群申请失败：%s", exc)
                await self._notify(
                    client,
                    "reject",
                    group_id,
                    group_name,
                    member_id,
                    user_name,
                    comment,
                    keyword=matched_reject,
                    source=apply_source,
                    invited_by=invited_by,
                    risk_tips=risk_tips,
                    result="自动拒绝失败",
                    reason=str(exc),
                )
                return

            await self._notify(
                client,
                "reject",
                group_id,
                group_name,
                member_id,
                user_name,
                comment,
                keyword=matched_reject,
                source=apply_source,
                invited_by=invited_by,
                risk_tips=risk_tips,
                result="自动拒绝",
                reason=reason,
            )
            return

        matched_accept: tuple[str, str] | None = None
        for category, keywords in self._get_accept_rules(group_id).items():
            for keyword in keywords:
                if keyword.lower() in lowered:
                    matched_accept = (category, keyword)
                    break
            if matched_accept:
                break

        if matched_accept:
            category, keyword = matched_accept
            try:
                await qq_hook.approve_join_request(
                    client,
                    group_id,
                    member_id,
                    request_id,
                )
                logger.info(
                    "[QQJoinManager] 已通过 %s(%s)，分类：%s，关键词：%s",
                    user_name,
                    member_id,
                    category,
                    keyword,
                )
            except Exception as exc:
                logger.error("[QQJoinManager] 通过入群申请失败：%s", exc)
                await self._notify(
                    client,
                    "accept",
                    group_id,
                    group_name,
                    member_id,
                    user_name,
                    comment,
                    category=category,
                    keyword=keyword,
                    source=apply_source,
                    invited_by=invited_by,
                    risk_tips=risk_tips,
                    result="自动通过失败",
                    reason=str(exc),
                )
                return

            self._record_join(group_id, member_id, user_name, category, "自动审核")
            self._mark_recent_approval(group_id, member_id)

            welcome = self._format(
                self._get_template("welcome_msg", group_id),
                **self._template_values(
                    group_id,
                    group_name,
                    member_id,
                    user_name,
                    comment,
                    category=category,
                    keyword=keyword,
                    source=apply_source,
                    invited_by=invited_by,
                    risk_tips=risk_tips,
                ),
            )
            await self._send_group_message(client, group_id, welcome)
            await self._notify(
                client,
                "accept",
                group_id,
                group_name,
                member_id,
                user_name,
                comment,
                category=category,
                keyword=keyword,
                source=apply_source,
                invited_by=invited_by,
                risk_tips=risk_tips,
                result="自动通过",
            )
            return

        logger.info(
            "[QQJoinManager] 入群申请未命中规则，保留待人工处理 | 群:%s | 用户:%s(%s)",
            group_id,
            user_name,
            member_id,
        )
        await self._notify(
            client,
            "pending",
            group_id,
            group_name,
            member_id,
            user_name,
            comment,
            source=apply_source,
            invited_by=invited_by,
            risk_tips=risk_tips,
            result="待人工处理",
        )

    # ------------------------------------------------------------------
    # 成员加入 / 退出事件
    # ------------------------------------------------------------------

    async def _handle_group_member_add(self, client: Any, event: Any) -> None:
        group_id = str(_field(event, "group_openid"))
        member_id = str(_field(event, "member_openid"))
        if not group_id or not member_id or not self._is_group_enabled(group_id):
            return
        if self._was_recently_approved(group_id, member_id):
            self._recent_approvals.pop((group_id, member_id), None)
            return

        cached = self._pending_cache.get(group_id, {}).get(member_id) or {}
        bucket = self.records.get(group_id, {})
        member_record = (
            bucket.get("members", {}).get(member_id, {})
            if isinstance(bucket, dict) and isinstance(bucket.get("members"), dict)
            else {}
        )
        user_name = str(
            cached.get("username")
            or (member_record.get("name") if isinstance(member_record, dict) else "")
            or "新成员",
        )
        comment = str(cached.get("comment", ""))
        group_name = self._get_cached_group_name(group_id)
        self._maybe_refresh_group_name(client, group_id)

        self._record_join(group_id, member_id, user_name, "人工审核", "人工审核")

        welcome = self._format(
            self._get_template("increase_msg", group_id),
            **self._template_values(
                group_id,
                group_name,
                member_id,
                user_name,
                comment,
            ),
        )
        await self._send_group_message(client, group_id, welcome)
        await self._notify(
            client,
            "increase",
            group_id,
            group_name,
            member_id,
            user_name,
            comment,
            result="管理员审核通过（人工审核）",
        )

    async def _handle_group_member_remove(self, client: Any, event: Any) -> None:
        group_id = str(_field(event, "group_openid"))
        member_id = str(_field(event, "member_openid"))
        if not group_id or not member_id or not self._is_group_enabled(group_id):
            return

        cached = self._pending_cache.get(group_id, {}).pop(member_id, None) or {}
        record = self._remove_member_record(group_id, member_id)
        user_name = str(
            (record or {}).get("name") or cached.get("username") or "一位成员",
        )
        group_name = self._get_cached_group_name(group_id)
        self._maybe_refresh_group_name(client, group_id)

        text = self._format(
            self._get_template("decrease_msg", group_id),
            **self._template_values(
                group_id,
                group_name,
                member_id,
                user_name,
                str(cached.get("comment", "")),
            ),
        )
        await self._send_group_message(client, group_id, text)
        await self._notify(
            client,
            "decrease",
            group_id,
            group_name,
            member_id,
            user_name,
            "",
            result="成员退出",
        )

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------

    def _get_client(self, event: AstrMessageEvent) -> Any:
        try:
            if event.get_platform_name() != "qq_official":
                return None
            inst = self.context.get_platform_inst(event.get_platform_id())
            return getattr(inst, "client", None) if inst else None
        except Exception:
            return None

    @staticmethod
    def _command_args(event: AstrMessageEvent) -> str:
        parts = (event.get_message_str() or "").strip().split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        sender = str(event.get_sender_id() or "").upper()
        return bool(sender) and sender in self.admin_openids

    @filter.command("入群统计", alias={"加群统计"})
    async def stats_command(self, event: AstrMessageEvent):
        """查看本群入群统计。"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("该命令仅限群聊使用。")
            return

        self._remember_group_from_event(event)
        client = self._get_client(event)
        group_name = ""
        if client is not None:
            group_name = await self._get_group_name(client, group_id)
        else:
            cached = self._group_name_cache.get(group_id)
            group_name = str(cached.get("name", "")) if cached else ""

        yield event.plain_result(
            self._build_stats_text(group_id, group_name),
        )

    def _build_stats_text(self, group_id: str, group_name: str) -> str:
        bucket = self.records.get(group_id, {})
        if not isinstance(bucket, dict):
            bucket = {}

        total = int(bucket.get("total", 0) or 0)
        categories = bucket.get("categories") or {}
        days = bucket.get("days") or {}
        members = bucket.get("members") or {}
        if not total and not categories and not members:
            return f"📊 入群统计\n群：{group_name or group_id}（{group_id}）\n暂无入群记录。"

        manual_count = 0
        auto_count = 0
        if isinstance(categories, dict):
            manual_count = int(categories.get("人工审核", 0) or 0)
            auto_count = max(0, total - manual_count)

        lines = [
            "📊 入群统计",
            f"群：{group_name or group_id}（{group_id}）",
            f"总入群：{total}",
            f"今日入群：{int(days.get(datetime.now().strftime('%Y-%m-%d'), 0) or 0)}",
            f"自动通过：{auto_count} ｜ 人工审核：{manual_count}",
        ]

        if isinstance(categories, dict) and categories:
            lines.append("来源分类：")
            for category, count in sorted(
                categories.items(),
                key=lambda item: int(item[1] or 0),
                reverse=True,
            ):
                lines.append(f"  • {category}：{int(count or 0)}")

        recent: list[tuple[str, dict[str, Any]]] = []
        if isinstance(members, dict):
            for member_id, info in members.items():
                if isinstance(info, dict):
                    recent.append((member_id, info))
        recent.sort(key=lambda item: str(item[1].get("time", "")), reverse=True)
        if recent:
            lines.append("最近入群：")
            for member_id, info in recent[:5]:
                name = str(info.get("name") or member_id)
                lines.append(
                    f"  • {name}｜{info.get('time', '')}｜{info.get('category', '')}",
                )
        return "\n".join(lines)

    @filter.command("入群审核", alias={"入群审批"})
    async def audit_command(self, event: AstrMessageEvent):
        """人工查看、通过、拒绝入群申请。"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("该命令仅限群聊使用。")
            return

        client = self._get_client(event)
        if client is None:
            yield event.plain_result(
                "当前平台不是 QQ 官方 WebSocket 适配器，无法调用入群审批接口。",
            )
            return

        self._remember_group_from_event(event)
        args = self._command_args(event)
        if not args:
            yield event.plain_result(self._audit_usage())
            return

        action, _, rest = args.partition(" ")
        action = action.strip()
        rest = rest.strip()

        if action.lower() in {"我是谁", "whoami", "我的id", "我的id是我"}:
            yield event.plain_result(
                f"你的 openid：{event.get_sender_id() or '未知'}\n"
                f"当前群 openid：{group_id}",
            )
            return

        if not self._is_admin(event):
            yield event.plain_result(
                "你没有执行入群审核的权限。\n"
                f"你的 openid：{event.get_sender_id() or '未知'}\n"
                "请让机器人管理员把该 openid 填入插件配置的 admin_openids。",
            )
            return

        if action.lower() in {"列表", "list", "待处理"}:
            async for result in self._audit_list(event, client, group_id):
                yield result
            return

        if action.lower() in {"通过", "approve", "同意"}:
            if not rest:
                yield event.plain_result(self._audit_usage())
                return
            async for result in self._audit_approve(event, client, group_id, rest):
                yield result
            return

        if action.lower() in {"拒绝", "reject", "decline"}:
            if not rest:
                yield event.plain_result(self._audit_usage())
                return
            target, _, reason = rest.partition(" ")
            reason = reason.strip()
            async for result in self._audit_reject(
                event,
                client,
                group_id,
                target.strip(),
                reason,
            ):
                yield result
            return

        yield event.plain_result(self._audit_usage())

    @staticmethod
    def _audit_usage() -> str:
        return (
            "入群审核用法：\n"
            "/入群审核 列表\n"
            "/入群审核 通过 <序号或member_openid>\n"
            "/入群审核 拒绝 <序号或member_openid> [理由]\n"
            "/入群审核 我是谁"
        )

    async def _fetch_pending(
        self,
        client: Any,
        group_id: str,
    ) -> list[dict[str, Any]]:
        data = await qq_hook.list_join_requests(client, group_id, limit=50)
        items = data.get("list") if isinstance(data, dict) else None
        return items if isinstance(items, list) else []

    async def _audit_list(self, event: AstrMessageEvent, client: Any, group_id: str):
        try:
            items = await self._fetch_pending(client, group_id)
        except Exception as exc:
            yield event.plain_result(f"拉取入群申请失败：{exc}")
            return

        if not items:
            yield event.plain_result("当前没有待处理的入群申请。")
            return

        group_name = ""
        cached = self._group_name_cache.get(group_id)
        if cached:
            group_name = str(cached.get("name", ""))
        lines = [f"待处理入群申请（{len(items)} 条）"]
        if group_name:
            lines[0] += f"｜{group_name}"

        for index, item in enumerate(items[:10], start=1):
            if not isinstance(item, dict):
                continue
            member_id = str(item.get("member_openid", ""))
            user_name = str(item.get("username") or member_id)
            comment = self._extract_verify_comment(item.get("verify_info"))
            source = self._source_text(item.get("apply_source"))
            risk_tips = str(item.get("risk_tips") or "")
            bot_flag = bool(item.get("bot"))

            lines.append(f"{index}. {user_name}（{member_id}）")
            detail = f"   {source or '未知来源'}｜{_short_time(item.get('apply_at'))}"
            if bot_flag:
                detail += "｜机器人账号"
            lines.append(detail)
            lines.append(f"   验证消息：{comment or '无'}")
            if risk_tips:
                lines.append(f"   风控提示：{risk_tips}")

            self._cache_pending(
                group_id,
                member_id,
                {
                    "username": user_name,
                    "comment": comment,
                    "join_request_id": str(item.get("join_request_id", "")),
                    "at": time.time(),
                    "apply_at": str(item.get("apply_at", "")),
                    "apply_source": source,
                    "invited_by": str(item.get("invited_by", "")),
                    "risk_tips": risk_tips,
                    "bot": bot_flag,
                },
            )

        if len(items) > 10:
            lines.append(f"（仅显示前 10 条，共 {len(items)} 条）")
        lines.append("处理：/入群审核 通过 1 或 /入群审核 拒绝 1 理由")
        yield event.plain_result("\n".join(lines))

    async def _resolve_target(
        self,
        client: Any,
        group_id: str,
        target: str,
    ) -> dict[str, Any] | None:
        items = await self._fetch_pending(client, group_id)
        if target.isdigit():
            index = int(target) - 1
            if 0 <= index < len(items) and isinstance(items[index], dict):
                return items[index]
            return None

        upper_target = target.upper()
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("member_openid", "")).upper() == upper_target:
                return item
        # 列表里没有时，仍允许直接用 member_openid 审批。
        return {"member_openid": target, "join_request_id": ""}

    async def _audit_approve(
        self,
        event: AstrMessageEvent,
        client: Any,
        group_id: str,
        target: str,
    ):
        try:
            item = await self._resolve_target(client, group_id, target)
        except Exception as exc:
            yield event.plain_result(f"拉取入群申请失败：{exc}")
            return
        if not item:
            yield event.plain_result(f"未找到序号为 {target} 的待处理申请。")
            return

        member_id = str(item.get("member_openid", ""))
        if not member_id:
            yield event.plain_result("申请信息缺少 member_openid，无法审批。")
            return

        user_name = str(item.get("username") or member_id)
        comment = self._extract_verify_comment(item.get("verify_info"))
        group_name = await self._get_group_name(client, group_id)
        try:
            await qq_hook.approve_join_request(
                client,
                group_id,
                member_id,
                str(item.get("join_request_id", "")),
            )
        except Exception as exc:
            yield event.plain_result(f"通过入群申请失败：{exc}")
            return

        self._record_join(group_id, member_id, user_name, "人工审核", "管理员审核")
        self._mark_recent_approval(group_id, member_id)

        welcome = self._format(
            self._get_template("increase_msg", group_id),
            **self._template_values(
                group_id,
                group_name,
                member_id,
                user_name,
                comment,
            ),
        )
        await self._send_group_message(client, group_id, welcome)
        await self._notify(
            client,
            "increase",
            group_id,
            group_name,
            member_id,
            user_name,
            comment,
            result="管理员手动通过",
        )
        yield event.plain_result(f"已通过 {user_name}（{member_id}）的入群申请。")

    async def _audit_reject(
        self,
        event: AstrMessageEvent,
        client: Any,
        group_id: str,
        target: str,
        reason: str,
    ):
        try:
            item = await self._resolve_target(client, group_id, target)
        except Exception as exc:
            yield event.plain_result(f"拉取入群申请失败：{exc}")
            return
        if not item:
            yield event.plain_result(f"未找到序号为 {target} 的待处理申请。")
            return

        member_id = str(item.get("member_openid", ""))
        if not member_id:
            yield event.plain_result("申请信息缺少 member_openid，无法审批。")
            return

        user_name = str(item.get("username") or member_id)
        comment = self._extract_verify_comment(item.get("verify_info"))
        group_name = await self._get_group_name(client, group_id)
        reject_reason = reason or "管理员拒绝了你的入群申请。"
        try:
            await qq_hook.decline_join_request(
                client,
                group_id,
                member_id,
                str(item.get("join_request_id", "")),
                reject_reason,
                self.reject_blacklist,
            )
        except Exception as exc:
            yield event.plain_result(f"拒绝入群申请失败：{exc}")
            return

        await self._notify(
            client,
            "reject",
            group_id,
            group_name,
            member_id,
            user_name,
            comment,
            result="管理员手动拒绝",
            reason=reject_reason,
        )
        yield event.plain_result(f"已拒绝 {user_name}（{member_id}）的入群申请。")
