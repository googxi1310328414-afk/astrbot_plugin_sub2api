# -*- coding: utf-8 -*-
"""sub2api 余额互动：邮箱直接绑定 / 签到 / 打劫 / 查询 / 状态。

余额写入前持久化意图；无法确认结果时保留流水并暂停相关账号。
后台只按已确认的执行证据恢复；未知请求不重复提交。
/状态 渲染渠道监控卡片（只读，不占用账务锁）。
"""
import asyncio
import json
import math
import os
import random
import re
import tempfile
import time
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import NamedTuple

import aiohttp
from PIL import Image, ImageDraw, ImageFont
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from .recall import QuotaRecallManager
from .recovery import LedgerRecovery, operation_key, operation_notes

try:
    from astrbot.api.message_components import At
except ImportError:
    from astrbot.core.message.components import At

TZ = ZoneInfo("Asia/Shanghai")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
MILLI = Decimal("0.001")
QQ_PATTERN = re.compile(r"[1-9][0-9]{4,11}\Z")
DEFAULT_CONFIG = {
    "base_url": "http://127.0.0.1:8080/api/v1",
    "admin_email": "",
    "admin_password": "",
    "robbery_cooldown": 600,
    "allow_bind_admin": False,
    "quota_recall_seconds": 5,
    "recovery_enabled": True,
    "recovery_interval_seconds": 30,
}


class UserError(RuntimeError):
    """可直接回复给用户的固定业务提示。"""


class StateError(UserError):
    pass


class QuotaReply(NamedTuple):
    public_text: str
    quota_text: str


class ApiError(UserError):
    """读取失败，或服务端明确拒绝了余额写入。"""

    def __init__(self, message="sub2api 服务暂时不可用，请稍后重试。", code=None,
                 category="request_rejected"):
        super().__init__(message)
        self.code = code
        self.category = category
        self.known_not_applied = True


class OutcomeUnknown(ApiError):
    """请求可能已执行，不能自动重新提交。"""

    def __init__(self, message="余额操作结果未知，请管理员核账。", code=None,
                 category="outcome_unknown"):
        super().__init__(message, code, category)
        self.known_not_applied = False


class SlashCommandFilter(filter.CustomFilter):
    """Match the original QQ slash text after AstrBot removes its wake prefix."""

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        return (
            event.is_at_or_wake_command
            and event.get_platform_name() == "aiocqhttp"
            and event.message_obj.message_str.lstrip().startswith("/")
        )


def money(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise UserError("账号金额数据异常，请联系管理员检查。") from None
    if not result.is_finite():
        raise UserError("账号金额数据异常，请联系管理员检查。")
    return result


def normalize_email(value: str) -> str:
    value = value.strip().lower()
    if len(value) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise UserError("邮箱格式不正确，请检查后重试。")
    return value


def _positive_id(value):
    return type(value) is int and value > 0


def _valid_date(value):
    if not isinstance(value, str):
        return False
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") == value
    except ValueError:
        return False


def load_state(path=None) -> dict:
    path = path or STATE_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        state = {"bindings": {}, "checkin": {}, "robbery_ts": {}}
    except (OSError, ValueError):
        raise StateError("状态文件无法读取或已损坏，插件停止操作；请管理员检查并恢复备份。") from None
    try:
        if not isinstance(state, dict) or type(state.get("version", 1)) is not int:
            raise ValueError
        if state.get("version", 1) not in (1, 2):
            raise ValueError
        legacy = state.get("version", 1) == 1
        for key in ("bindings", "checkin", "robbery_ts"):
            if not isinstance(state.get(key), dict):
                raise ValueError
        for key in ("checkin_uid", "binding_requests", "transactions"):
            if key not in state:
                if not legacy:
                    raise ValueError
                state[key] = {}
            if not isinstance(state[key], dict):
                raise ValueError
        for qq, binding in state["bindings"].items():
            if not QQ_PATTERN.fullmatch(qq) or not isinstance(binding, dict):
                raise ValueError
            if not _positive_id(binding.get("uid")):
                raise ValueError
            binding["email"] = normalize_email(binding["email"])
            if "approved" not in binding:
                if not legacy:
                    raise ValueError
                # 保留旧绑定待重新校验；用户再次 /绑定 后启用。
                binding["approved"] = False
            if type(binding["approved"]) is not bool:
                raise ValueError
            if not binding["approved"]:
                state["binding_requests"].setdefault(
                    qq, {"email": binding["email"], "created_at": 0}
                )
        seen = set()
        for binding in state["bindings"].values():
            if binding["approved"]:
                if binding["uid"] in seen:
                    raise ValueError
                seen.add(binding["uid"])
        for qq, day in state["checkin"].items():
            if not QQ_PATTERN.fullmatch(qq) or not _valid_date(day):
                raise ValueError
            binding = state["bindings"].get(qq)
            if binding:
                uid = str(binding["uid"])
                state["checkin_uid"][uid] = max(state["checkin_uid"].get(uid, ""), day)
        for uid, day in state["checkin_uid"].items():
            if not uid.isdecimal() or int(uid) < 1 or not _valid_date(day):
                raise ValueError
        for qq, stamp in state["robbery_ts"].items():
            if not QQ_PATTERN.fullmatch(qq) or type(stamp) not in (int, float):
                raise ValueError
            if not math.isfinite(stamp) or stamp < 0:
                raise ValueError
        # protections 是非金融可选键：缺失只回填空表；存在时严格校验，损坏拒绝加载。
        if "protections" not in state:
            state["protections"] = {}
        for qq, entry in state["protections"].items():
            if not QQ_PATTERN.fullmatch(qq) or not isinstance(entry, dict):
                raise ValueError
            if type(entry.get("enabled")) is not bool:
                raise ValueError
            updated_at = entry.setdefault("updated_at", 0)  # 纯诊断字段，缺失回填 0。
            if type(updated_at) not in (int, float) or not math.isfinite(updated_at) or updated_at < 0:
                raise ValueError
        for qq, request in state["binding_requests"].items():
            if not QQ_PATTERN.fullmatch(qq) or not isinstance(request, dict):
                raise ValueError
            request["email"] = normalize_email(request["email"])
        for txid, tx in state["transactions"].items():
            if not isinstance(tx, dict) or tx.get("id") != txid:
                raise ValueError
            if tx.get("status") not in ("pending", "completed", "failed", "resolved"):
                raise ValueError
            if tx.get("kind") not in ("checkin", "rob_success", "rob_failure"):
                raise ValueError
            if not QQ_PATTERN.fullmatch(tx.get("qq", "")):
                raise ValueError
            if tx["kind"] == "checkin" and not _valid_date(tx.get("date")):
                raise ValueError
            if not isinstance(tx.get("uids"), list) or not tx["uids"]:
                raise ValueError
            if not all(_positive_id(uid) for uid in tx["uids"]):
                raise ValueError
            if not isinstance(tx.get("steps"), list) or not tx["steps"]:
                raise ValueError
            for step in tx["steps"]:
                if step.get("uid") not in tx["uids"] or step.get("operation") not in ("add", "subtract"):
                    raise ValueError
            completed = tx.get("completed_steps")
            if type(completed) is not int or not 0 <= completed <= len(tx["steps"]):
                raise ValueError
            if tx["status"] == "completed" and completed != len(tx["steps"]):
                raise ValueError
            if tx["status"] == "failed" and completed != 0:
                raise ValueError
            amount = money(tx["amount"])
            if amount <= 0 or amount != amount.quantize(MILLI):
                raise ValueError
            if tx["status"] == "resolved" and not tx.get("resolution_note"):
                raise ValueError
        state["version"] = 2
        return state
    except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation, UserError):
        raise StateError("状态文件结构异常，插件停止操作；请管理员检查并恢复备份。") from None


def save_state(state: dict, path=None):
    path = os.path.abspath(path or STATE_FILE)
    directory = os.path.dirname(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory,
            prefix=".sub2api-state-", suffix=".tmp", delete=False,
        ) as f:
            temporary = f.name
            json.dump(state, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except (OSError, ValueError, TypeError):
        raise StateError("状态保存失败，已暂停插件操作；请管理员检查存储并核对未完成流水。") from None
    finally:
        if temporary and os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


class Sub2ApiClient:
    def __init__(self, base_url: str, email: str, password: str):
        self.base = base_url.rstrip("/")
        parsed = urlsplit(self.base)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("sub2api API 地址格式错误。")
        self.email = email
        self.password = password
        self._token = ""
        self._token_exp = 0.0
        self._login_lock = asyncio.Lock()

    async def _login(self):
        if not self.email or not self.password:
            raise ApiError("尚未配置 sub2api 管理员凭据，请联系管理员完成插件配置。", category="auth_not_sent")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base}/auth/login",
                    json={"email": self.email, "password": self.password},
                    timeout=aiohttp.ClientTimeout(total=10), allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        raise ApiError("sub2api 管理员登录失败，请管理员检查配置。", response.status, "auth_not_sent")
                    body = await response.json()
                    data = body["data"]
                    token = data["access_token"]
                    lifetime = int(data.get("expires_in", 86400))
                    if not isinstance(token, str) or not token or lifetime <= 0:
                        raise ValueError
        except ApiError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, TypeError, KeyError):
            raise ApiError("sub2api 管理员登录失败，请管理员检查服务与配置。", category="auth_not_sent") from None
        self._token = token
        self._token_exp = time.time() + min(lifetime, 86400) - min(60, lifetime / 10)

    async def _token_valid(self) -> str:
        async with self._login_lock:
            if not self._token or time.time() >= self._token_exp:
                await self._login()
            return self._token

    async def _req(self, method: str, path: str, body=None, params=None, headers=None) -> tuple[int, dict]:
        token = await self._token_valid()
        is_write = method.upper() != "GET"
        for attempt in (1, 2):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.request(
                        method, f"{self.base}{path}", json=body, params=params,
                        headers={**(headers or {}), "Authorization": f"Bearer {token}"},
                        timeout=aiohttp.ClientTimeout(total=15), allow_redirects=False,
                    ) as response:
                        code = response.status
                        if code == 401 and attempt == 1:
                            self._token = ""
                            token = await self._token_valid()
                            continue
                        if code != 200:
                            # 此完整错误来自已核验的原子条件 UPDATE 未命中分支。
                            # 其他 500（包括更新后读取失败）仍必须保留未知结果。
                            if is_write and code == 500:
                                try:
                                    error = await response.json()
                                except (aiohttp.ClientError, ValueError, TypeError):
                                    error = None
                                message = error.get("message") if isinstance(error, dict) else None
                                if isinstance(message, str) and re.fullmatch(
                                    r"balance cannot be negative, current balance: -?\d+\.\d{2}, "
                                    r"requested operation would result in: -?\d+\.\d{2}", message,
                                ):
                                    raise ApiError("账号余额不足，本次余额操作未执行，请稍后重试。",
                                                   code, "balance_negative_rejected")
                            if is_write and code not in (400, 401, 403, 405, 413, 415, 422, 429):
                                raise OutcomeUnknown(code=code, category="http_unconfirmed")
                            raise ApiError(f"sub2api 请求被拒绝（HTTP {code}），请稍后重试或联系管理员。",
                                           code, "http_rejected")
                        data = await response.json()
                        if not isinstance(data, dict):
                            raise ValueError
                        return code, data
            except ApiError:
                raise
            except aiohttp.ClientConnectorError:
                # 连接尚未建立，当前这一次请求不可能到达余额处理器。
                raise ApiError("无法连接余额服务，本次操作未执行，请稍后重试。",
                               category="connector_not_sent") from None
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, TypeError):
                if is_write:
                    raise OutcomeUnknown(category="transport_or_response_unconfirmed") from None
                raise ApiError() from None
        raise ApiError("sub2api 身份验证失败，请管理员检查配置。", 401)

    async def find_user_by_email(self, email: str) -> dict | None:
        email = normalize_email(email)
        page = 1
        while True:
            _, body = await self._req(
                "GET", "/admin/users",
                params={"page": page, "page_size": 100, "search": email},
            )
            data = body.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise ApiError("sub2api 用户检索响应异常，请联系管理员。")
            items = data["items"]
            for user in items:
                if isinstance(user, dict) and str(user.get("email", "")).strip().lower() == email:
                    return user
            total = data.get("total")
            if len(items) < 100 or (type(total) is int and page * 100 >= total):
                return None
            if page >= 20:
                raise ApiError("邮箱搜索结果过多，请管理员检查账号数据。")
            page += 1

    async def get_user(self, uid: int) -> dict:
        _, body = await self._req("GET", f"/admin/users/{uid}")
        user = body.get("data")
        if not isinstance(user, dict) or not _positive_id(user.get("id")) or user["id"] != uid or "balance" not in user:
            raise ApiError("sub2api 用户查询响应异常，请联系管理员。")
        return user

    async def balance_op(self, uid: int, amount, op: str, notes: str) -> dict:
        value = money(amount)
        if (not _positive_id(uid) or op not in ("add", "subtract") or value <= 0
                or value != value.quantize(MILLI)):
            raise ApiError("余额操作参数无效，未发送请求。", category="validation_rejected")
        key = operation_key(uid, value, op, notes)
        _, body = await self._req(
            "POST", f"/admin/users/{uid}/balance",
            {"balance": float(value), "operation": op, "notes": notes},
            headers={"Idempotency-Key": key},
        )
        user = body.get("data")
        try:
            if not isinstance(user, dict) or not _positive_id(user.get("id")) or user["id"] != uid or "balance" not in user:
                raise ValueError
            money(user["balance"])
        except (UserError, ValueError):
            raise OutcomeUnknown("余额操作返回数据异常，请管理员核账。",
                                 category="response_invalid") from None
        return user

    async def lookup_balance_operation(self, uid, amount, op, notes, created_at):
        """只读查找原操作的唯一账务记录；没有记录不能证明未执行。"""
        if (not _positive_id(uid) or op not in ("add", "subtract")
                or not isinstance(notes, str) or not notes
                or type(created_at) not in (int, float) or not math.isfinite(created_at)):
            raise ApiError(category="evidence_conflict")
        expected = money(amount) * (1 if op == "add" else -1)
        found = None
        for page in range(1, 21):
            try:
                _, body = await self._req("GET", f"/admin/users/{uid}/balance-history",
                                          params={"type": "admin_balance", "page": page, "page_size": 100})
            except ApiError:
                raise ApiError("暂时无法获取账务核对证据。", category="evidence_unavailable") from None
            data = body.get("data")
            if not isinstance(data, dict):
                raise ApiError(category="evidence_unavailable")
            items, total = data.get("items"), data.get("total")
            pages, page_size = data.get("pages"), data.get("page_size")
            if (not isinstance(items, list) or type(total) is not int or total < 0
                    or type(data.get("page")) is not int or data["page"] != page
                    or type(page_size) is not int or not 1 <= page_size <= 100
                    or type(pages) is not int or pages != max(1, math.ceil(total / page_size))
                    or pages > 20 or len(items) != min(page_size, max(0, total - (page - 1) * page_size))):
                raise ApiError(category="evidence_unavailable")
            for item in items:
                if not isinstance(item, dict):
                    raise ApiError(category="evidence_unavailable")
                if item.get("notes") != notes:
                    continue
                try:
                    stamp = datetime.fromisoformat((item.get("used_at") or item["created_at"]).replace("Z", "+00:00"))
                    valid = (found is None and _positive_id(item.get("id"))
                             and type(item.get("used_by")) is int and item["used_by"] == uid
                             and item.get("type") == "admin_balance" and item.get("status") == "used"
                             and abs(money(item.get("value")) - expected) <= Decimal("0.0000001")
                             and stamp.tzinfo is not None
                             and created_at - 5 <= stamp.timestamp() <= time.time() + 60)
                except (KeyError, ValueError, TypeError, AttributeError, UserError, OverflowError):
                    valid = False
                if not valid:
                    raise ApiError("账务证据不一致，请管理员核对。", category="evidence_conflict")
                found = item
            if page >= pages:
                return found
        raise ApiError(category="evidence_unavailable")

    async def balance(self, uid: int) -> Decimal:
        return money((await self.get_user(uid))["balance"])

    async def get_ops(self, path: str, params: dict | None = None):
        """读取运维监控数据（只读，不参与账务锁）。"""
        _, body = await self._req("GET", path, params=params)
        data = body.get("data")
        if not isinstance(data, dict):
            raise ApiError("sub2api 监控数据响应异常，请联系管理员。")
        return data

    async def get_ops_list(self, path: str) -> list:
        """读取运维数据列表接口（只读）。"""
        _, body = await self._req("GET", path)
        data = body.get("data")
        if not isinstance(data, list):
            raise ApiError("sub2api 监控数据响应异常，请联系管理员。")
        return data


async def collect_group_success_rates(client: Sub2ApiClient, name_filter: str = "") -> str:
    """收集启用分组近一小时成功率。输出为白名单脱敏文本：
    只有分组名与成功百分比，绝不包含账号数量、请求量、吞吐等规模数据。"""
    defs = await client.get_ops_list("/admin/groups/all")
    enabled = [g for g in defs if isinstance(g, dict)
               and g.get("status") == "active" and _positive_id(g.get("id"))]
    keyword = (name_filter or "").strip().lower()
    if keyword:
        enabled = [g for g in enabled if keyword in str(g.get("name", "")).lower()]
    rows = []
    for group in enabled:
        name = str(group.get("name") or group["id"])
        try:
            snapshot = await client.get_ops(
                "/admin/ops/dashboard/snapshot-v2", {"group_id": group["id"]})
        except ApiError:
            rows.append((name, None, False))
            continue
        overview = snapshot.get("overview")
        overview = overview if isinstance(overview, dict) else {}
        try:
            total = int(overview.get("request_count_total"))
            success = int(overview.get("success_count"))
        except (TypeError, ValueError):
            rows.append((name, None, False))
            continue
        if total <= 0:
            rows.append((name, None, False))
            continue
        rate = round(success * 100.0 / total, 1)
        rows.append((name, rate, total < 20))
    rows.sort(key=lambda r: (r[1] is None, -(r[1] or 0.0)))
    lines = [
        f"{name}:近一小时无请求或数据暂缺" if rate is None
        else f"{name}:{rate:.1f}%" + ("(样本少)" if sparse else "")
        for name, rate, sparse in rows
    ]
    header = "启用分组近一小时请求成功率（仅百分比，不含账号数量、请求量、吞吐等其他口径）："
    if not lines:
        return header + "\n（没有匹配的启用分组）"
    return header + "\n" + "\n".join(lines)


# ---------------- /状态 分组状态卡片渲染(仅公开分组,浅色卡片网格) ----------------

_PAGE_BG = (238, 241, 240)
_CARD_BG = (255, 255, 255)
_CARD_BORDER = (226, 229, 228)
_TILE_BG = (243, 245, 244)
_TEXT = (24, 30, 38)
_MUTED = (112, 120, 128)
_GREEN = (22, 130, 66)
_GREEN_BG = (231, 244, 235)
_AMBER = (176, 120, 14)
_AMBER_BG = (248, 240, 220)
_RED = (200, 50, 50)
_RED_BG = (250, 232, 232)
_GRAY_PILL = (235, 237, 236)
_BLUE = (47, 90, 180)
_BLUE_BG = (230, 238, 250)
_BAR_OK = (52, 168, 83)
_BAR_FAIL = (220, 68, 60)
_BAR_NONE = (209, 213, 212)

_FONT_BOLD = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]
_FONT_REG = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
]


def _card_font(size: int, bold: bool = True):
    """加载 Noto CJK 字体并优先选中简体(SC)字形集合。"""
    fallback = None
    for path in (_FONT_BOLD if bold else _FONT_REG):
        for index in range(5):
            try:
                font = ImageFont.truetype(path, size, index=index)
            except OSError:
                break
            name = " ".join(font.getname())
            if "SC" in name or "简" in name:
                return font
            if fallback is None:
                fallback = font
    if fallback is not None:
        return fallback
    raise UserError("监控卡片渲染字体缺失，请联系管理员安装 Noto CJK 字体。")


def _pct_color(pct):
    if pct is None:
        return _MUTED
    return _GREEN if pct >= 95 else (_AMBER if pct >= 70 else _RED)


async def build_group_rows(client: Sub2ApiClient) -> list:
    """收集公开启用分组的展示数据；专属(is_exclusive)与停用分组一律不收集。"""
    defs = await client.get_ops_list("/admin/groups/all")
    availability = (await client.get_ops("/admin/ops/account-availability")).get("group")
    availability = availability if isinstance(availability, dict) else {}
    rows = []
    for group in defs:
        if (not isinstance(group, dict) or group.get("status") != "active"
                or group.get("is_exclusive")):
            continue
        gid = group.get("id")
        if not _positive_id(gid):
            continue
        status_row = availability.get(str(gid))
        status_row = status_row if isinstance(status_row, dict) else {}
        try:
            snapshot = await client.get_ops(
                "/admin/ops/dashboard/snapshot-v2", {"group_id": gid})
        except ApiError:
            snapshot = {}
        overview = snapshot.get("overview") if isinstance(snapshot, dict) else None
        overview = overview if isinstance(overview, dict) else {}
        try:
            request_total = int(overview.get("request_count_total") or 0)
            success = int(overview.get("success_count") or 0)
            rate = round(success * 100.0 / request_total, 1) if request_total > 0 else None
            health = int(overview.get("health_score"))
        except (TypeError, ValueError):
            rate = None
            health = None
        cells = []
        if isinstance(snapshot, dict):
            trend = snapshot.get("throughput_trend")
            throughput = trend.get("points") if isinstance(trend, dict) else []
            errors = snapshot.get("error_trend")
            error_points = errors.get("points") if isinstance(errors, dict) else []
            throughput = throughput if isinstance(throughput, list) else []
            error_points = error_points if isinstance(error_points, list) else []
            for i in range(max(len(throughput), len(error_points))):
                request = (throughput[i].get("request_count", 0)
                           if i < len(throughput) and isinstance(throughput[i], dict) else 0)
                failed = (error_points[i].get("error_count_total", 0)
                          if i < len(error_points) and isinstance(error_points[i], dict) else 0)
                cells.append("fail" if failed else ("ok" if request else "none"))
        rows.append({
            "platform": str(group.get("platform") or "-"),
            "name": str(group.get("name") or gid),
            "multiplier": group.get("rate_multiplier"),
            "total": status_row.get("total_accounts", 0) or 0,
            "avail": status_row.get("available_count", 0) or 0,
            "limit": status_row.get("rate_limit_count", 0) or 0,
            "error": status_row.get("error_count", 0) or 0,
            "rate": rate,
            "health": health,
            "cells": cells[:60],
        })
    rows.sort(key=lambda row: row["platform"])
    return rows


def _spaced_label(draw, pos, text, font_obj, fill, extra=2):
    x, y = pos
    for ch in str(text).upper():
        draw.text((x, y), ch, font=font_obj, fill=fill)
        x += draw.textlength(ch, font=font_obj) + extra


def _chip(draw, x, y, text, font_obj, fill):
    width = draw.textlength(text, font=font_obj)
    draw.rounded_rectangle((x, y, x + width + 26, y + 32), radius=16,
                           fill=_CARD_BG, outline=_CARD_BORDER, width=1)
    draw.text((x + 13, y + 7), text, font=font_obj, fill=fill)
    return width + 26 + 8


def _pill(draw, right_x, y, text, fg, bg, font_obj):
    width = draw.textlength(text, font=font_obj)
    draw.rounded_rectangle((right_x - width - 30, y, right_x, y + 26), radius=13, fill=bg)
    draw.ellipse((right_x - width - 21, y + 10, right_x - width - 13, y + 18), fill=fg)
    draw.text((right_x - width - 8, y + 5), text, font=font_obj, fill=fg)


def render_group_cards(rows: list, generated: str) -> str:
    """把公开分组状态渲染为浅色卡片网格 PNG，返回文件路径。"""
    width, margin, gap, pad = 1180, 36, 24, 20
    card_w = (width - 2 * margin - gap) // 2
    card_h = 262
    grid_rows = (len(rows) + 1) // 2
    height = 64 + 84 + grid_rows * card_h + (grid_rows - 1) * gap + 56

    f_chips = _card_font(13, bold=False)
    f_caps = _card_font(12, bold=False)
    f_name = _card_font(23)
    f_badge = _card_font(12, bold=False)
    f_tlabel = _card_font(12, bold=False)
    f_value = _card_font(22)
    f_sub = _card_font(11, bold=False)
    f_hist = _card_font(11, bold=False)
    f_foot = _card_font(12, bold=False)
    f_title = _card_font(26)

    img = Image.new("RGB", (width, height), _PAGE_BG)
    draw = ImageDraw.Draw(img)
    draw.text((margin, 24), "分组状态监控", font=f_title, fill=_TEXT)

    total_groups = len(rows)
    # 只统计分组级状态；渠道数量等集群规模数据一律不展示。
    groups_ok = sum(1 for r in rows if not r["error"] and not r["limit"] and r["total"])
    groups_limit = sum(1 for r in rows if not r["error"] and r["limit"])
    groups_error = sum(1 for r in rows if r["error"])
    x, y = margin, 66
    x = _chip(draw, x, y, f"概览：{total_groups} 分组", f_chips, _TEXT)
    x = _chip(draw, x, y, f"正常 {groups_ok} · 限流 {groups_limit} · 异常 {groups_error}", f_chips, _TEXT)
    y += 40
    x = _chip(draw, margin, y, "周期：近 1 小时", f_chips, _TEXT)
    _chip(draw, x, y, f"更新于 {generated}", f_chips, _MUTED)

    top = 150
    for i, row in enumerate(rows):
        col, line = i % 2, i // 2
        x0 = margin + col * (card_w + gap)
        y0 = top + line * (card_h + gap)
        draw.rounded_rectangle((x0, y0, x0 + card_w, y0 + card_h), radius=16,
                               fill=_CARD_BG, outline=_CARD_BORDER, width=1)
        ix, iy = x0 + pad, y0 + pad

        _spaced_label(draw, (ix, iy), row["platform"], f_caps, _MUTED)
        name_y = iy + 20
        draw.text((ix, name_y), row["name"], font=f_name, fill=_TEXT)
        if row["error"]:
            _pill(draw, x0 + card_w - pad, name_y - 2, "异常", _RED, _RED_BG, f_badge)
        elif row["limit"]:
            _pill(draw, x0 + card_w - pad, name_y - 2, "限流", _AMBER, _AMBER_BG, f_badge)
        elif row["total"] == 0:
            _pill(draw, x0 + card_w - pad, name_y - 2, "无渠道", _MUTED, _GRAY_PILL, f_badge)
        else:
            _pill(draw, x0 + card_w - pad, name_y - 2, "正常", _GREEN, _GREEN_BG, f_badge)

        badge_y = name_y + 38
        multiplier = row["multiplier"]
        if isinstance(multiplier, (int, float)):
            badge_text = f"倍率 {multiplier}x"
            badge_w = draw.textlength(badge_text, font=f_badge)
            draw.rounded_rectangle((ix, badge_y, ix + badge_w + 18, badge_y + 22),
                                   radius=5, fill=_BLUE_BG)
            draw.text((ix + 9, badge_y + 4), badge_text, font=f_badge, fill=_BLUE)

        tile_y = badge_y + 36
        tile_w = (card_w - 2 * pad - 2 * 10) // 3
        avail_pct = round(row["avail"] * 100.0 / row["total"], 1) if row["total"] else None
        health = row["health"]
        tiles = (
            ("渠道可用", f"{avail_pct:.1f}%" if avail_pct is not None else "—",
             "", _pct_color(avail_pct)),
            ("成功率", f"{row['rate']:.1f}%" if row["rate"] is not None else "—",
             "近 1 小时", _pct_color(row["rate"])),
            ("健康分", str(health) if health is not None else "—",
             "0 - 100", _GREEN if (health or 0) >= 80 else (_AMBER if (health or 0) >= 50 else _RED)),
        )
        for j, (label, value, sub, color) in enumerate(tiles):
            tx = ix + j * (tile_w + 10)
            draw.rounded_rectangle((tx, tile_y, tx + tile_w, tile_y + 74), radius=10, fill=_TILE_BG)
            draw.text((tx + 12, tile_y + 10), label, font=f_tlabel, fill=_MUTED)
            draw.text((tx + 12, tile_y + 28), value, font=f_value, fill=color)
            draw.text((tx + 12, tile_y + 55), sub, font=f_sub, fill=_MUTED)

        hist_y = tile_y + 88
        draw.text((ix, hist_y), "HISTORY · 1min/格", font=f_hist, fill=_MUTED)
        bar_y = hist_y + 18
        for j, cell in enumerate(row["cells"]):
            color = {"ok": _BAR_OK, "fail": _BAR_FAIL}.get(cell, _BAR_NONE)
            bx = ix + j * 8
            draw.rounded_rectangle((bx, bar_y, bx + 6, bar_y + 18), radius=2, fill=color)

    foot = "数据来源：sub2api 渠道状态（仅公开分组） · neuro-sama 渲染"
    draw.text(((width - draw.textlength(foot, font=f_foot)) / 2, height - 36),
              foot, font=f_foot, fill=_MUTED)

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "status")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"status_{int(time.time() * 1000)}.png")
    img.save(path, "PNG")
    try:
        olds = sorted(f for f in os.listdir(out_dir)
                      if f.startswith("status_") and f.endswith(".png"))
        for old in olds[:-3]:
            os.unlink(os.path.join(out_dir, old))
    except OSError:
        pass
    return path


@register("astrbot_plugin_sub2api", "zcode", "sub2api 余额互动：自动核账恢复与额度撤回", "1.6.0")
class Sub2ApiPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        cfg = dict(DEFAULT_CONFIG)
        if config:
            for key in DEFAULT_CONFIG:
                if config.get(key) is not None:
                    cfg[key] = config.get(key)
        if type(cfg["robbery_cooldown"]) is not int or cfg["robbery_cooldown"] < 0:
            raise ValueError("robbery_cooldown 必须为非负整数。")
        if type(cfg["allow_bind_admin"]) is not bool:
            raise ValueError("allow_bind_admin 必须为布尔值。")
        if type(cfg["quota_recall_seconds"]) is not int or not 1 <= cfg["quota_recall_seconds"] <= 120:
            raise ValueError("quota_recall_seconds 必须为 1 至 120 的整数。")
        if type(cfg["recovery_enabled"]) is not bool:
            raise ValueError("recovery_enabled 必须为布尔值。")
        if type(cfg["recovery_interval_seconds"]) is not int or not 10 <= cfg["recovery_interval_seconds"] <= 900:
            raise ValueError("recovery_interval_seconds 必须为 10 至 900 的整数。")
        self.cfg = cfg
        self.client = Sub2ApiClient(cfg["base_url"], cfg["admin_email"], cfg["admin_password"])
        self.state_path = STATE_FILE
        self.state = load_state(self.state_path)
        # 所有命令共用一把锁，覆盖绑定、检查、账务请求与状态落盘。
        self._lock = asyncio.Lock()
        self._state_failed = False
        self.quota_recall = QuotaRecallManager(
            context, Path(self.state_path).with_name("pending_recalls.json"),
            delay_seconds=cfg["quota_recall_seconds"],
        )
        self.ledger_recovery = LedgerRecovery(self)

    async def initialize(self):
        await self.quota_recall.initialize()
        await self.ledger_recovery.initialize()

    @filter.on_platform_loaded()
    async def recall_platform_ready(self):
        await self.quota_recall.initialize()

    async def terminate(self):
        await self.ledger_recovery.terminate()
        await self.quota_recall.terminate()

    def _save(self):
        try:
            save_state(self.state, self.state_path)
        except StateError:
            self._state_failed = True
            raise

    def _qq(self, event: AstrMessageEvent) -> str:
        qq = str(event.get_sender_id() or "")
        if event.get_platform_name() != "aiocqhttp" or not QQ_PATTERN.fullmatch(qq):
            raise UserError("本插件仅支持 OneBot QQ 消息，请使用有效 QQ 账号操作。")
        return qq

    def _binding(self, qq: str) -> dict:
        binding = self.state["bindings"].get(qq)
        if not binding or not binding.get("approved"):
            raise UserError("你尚未完成账号绑定，请使用 /绑定 {邮箱} 完成绑定。")
        return binding

    @staticmethod
    def _args(event, commands):
        parts = (event.message_str or "").split()
        if parts and parts[0].lstrip("/") in commands:
            parts = parts[1:]
        return parts

    def _at_targets(self, event):
        bot_id = str(event.get_self_id())
        targets = []
        for segment in event.message_obj.message:
            if isinstance(segment, At):
                target = str(segment.qq)
                if (target not in ("all", bot_id) and QQ_PATTERN.fullmatch(target)
                        and target not in targets):
                    targets.append(target)
        return targets

    def _remaining_cooldown(self, qq):
        elapsed = time.time() - self.state["robbery_ts"].get(qq, 0)
        return max(0, math.ceil(self.cfg["robbery_cooldown"] - elapsed))

    def _protected(self, qq) -> bool:
        # 防御式读取：load_state 保证结构，但测试与外部脚本可能整体替换 state。
        entry = (self.state.get("protections") or {}).get(qq)
        return entry is not None and entry.get("enabled") is True

    @staticmethod
    def _rand_amount() -> Decimal:
        return Decimal(random.randint(100, 500)) / 1000

    def _pending(self, uids):
        return next((tx for tx in self.state["transactions"].values()
                     if tx["status"] == "pending" and set(tx["uids"]).intersection(uids)), None)

    @staticmethod
    def _pending_message(tx):
        return f"账务待核对（流水 {tx['id']}），余额操作暂缓；系统自动核对中，必要时联系管理员。"

    def _require_clear(self, *uids):
        pending = self._pending(uids)
        if pending:
            raise UserError(self._pending_message(pending))

    def _validate_account(self, user, for_write=False):
        if not isinstance(user, dict) or not _positive_id(user.get("id")):
            raise UserError("账号数据异常，请联系管理员检查。")
        if user.get("status") != "active":
            raise UserError("账号当前不是正常启用状态，无法操作。")
        role = user.get("role")
        if role not in ("user", "admin") or (role == "admin" and not self.cfg["allow_bind_admin"]):
            raise UserError("该账号角色不允许参与余额互动，请联系管理员。")
        balance = money(user.get("balance"))
        frozen = money(user.get("frozen_balance") or 0)
        if for_write and frozen > 0:
            raise UserError("账号存在冻结余额，暂不能参与签到或打劫，请联系管理员。")
        return balance

    async def _account(self, binding, for_write=False):
        try:
            user = await self.client.get_user(binding["uid"])
        except ApiError as exc:
            if exc.code == 404:
                raise UserError("绑定账号已不存在，请使用 /绑定 {邮箱} 重新绑定。") from None
            raise
        self._validate_account(user, for_write)
        if (user["id"] != binding["uid"]
                or normalize_email(user.get("email", "")) != binding["email"]):
            raise UserError("绑定账号身份发生变化，请使用 /绑定 {邮箱} 重新绑定。")
        return user

    def _new_tx(self, kind, qq, amount, steps, users, **extra):
        txid = uuid.uuid4().hex
        tx = {
            "id": txid, "kind": kind, "qq": qq,
            "uids": sorted({step["uid"] for step in steps}),
            "amount": f"{amount:.3f}", "steps": steps,
            "balances_before": {str(user["id"]): str(user["balance"]) for user in users},
            "status": "pending", "phase": "intent", "completed_steps": 0,
            "created_at": time.time(), **extra,
        }
        tx["request_keys"] = [operation_key(step["uid"], amount, step["operation"], operation_notes(tx, i))
                              for i, step in enumerate(steps)]
        tx["step_outcomes"] = [{"status": "not_sent"} for _ in steps]
        self.state["transactions"][txid] = tx
        self._save()
        return tx

    async def _execute_tx(self, tx):
        results = []
        for index, step in enumerate(tx["steps"]):
            previous_phase, previous_outcome = tx["phase"], tx["step_outcomes"][index]
            tx["phase"] = f"step_{index + 1}_intent"
            tx["step_outcomes"][index] = {"status": "inflight"}
            try:
                self._save()
            except Exception:
                # 请求还未开始，存储恢复后应保留这个确定事实。
                tx["phase"], tx["step_outcomes"][index] = previous_phase, previous_outcome
                raise
            try:
                user = await self.client.balance_op(
                    step["uid"], Decimal(tx["amount"]), step["operation"],
                    operation_notes(tx, index),
                )
            except OutcomeUnknown as exc:
                tx["phase"] = f"step_{index + 1}_unknown"
                tx["step_outcomes"][index] = {"status": "unknown", "category": exc.category, "code": exc.code}
                self._save()
                raise UserError(self._pending_message(tx)) from None
            except ApiError as exc:
                tx["phase"] = f"step_{index + 1}_rejected"
                tx["failure_code"] = exc.code
                tx["step_outcomes"][index] = {"status": "not_applied", "category": exc.category, "code": exc.code}
                if tx["completed_steps"] == 0:
                    tx["status"] = "failed"
                    if tx["kind"].startswith("rob_"):
                        self.state["robbery_ts"].pop(tx["qq"], None)
                self._save()
                if tx["status"] == "pending":
                    raise UserError(self._pending_message(tx)) from None
                raise ApiError("余额操作未执行，请稍后重试或联系管理员。", exc.code) from None
            except Exception:
                tx["phase"] = f"step_{index + 1}_unknown"
                tx["step_outcomes"][index] = {"status": "unknown", "category": "unexpected_error"}
                self._save()
                raise UserError(self._pending_message(tx)) from None
            results.append(user)
            tx["completed_steps"] = index + 1
            tx["phase"] = f"step_{index + 1}_applied"
            tx["step_outcomes"][index] = {"status": "applied"}
            self._save()
        return results

    def _complete_tx(self, tx):
        if tx["kind"] == "checkin":
            qq, uid, day = tx["qq"], str(tx["uids"][0]), tx["date"]
            self.state["checkin"][qq] = max(self.state["checkin"].get(qq, ""), day)
            self.state["checkin_uid"][uid] = max(self.state["checkin_uid"].get(uid, ""), day)
        tx["status"] = "completed"
        tx["phase"] = "committed"
        tx["completed_at"] = time.time()
        self._save()

    async def _run(self, event, action):
        try:
            async with self._lock:
                if self._state_failed:
                    raise StateError("状态存储异常，插件已暂停操作，请管理员检查并核对未完成流水。")
                qq = self._qq(event)
                return await action(event, qq)
        except UserError as exc:
            return str(exc)
        except Exception:
            return "服务暂时不可用，请联系管理员检查。"

    @filter.custom_filter(SlashCommandFilter, priority=-10000)
    async def slash_command_fallback(self, event: AstrMessageEvent):
        """Keep QQ slash commands in plugin handling, including unknown commands."""
        try:
            # Normal command handlers run first. Do not duplicate their replies.
            if len(event.get_extra("activated_handlers") or []) <= 1:
                yield event.plain_result(
                    "未知指令，发送 /help 查看用法。"
                )
        finally:
            event.stop_event()

    @filter.command("绑定", alias={"bind"})
    async def bind(self, event: AstrMessageEvent):
        """直接绑定 sub2api 账号：/绑定 邮箱。"""
        try:
            yield event.plain_result(await self._run(event, self._bind))
        finally:
            event.stop_event()

    async def _bind(self, event, qq):
        args = self._args(event, ("绑定", "bind"))
        if len(args) != 1:
            raise UserError("用法：/绑定 {邮箱}，例如 /绑定 name@example.com。")
        email = normalize_email(args[0])
        await self._bind_account(qq, email, qq)
        return "绑定成功！可用 /签到 /打劫 /查询。"

    @filter.command("解绑", alias={"unbind"})
    async def unbind(self, event: AstrMessageEvent):
        """解除当前 QQ 的账号绑定：/解绑。有待核对流水时暂不可解绑。"""
        try:
            yield event.plain_result(await self._run(event, self._unbind))
        finally:
            event.stop_event()

    async def _unbind(self, event, qq):
        binding = self._binding(qq)
        # 该 uid 存在待核对流水（含作为打劫对手方）时拒绝解绑，绑定保持不变。
        self._require_clear(binding["uid"])
        email = binding["email"]
        # 保留 checkin/checkin_uid/robbery_ts/protections：防止解绑重绑刷签到，冷却与保护意愿延续。
        self.state["bindings"].pop(qq, None)
        self._save()
        return f"已解绑 {email}。当日签到记录保留，重新绑定用 /绑定 邮箱。"

    @filter.command("保护", alias={"免打劫"})
    async def protect(self, event: AstrMessageEvent):
        """切换免打劫保护：/保护。开启后不能打劫别人，也不会被打劫。"""
        try:
            yield event.plain_result(await self._run(event, self._protect))
        finally:
            event.stop_event()

    async def _protect(self, event, qq):
        self._binding(qq)
        enabled = not self._protected(qq)
        self.state.setdefault("protections", {})[qq] = {
            "enabled": enabled, "updated_at": time.time(),
        }
        self._save()
        if enabled:
            return "免打劫保护已开启：不能打劫别人，也不会被打劫。再发 /保护 关闭。"
        return "免打劫保护已关闭。"

    @filter.command("确认绑定")
    async def confirm_bind(self, event: AstrMessageEvent):
        """兼容旧版申请，仅 AstrBot 管理员：/确认绑定 QQ 邮箱。"""
        try:
            yield event.plain_result(await self._run(event, self._confirm_bind))
        finally:
            event.stop_event()

    async def _confirm_bind(self, event, qq):
        if not event.is_admin():
            raise UserError("只有 AstrBot 管理员可以确认绑定。")
        args = self._args(event, ("确认绑定",))
        if len(args) != 2 or not QQ_PATTERN.fullmatch(args[0]):
            raise UserError("用法：/确认绑定 {QQ号} {邮箱}；执行前请先核实账号归属。")
        target, email = args[0], normalize_email(args[1])
        request = self.state["binding_requests"].get(target)
        if not request or request["email"] != email:
            raise UserError("没有匹配的旧版绑定申请。用户可直接使用 /绑定 {邮箱} 完成绑定。")
        await self._bind_account(target, email, qq)
        return f"已确认 QQ {target} 绑定。"

    async def _bind_account(self, target, email, bound_by):
        user = await self.client.find_user_by_email(email)
        if not user:
            raise UserError("未找到该邮箱对应的 sub2api 账号。")
        self._validate_account(user)
        if normalize_email(user.get("email", "")) != email:
            raise UserError("账号邮箱不匹配，无法绑定。")
        old = self.state["bindings"].get(target)
        self._require_clear(user["id"], *([old["uid"]] if old else []))
        if any(other != target and b.get("approved") and b["uid"] == user["id"]
               for other, b in self.state["bindings"].items()):
            raise UserError("该 sub2api 账号已经绑定其他 QQ，不能重复绑定。")
        self.state["bindings"][target] = {
            "uid": user["id"], "email": email, "approved": True,
            "bound_by": bound_by, "bound_at": time.time(),
        }
        self.state["binding_requests"].pop(target, None)
        self._save()

    @filter.command("签到", alias={"checkin"})
    async def checkin(self, event: AstrMessageEvent):
        try:
            result = await self._run(event, self._checkin)
            yield event.plain_result(await self._quota_result_text(event, result))
        finally:
            event.stop_event()

    async def _quota_result_text(self, event, result):
        if not isinstance(result, QuotaReply):
            return result
        scheduled = await self.quota_recall.send(event, result.quota_text)
        text = result.public_text
        if not scheduled:
            text += "\n额度明细展示或撤回安排异常，请联系管理员核查。"
        return text

    async def _checkin(self, event, qq):
        binding = self._binding(qq)
        uid = binding["uid"]
        # 与打劫相同，先读取并拦截负余额，避免签到加款请求触发上游拒绝后留下未知流水。
        before = await self._account(binding, for_write=True)
        if money(before["balance"]) < 0:
            raise UserError("你的余额为负，暂不能签到；本次未扣款，不计入签到记录。")
        self._require_clear(uid)
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        if self.state["checkin"].get(qq) == today or self.state["checkin_uid"].get(str(uid)) == today:
            raise UserError("今天已经签过了，明天再来吧~")
        amount = self._rand_amount()
        tx = self._new_tx("checkin", qq, amount, [{"uid": uid, "operation": "add"}], [before], date=today)
        user = (await self._execute_tx(tx))[0]
        self.state["checkin"][qq] = today
        self.state["checkin_uid"][str(uid)] = today
        self._complete_tx(tx)
        return QuotaReply(
            "签到成功！",
            f"奖励 {amount:.3f}，总额度 {money(user['balance']):.3f}"
            f"（{self.cfg['quota_recall_seconds']} 秒后自动撤回）",
        )

    @filter.command("打劫", alias={"rob"})
    async def rob(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(await self._run(event, self._rob))
        finally:
            event.stop_event()

    async def _rob(self, event, qq):
        binding = self._binding(qq)
        targets = self._at_targets(event)
        if len(targets) != 1:
            raise UserError("用法：/打劫 @某人（仅限一位）。")
        target = targets[0]
        if target == qq:
            raise UserError("不能打劫自己啦！")
        other = self.state["bindings"].get(target)
        if not other or not other.get("approved"):
            raise UserError("对方尚未绑定账号，请对方先使用 /绑定 {邮箱} 完成绑定。")
        if binding["uid"] == other["uid"]:
            raise UserError("两个 QQ 指向同一账号，不能互相打劫。")
        # 免打劫保护是纯本地开关：在任何账号读取之前拦截，不消耗冷却、不产生流水。
        if self._protected(qq):
            raise UserError("你已开启免打劫保护，先发 /保护 关闭后再打劫。")
        if self._protected(target):
            raise UserError("对方已开启免打劫保护，无法对其打劫。")
        # 预检必须先于已有 pending 锁：旧流水不能遮蔽当前账号的负余额原因。
        # `_account` 只做读取和账号状态校验；后续仍会由 `_require_clear` 阻止任何新写入。
        robber = await self._account(binding, for_write=True)
        if money(robber["balance"]) < 0:
            raise UserError("你的余额为负，暂不能打劫；本次未扣款，不计入冷却。")
        if money(robber["balance"]) < Decimal("0.500"):
            raise UserError("你的余额不足以承担 0.500 赔款，暂不能打劫；本次未扣款，不计入冷却。")
        victim = await self._account(other, for_write=True)
        if money(victim["balance"]) < 0:
            raise UserError("对方余额为负，暂不能打劫；本次未扣款，不计入冷却。")
        # 先完成负余额预检，再应用已有流水锁和冷却提示。
        # 这样旧的待核对流水不会遮蔽当前账号的负余额原因；预检只读，不会绕过流水锁。
        self._require_clear(binding["uid"], other["uid"])
        remaining = self._remaining_cooldown(qq)
        if remaining:
            raise UserError(f"打劫冷却中，还剩 {remaining} 秒，先歇歇~")
        failed = random.random() < 0.3
        if failed:
            amount = Decimal("0.500")
            source, destination = binding["uid"], other["uid"]
        else:
            available = max(Decimal(0), money(victim["balance"])).quantize(MILLI, rounding=ROUND_DOWN)
            amount = min(self._rand_amount(), available)
            source, destination = other["uid"], binding["uid"]
        self.state["robbery_ts"][qq] = time.time()
        if amount <= 0:
            self._save()
            return "打劫成功！对方无可转移余额，本次获得 0.000。"
        tx = self._new_tx(
            "rob_failure" if failed else "rob_success", qq, amount,
            [{"uid": source, "operation": "subtract"}, {"uid": destination, "operation": "add"}],
            [robber, victim], target_qq=target,
        )
        await self._execute_tx(tx)
        self._complete_tx(tx)
        if failed:
            return f"打劫失败！向 {other['email']} 赔偿了 0.500。"
        return f"打劫成功！从 {other['email']} 抢到 {amount:.3f}。"

    @filter.command("查询", alias={"balance", "余额"})
    async def query(self, event: AstrMessageEvent):
        try:
            result = await self._run(event, self._query)
            yield event.plain_result(await self._quota_result_text(event, result))
        finally:
            event.stop_event()

    async def _query(self, event, qq):
        binding = self._binding(qq)
        user = await self._account(binding)
        result = f"已绑定 {binding['email']}，状态正常"
        pending = self._pending((binding["uid"],))
        if pending:
            result += "\n" + self._pending_message(pending)
        return QuotaReply(
            result,
            f"当前余额：{money(user['balance']):.3f}"
            f"（{self.cfg['quota_recall_seconds']} 秒后自动撤回）",
        )

    @filter.command("状态", alias={"status"})
    async def status(self, event: AstrMessageEvent):
        """渲染 sub2api 公开分组状态卡片并发送图片：/状态。只读操作，无需绑定。"""
        try:
            try:
                # 只读指令：不占用全局账务锁，不要求绑定；仅收集公开启用分组。
                rows = await build_group_rows(self.client)
                image_path = render_group_cards(
                    rows, datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"))
            except UserError as exc:
                yield event.plain_result(str(exc))
                return
            except Exception:
                yield event.plain_result("监控卡片生成失败，请稍后重试或联系管理员。")
                return
            yield event.image_result(image_path)
        finally:
            event.stop_event()

    @filter.llm_tool(name="query_group_success_rate")
    async def query_group_success_rate(self, event: AstrMessageEvent, group_name: str = ""):
        """查询各渠道分组近一小时的请求成功率。当用户询问分组或渠道的服务质量、成功率、稳定性、哪个分组不稳定时调用。返回内容只有分组名称和成功百分比，不包含账号数量、请求量、吞吐量等规模数据。返回数值仅供口头总结，不要编造未提供的数据。

        Args:
            group_name(string): 可选。分组名称关键词，只返回名称包含该关键词的分组；留空或空字符串返回全部启用分组。
        """
        keyword = (group_name or "").strip().lower()
        cached = getattr(self, "_rate_cache", None)
        if cached and cached[0] == keyword and time.time() - cached[1] < 60:
            return cached[2]
        try:
            text = await collect_group_success_rates(self.client, group_name)
        except UserError as exc:
            return f"查询失败：{exc}"
        except Exception:
            return "查询失败，请稍后再试。"
        self._rate_cache = (keyword, time.time(), text)
        return text
