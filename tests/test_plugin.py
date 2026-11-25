"""Offline command and durable-state simulations; no real account or network access."""
import asyncio
import contextlib
import copy
import importlib.util
import json
import re
import sys
import tempfile
import types
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image, ImageDraw, ImageFont


def load_plugin():
    modules = {name: types.ModuleType(name) for name in (
        "astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star",
        "astrbot.api.message_components",
    )}

    def decorator(*args, **kwargs):
        return lambda target: target

    class Star:
        def __init__(self, context):
            self.context = context

    class At:
        def __init__(self, qq):
            self.qq = qq

    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].filter = types.SimpleNamespace(
        command=decorator, custom_filter=decorator, CustomFilter=object,
        on_platform_loaded=decorator, llm_tool=decorator,
    )
    modules["astrbot.api.star"].Star = Star
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].register = decorator
    modules["astrbot.api.message_components"].At = At
    source = Path(__file__).resolve().parents[1]
    package_name = "_sub2api_command_simulation"
    package = types.ModuleType(package_name)
    package.__path__ = [str(source)]
    spec = importlib.util.spec_from_file_location(package_name + ".main", source / "main.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {**modules, package_name: package, spec.name: module}):
        spec.loader.exec_module(module)
    return module


plugin = load_plugin()


class FakeQuotaRecallManager:
    """Capture wallet-to-recall handoff; transport timing has separate tests."""

    def __init__(self):
        self.messages = []
        self.send_result = True
        self.initialize_calls = 0
        self.terminate_calls = 0

    async def send(self, event, text):
        if event.stopped:
            raise AssertionError("Quota details must be sent before stopping the event")
        self.messages.append((event, text))
        return self.send_result

    async def initialize(self):
        self.initialize_calls += 1

    async def terminate(self):
        self.terminate_calls += 1


class Event:
    def __init__(self, qq="10001", text="", targets=(), admin=False, platform="aiocqhttp"):
        self.qq, self.message_str, self.admin, self.platform = qq, text, admin, platform
        self.message_obj = types.SimpleNamespace(message=[plugin.At(q) for q in targets], message_str=text)
        self.is_at_or_wake_command = False
        self.extras = {}
        self.stopped = False

    def get_extra(self, key):
        return self.extras.get(key)

    def get_sender_id(self):
        return self.qq

    def get_self_id(self):
        return "99999"

    def get_platform_name(self):
        return self.platform

    def is_admin(self):
        return self.admin

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text

    def image_result(self, path):
        return f"[image] {path}"


class FakeClient:
    def __init__(self):
        self.users = {
            uid: {"id": uid, "email": f"user{uid}@example.test", "balance": Decimal("2"),
                  "status": "active", "role": "user", "frozen_balance": 0}
            for uid in (1, 2, 3)
        }
        self.operations, self.reads, self.searches, self.plans = [], [], [], []
        self.before_write = None

    async def find_user_by_email(self, email):
        self.searches.append(email)
        await asyncio.sleep(0)
        return next((copy.deepcopy(u) for u in self.users.values() if u["email"] == email), None)

    async def get_user(self, uid):
        self.reads.append(uid)
        await asyncio.sleep(0)
        if uid not in self.users:
            raise plugin.ApiError("account missing", 404)
        return copy.deepcopy(self.users[uid])

    async def balance_op(self, uid, amount, op, notes):
        self.operations.append((uid, amount, op, notes))
        if self.before_write:
            self.before_write(uid, amount, op, notes)
        await asyncio.sleep(0)
        plan = self.plans.pop(0) if self.plans else None
        if plan == "reject":
            raise plugin.ApiError("SIMULATED_PRIVATE_REJECTION", 400)
        if plan == "unknown_before":
            raise plugin.OutcomeUnknown("SIMULATED_PRIVATE_TIMEOUT")
        user = self.users[uid]
        if op == "subtract" and user["balance"] < amount:
            raise plugin.ApiError("insufficient funds", 400)
        user["balance"] += amount if op == "add" else -amount
        if plan == "unknown_after":
            raise plugin.OutcomeUnknown("SIMULATED_PRIVATE_TIMEOUT")
        if plan == "cancel_after":
            raise asyncio.CancelledError
        return copy.deepcopy(user)


class FakeOpsClient:
    """Read-only ops endpoints whose scale fields must never reach users or the LLM."""

    def __init__(self, groups, snapshots=None, availability=None, failing=()):
        self.groups, self.snapshots = groups, snapshots or {}
        self.availability, self.failing = availability or {}, set(failing)
        self.calls = []

    async def get_ops_list(self, path):
        self.calls.append((path, None))
        await asyncio.sleep(0)
        return copy.deepcopy(self.groups)

    async def get_ops(self, path, params=None):
        self.calls.append((path, params))
        await asyncio.sleep(0)
        if path == "/admin/ops/account-availability":
            return {"group": copy.deepcopy(self.availability)}
        if params["group_id"] in self.failing:
            raise plugin.ApiError("SIMULATED_PRIVATE_OPS_FAILURE")
        return copy.deepcopy(self.snapshots.get(params["group_id"], {}))

    def snapshot_ids(self):
        return [params["group_id"] for path, params in self.calls
                if path == "/admin/ops/dashboard/snapshot-v2"]


def ops_snapshot(total, success, health=90, points=()):
    return {
        "overview": {"request_count_total": total, "success_count": success, "health_score": health,
                     "account_count": 37, "qps": 12.5, "token_count": 987654},
        "throughput_trend": {"points": [{"request_count": requests} for requests, _ in points]},
        "error_trend": {"points": [{"error_count_total": errors} for _, errors in points]},
    }


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="sub2api-test-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.json"
        self.state_patch = patch.object(plugin, "STATE_FILE", str(self.path))
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.recall = FakeQuotaRecallManager()
        self.recall_patch = patch.object(plugin, "QuotaRecallManager", return_value=self.recall)
        self.recall_factory = self.recall_patch.start()
        self.addCleanup(self.recall_patch.stop)
        self.bot = plugin.Sub2ApiPlugin(object())
        self.addAsyncCleanup(self.bot.terminate)
        self.client = FakeClient()
        self.bot.client = self.client
        self.bindings()
        self.amount_patch = patch.object(self.bot, "_rand_amount", return_value=Decimal("0.300"))
        self.amount_patch.start()
        self.addCleanup(self.amount_patch.stop)

    def bindings(self):
        self.bot.state["bindings"] = {
            f"1000{uid}": {"uid": uid, "email": f"user{uid}@example.test", "approved": True}
            for uid in (1, 2, 3)
        }

    async def invoke(self, method, event=None, bot=None):
        event = event or Event()
        before_recall = len(self.recall.messages)
        values = []
        async for value in getattr(bot or self.bot, method)(event):
            # The reply must reach AstrBot's send stage before stop_event.
            self.assertFalse(event.stopped)
            values.append(value)
        self.assertTrue(event.stopped)
        self.assertEqual(len(values), 1)
        quota = [text for owner_event, text in self.recall.messages[before_recall:] if owner_event is event]
        return "\n".join([values[0], *quota])

    def reload(self):
        bot = plugin.Sub2ApiPlugin(object())
        bot.client = self.client
        self.addAsyncCleanup(bot.terminate)
        return bot

    def transactions(self):
        return list(self.bot.state["transactions"].values())

    async def test_slash_filter_uses_original_text_and_respects_waking(self):
        event = Event(text="/unknown")
        guard = plugin.SlashCommandFilter()
        self.assertFalse(guard.filter(event, {}))
        event.is_at_or_wake_command = True
        event.message_str = "unknown"  # WakingCheckStage has removed '/'.
        self.assertTrue(guard.filter(event, {}))
        event.message_obj.message_str = "neuro-sama 你好"
        self.assertFalse(guard.filter(event, {}))
        event.message_obj.message_str = "/unknown"
        event.platform = "telegram"
        self.assertFalse(guard.filter(event, {}))

    async def test_unknown_slash_replies_locally_and_stops(self):
        event = Event(text="/unknown")
        event.extras["activated_handlers"] = [object()]
        text = await self.invoke("slash_command_fallback", event)
        self.assertIn("未知指令", text)
        self.assertEqual(self.client.operations, [])
        self.assertEqual(self.client.reads, [])
        self.assertEqual(self.client.searches, [])

    async def test_slash_fallback_does_not_duplicate_another_plugin_reply(self):
        event = Event(text="/handled_elsewhere")
        event.extras["activated_handlers"] = [object(), object()]
        values = [value async for value in self.bot.slash_command_fallback(event)]
        self.assertEqual(values, [])
        self.assertTrue(event.stopped)

    async def test_binding_directly_validates_and_persists_without_approval(self):
        self.bot.state["bindings"] = {}
        self.client.users[1]["email"] = "someone+audit&tag#x@example.test"
        self.bot.state["binding_requests"]["10001"] = {
            "email": "someone+audit&tag#x@example.test", "created_at": 0,
        }
        text = await self.invoke("bind", Event(text="/绑定 Someone+audit&tag#x@example.test"))
        self.assertIn("绑定成功", text)
        self.assertEqual(self.client.searches, ["someone+audit&tag#x@example.test"])
        saved = plugin.load_state(str(self.path))
        self.assertEqual(saved["bindings"]["10001"]["uid"], 1)
        self.assertTrue(saved["bindings"]["10001"]["approved"])
        self.assertEqual(saved["bindings"]["10001"]["bound_by"], "10001")
        self.assertNotIn("10001", saved["binding_requests"])
        self.assertEqual(self.client.operations, [])

    async def test_binding_missing_or_inactive_account_is_rejected(self):
        self.bot.state["bindings"] = {}
        self.assertIn("未找到", await self.invoke("bind", Event(text="/绑定 missing@example.test")))
        self.client.users[1]["status"] = "disabled"
        self.assertIn("不是正常启用", await self.invoke("bind", Event(text="/绑定 user1@example.test")))
        self.assertEqual(self.bot.state["bindings"], {})
        self.assertEqual(self.client.operations, [])

    async def test_binding_admin_role_requires_config_but_not_astrbot_admin(self):
        self.bot.state["bindings"] = {}
        self.client.users[1]["role"] = "admin"
        command = Event(text="/绑定 user1@example.test")
        self.assertIn("角色不允许", await self.invoke("bind", command))
        self.assertEqual(self.bot.state["bindings"], {})
        self.bot.cfg["allow_bind_admin"] = True
        self.assertIn("绑定成功", await self.invoke("bind", Event(text="/绑定 user1@example.test")))
        self.assertTrue(self.bot.state["bindings"]["10001"]["approved"])
        self.assertEqual(self.client.operations, [])

    async def test_binding_rejects_mismatched_email_and_invalid_id(self):
        self.bot.state["bindings"] = {}
        for changes, expected in (({"email": "other@example.test"}, "邮箱不匹配"),
                                  ({"id": 0}, "账号数据异常")):
            with self.subTest(changes=changes), patch.object(
                self.client, "find_user_by_email",
                new=AsyncMock(return_value={**self.client.users[1], **changes}),
            ):
                self.assertIn(expected, await self.invoke("bind", Event(text="/绑定 user1@example.test")))
        self.assertEqual(self.bot.state["bindings"], {})
        self.assertEqual(self.client.operations, [])

    async def test_confirm_requires_astrbot_admin_and_matching_request(self):
        self.bot.state["bindings"] = {}
        self.bot.state["binding_requests"]["10001"] = {"email": "user1@example.test", "created_at": 0}
        command = "/确认绑定 10001 user1@example.test"
        self.assertIn("只有 AstrBot 管理员", await self.invoke("confirm_bind", Event(qq="10003", text=command)))
        self.assertEqual(self.client.searches, [])
        changed = "/确认绑定 10001 user2@example.test"
        self.assertIn("没有匹配", await self.invoke("confirm_bind", Event(qq="10003", text=changed, admin=True)))
        text = await self.invoke("confirm_bind", Event(qq="10003", text=command, admin=True))
        self.assertIn("已确认", text)
        self.assertTrue(self.bot.state["bindings"]["10001"]["approved"])
        self.assertNotIn("10001", self.bot.state["binding_requests"])

    async def test_simultaneous_direct_binding_only_binds_one_qq_per_uid(self):
        self.bot.state["bindings"] = {}
        results = await asyncio.gather(*(
            self.invoke("bind", Event(qq=qq, text="/绑定 user1@example.test"))
            for qq in ("10001", "10002")
        ))
        self.assertEqual(sum("绑定成功" in result for result in results), 1)
        self.assertEqual(sum("已经绑定其他 QQ" in result for result in results), 1)
        self.assertEqual(len(self.bot.state["bindings"]), 1)
        self.assertEqual(self.client.operations, [])

    async def test_direct_binding_consumes_old_request_and_prevents_stale_approval(self):
        self.bot.state["bindings"] = {}
        self.bot.state["binding_requests"]["10001"] = {"email": "user1@example.test", "created_at": 0}
        await self.invoke("bind", Event(text="/绑定 user2@example.test"))
        result = await self.invoke("confirm_bind", Event(qq="10003", text="/确认绑定 10001 user1@example.test", admin=True))
        self.assertIn("没有匹配", result)
        self.assertEqual(self.client.searches, ["user2@example.test"])
        self.assertEqual(self.bot.state["bindings"]["10001"]["uid"], 2)

    async def _pending_checkin(self, plan):
        """产生一条结果未知的待核对签到流水，返回 (tx, 用户收到的提示)。"""
        self.client.plans = [plan]
        reply = await self.invoke("checkin")
        self.assertIn("待核对", reply)
        tx = self.transactions()[0]
        self.assertEqual((tx["status"], tx["kind"]), ("pending", "checkin"))
        return tx, reply

    async def test_audit_requires_admin_usage_and_eligible_transaction(self):
        tx, _ = await self._pending_checkin("unknown_before")
        txid = tx["id"]
        for text in ("/核账", "/核账 abc", "/核账 " + txid + " maybe",
                     "/核账 zzz" + "0" * 29 + " not_applied"):
            with self.subTest(text=text):
                self.assertIn("用法", await self.invoke("audit", Event(admin=True, text=text)))
        self.assertIn("没有找到待核对状态", await self.invoke(
            "audit", Event(admin=True, text="/核账 " + "b" * 32 + " not_applied")))
        # 非管理员拒绝，且零 API 调用、零状态变更
        before = copy.deepcopy(self.bot.state)
        denied = await self.invoke("audit", Event(text="/核账 " + txid + " not_applied"))
        self.assertIn("只有 AstrBot 管理员", denied)
        self.assertEqual(self.bot.state, before)
        # /核账 全程零 API 调用；reads 里的 [1] 仅来自先前那次签到尝试的账号预检。
        self.assertEqual(self.client.reads, [1])
        self.assertEqual(self.client.searches, [])
        self.assertEqual(len(self.client.operations), 1)  # 只有先前那次签到尝试

    async def test_audit_rejects_rob_and_non_unknown_transactions(self):
        self.client.plans = [None, "unknown_after"]
        with patch.object(plugin.random, "random", return_value=0.9):
            await self.invoke("rob", Event(targets=("10002",)))
        rob_tx = self.transactions()[0]
        self.assertEqual(rob_tx["status"], "pending")
        refused = await self.invoke("audit", Event(admin=True, text=f"/核账 {rob_tx['id']} not_applied"))
        self.assertIn("两步资金操作", refused)
        self.assertEqual(rob_tx["status"], "pending")
        # 没有 unknown/inflight 步骤的流水拒绝受理
        manual = {"id": "c" * 32, "kind": "checkin", "qq": "10001", "uids": [1], "date": "2026-10-01",
                  "amount": "0.300", "steps": [{"uid": 1, "operation": "add"}],
                  "balances_before": {"1": "2.000"}, "status": "pending", "phase": "intent",
                  "completed_steps": 0, "created_at": plugin.time.time(),
                  "step_outcomes": [{"status": "not_sent"}]}
        self.bot.state["transactions"][manual["id"]] = manual
        self.assertIn("没有结果未知的步骤", await self.invoke(
            "audit", Event(admin=True, text=f"/核账 {manual['id']} not_applied")))
        self.assertEqual(manual["status"], "pending")

    async def test_audit_not_applied_resolves_and_allows_fresh_checkin(self):
        tx, reply = await self._pending_checkin("unknown_before")
        self.assertNotIn("系统自动核对中", reply)
        self.assertIn("需管理员核对", reply)
        result = await self.invoke("audit", Event(admin=True, text=f"/核账 {tx['id']} not_applied"))
        self.assertIn("未执行", result)
        self.assertEqual(tx["status"], "resolved")
        self.assertTrue(tx["resolution_note"].strip())
        # 不补签到标记；用户可重新 /签到 补领，余额只加一次
        self.assertEqual(self.bot.state["checkin"], {})
        self.assertEqual(self.bot.state["checkin_uid"], {})
        again = await self.invoke("checkin")
        self.assertIn("签到成功", again)
        self.assertEqual(len(self.client.operations), 2)
        self.assertEqual(self.client.users[1]["balance"], Decimal("2.300"))
        saved = plugin.load_state(str(self.path))
        self.assertEqual(saved["transactions"][tx["id"]]["status"], "resolved")
        self.assertEqual(saved["checkin"]["10001"], saved["checkin_uid"]["1"])

    async def test_audit_applied_backfills_checkin_markers_with_max_semantics(self):
        self.client.plans = ["unknown_after"]
        tx, _ = await self._pending_checkin("unknown_after")
        # 请求实际已到账（unknown_after 在写入后才丢响应）
        self.assertEqual(self.client.users[1]["balance"], Decimal("2.300"))
        result = await self.invoke("audit", Event(admin=True, text=f"/核账 {tx['id']} applied"))
        self.assertIn("已到账", result)
        self.assertEqual(tx["status"], "resolved")
        # 取较新日期、不回退：已有更新的签到标记时保持不变
        self.assertEqual(self.bot.state["checkin"]["10001"], tx["date"])
        self.assertEqual(self.bot.state["checkin_uid"]["1"], tx["date"])
        self.assertIn("已经签过", await self.invoke("checkin"))
        self.assertEqual(len(self.client.operations), 1)
        self.assertEqual(self.client.users[1]["balance"], Decimal("2.300"))
        # 已 resolved 的流水不能再裁决
        self.assertIn("没有找到待核对状态", await self.invoke(
            "audit", Event(admin=True, text=f"/核账 {tx['id']} applied")))

    async def test_audit_applied_never_rolls_back_newer_checkin_marker(self):
        self.client.plans = ["unknown_after"]
        tx, _ = await self._pending_checkin("unknown_after")
        self.bot.state["checkin"]["10001"] = "2099-01-01"
        self.bot.state["checkin_uid"]["1"] = "2099-01-01"
        await self.invoke("audit", Event(admin=True, text=f"/核账 {tx['id']} applied"))
        self.assertEqual(self.bot.state["checkin"]["10001"], "2099-01-01")
        self.assertEqual(self.bot.state["checkin_uid"]["1"], "2099-01-01")

    async def test_binding_save_failure_latches_and_never_grants_balance_access(self):
        self.bot.state["bindings"] = {}
        self.bot._save()
        with patch.object(plugin.os, "replace", side_effect=OSError("simulated")):
            result = await self.invoke("bind", Event(text="/绑定 user1@example.test"))
        self.assertIn("状态保存失败", result)
        self.assertTrue(self.bot._state_failed)
        self.assertEqual(plugin.load_state(str(self.path))["bindings"], {})
        self.assertIn("插件已暂停操作", await self.invoke("checkin"))
        self.assertEqual(self.client.operations, [])
        self.assertEqual(self.reload().state["bindings"], {})

    async def test_qq_scope_and_invalid_email_rejected(self):
        for event in (Event(qq=""), Event(qq="10001", platform="telegram"), Event(qq="openid-10001")):
            with self.subTest(qq=event.qq, platform=event.platform):
                self.assertIn("OneBot QQ", await self.invoke("checkin", event))
        for text, expected in (("/绑定", "用法：/绑定"), ("/绑定 invalid", "邮箱格式不正确"),
                               ("/绑定 a@example.test unexpected", "用法：/绑定")):
            with self.subTest(text=text):
                self.assertIn(expected, await self.invoke("bind", Event(text=text)))
        self.assertEqual(self.client.operations, [])
        self.assertEqual(self.client.searches, [])
        self.assertEqual(self.bot.state["binding_requests"], {})

    async def test_concurrent_checkins_credit_exactly_once(self):
        results = await asyncio.gather(self.invoke("checkin"), self.invoke("checkin"))
        self.assertEqual(sum("签到成功" in value for value in results), 1)
        self.assertEqual(len(self.client.operations), 1)
        self.assertEqual(self.client.users[1]["balance"], Decimal("2.300"))
        tx = self.transactions()[0]
        self.assertEqual((tx["status"], tx["completed_steps"]), ("completed", 1))
        persisted = plugin.load_state(str(self.path))
        self.assertEqual(persisted["checkin"]["10001"], persisted["checkin_uid"]["1"])

    async def test_checkin_keeps_public_success_and_hands_quota_once_to_recall_manager(self):
        event = Event(text="/签到")
        public = []
        async for text in self.bot.checkin(event):
            self.assertFalse(event.stopped)
            public.append(text)
        self.assertEqual(public, ["签到成功！"])
        self.assertTrue(event.stopped)
        self.assertEqual(len(self.recall.messages), 1)
        sent_event, quota_text = self.recall.messages[0]
        self.assertIs(sent_event, event)
        self.assertIn("奖励 0.300", quota_text)
        self.assertIn("总额度 2.300", quota_text)
        self.assertIn("秒后自动撤回", quota_text)
        self.assertNotIn("\n", quota_text)
        self.assertEqual(len(self.client.operations), 1)
        self.assertEqual(self.client.users[1]["balance"], Decimal("2.300"))

    async def test_unconfirmed_quota_send_never_falls_back_to_public_amounts_or_retries_reward(self):
        self.recall.send_result = False
        event = Event(text="/签到")
        public = [text async for text in self.bot.checkin(event)]
        self.assertTrue(event.stopped)
        self.assertEqual(len(public), 1)
        self.assertIn("签到成功", public[0])
        self.assertIn("管理员", public[0])
        self.assertNotRegex(public[0], r"\d+\.\d+")
        self.assertEqual(len(self.recall.messages), 1)
        self.assertIn("已经签过", await self.invoke("checkin"))
        self.assertEqual(len(self.recall.messages), 1)
        self.assertEqual(len(self.client.operations), 1)
        self.assertEqual(self.transactions()[0]["status"], "completed")

    async def test_non_quota_replies_never_use_recall_transport(self):
        cases = (("query", Event(qq="10009", text="/查询")),
                 ("bind", Event(text="/绑定 invalid")),
                 ("checkin", Event(qq="10009", text="/签到")),
                 ("rob", Event(text="/打劫")),
                 ("slash_command_fallback", Event(text="/unknown")))
        for method, event in cases:
            with self.subTest(method=method):
                await self.invoke(method, event)
        self.assertEqual(self.recall.messages, [])
        self.assertEqual(self.client.operations, [])

    async def test_recall_manager_uses_five_seconds_and_receives_lifecycle_calls(self):
        self.assertEqual(self.recall_factory.call_args.kwargs["delay_seconds"], 5)
        self.assertEqual(Path(self.recall_factory.call_args.args[1]),
                         self.path.with_name("pending_recalls.json"))
        await self.bot.initialize()
        self.assertEqual(self.recall.initialize_calls, 1)
        await self.bot.terminate()
        self.assertEqual(self.recall.terminate_calls, 1)

    async def test_checkin_qq_and_uid_both_prevent_rebind_duplicate(self):
        await self.invoke("checkin")
        self.bot.state["bindings"].pop("10003")
        self.assertIn("绑定成功", await self.invoke("bind", Event(text="/绑定 user3@example.test")))
        self.assertIn("绑定成功", await self.invoke("bind", Event(qq="10002", text="/绑定 user1@example.test")))
        self.assertIn("已经签过", await self.invoke("checkin"))
        self.assertIn("已经签过", await self.invoke("checkin", Event(qq="10002")))
        self.assertEqual(len(self.client.operations), 1)

    async def test_repeat_checkin_is_answered_locally_without_account_read(self):
        self.assertIn("签到成功", await self.invoke("checkin"))
        self.assertEqual(self.client.reads, [1])
        # 重复签到不应再请求管理 API：上游不可用时也直接得到"已签过"。
        self.client.get_user = AsyncMock(side_effect=AssertionError("repeat check-in read the account"))
        self.assertIn("已经签过", await self.invoke("checkin"))
        self.client.get_user.assert_not_called()
        self.assertEqual(len(self.client.operations), 1)
        self.assertEqual(len(self.transactions()), 1)

    async def test_checkin_resets_at_shanghai_midnight(self):
        with patch.object(plugin, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 9, 15, 23, 59, 59, tzinfo=plugin.TZ)
            await self.invoke("checkin")
            clock.now.return_value = datetime(2026, 9, 16, 0, 0, 0, tzinfo=plugin.TZ)
            self.assertIn("签到成功", await self.invoke("checkin"))
            self.assertEqual(clock.now.call_args.args, (plugin.TZ,))
        self.assertEqual(len(self.client.operations), 2)

    async def test_intent_is_durable_before_any_balance_write(self):
        def inspect_intent(uid, amount, op, notes):
            persisted = plugin.load_state(str(self.path))
            tx = next(iter(persisted["transactions"].values()))
            self.assertEqual(tx["status"], "pending")
            self.assertEqual(tx["phase"], "step_1_intent")
            self.assertIn(tx["id"], notes)
        self.client.before_write = inspect_intent
        await self.invoke("checkin")

    async def test_uncertain_checkin_blocks_retries_after_restart(self):
        self.client.plans = ["unknown_after"]
        first = await self.invoke("checkin")
        self.assertIn("待核对", first)
        self.assertNotIn("SIMULATED", first)
        restarted = self.reload()
        self.assertIn("待核对", await self.invoke("checkin", bot=restarted))
        self.assertIn("待核对", await self.invoke("query", bot=restarted))
        self.assertEqual(len(self.client.operations), 1)
        self.assertEqual(self.client.users[1]["balance"], Decimal("2.300"))

    async def test_definite_first_rejection_allows_safe_retry(self):
        self.client.plans = ["reject"]
        result = await self.invoke("checkin")
        self.assertIn("未执行", result)
        self.assertNotIn("SIMULATED", result)
        self.assertEqual(self.transactions()[0]["status"], "failed")
        self.assertIn("签到成功", await self.invoke("checkin"))
        self.assertEqual(self.client.users[1]["balance"], Decimal("2.300"))

    async def test_checkin_read_timeout_message_reaches_user_without_journal(self):
        # 签到前置 GET 超时（含自动重试后仍失败）：细分文案直接透出，
        # 且发生在任何流水写入之前——无状态残留，用户稍后重试安全。
        async def slow_lookup(uid):
            raise plugin.ApiError("账号数据查询超时，请稍后重试。", category="read_timeout")
        self.client.get_user = slow_lookup
        result = await self.invoke("checkin")
        self.assertIn("账号数据查询超时，请稍后重试。", result)
        self.assertNotIn("签到成功", result)
        self.assertEqual(self.client.operations, [])
        self.assertEqual(self.transactions(), [])
        self.assertEqual(self.bot.state["checkin"], {})

    async def test_save_before_request_failure_has_zero_writes_and_latches(self):
        with patch.object(plugin, "save_state", side_effect=plugin.StateError("state unavailable")):
            await self.invoke("checkin")
        self.assertTrue(self.bot._state_failed)
        self.assertEqual(self.client.operations, [])
        self.assertIn("已暂停", await self.invoke("checkin"))
        self.assertEqual(self.client.operations, [])

    async def test_save_after_credit_failure_retains_pending_on_disk(self):
        save = plugin.save_state
        def fail_after_credit(state, path):
            if any(tx["completed_steps"] for tx in state["transactions"].values()):
                raise plugin.StateError("disk full")
            return save(state, path)
        with patch.object(plugin, "save_state", side_effect=fail_after_credit):
            await self.invoke("checkin")
        self.assertEqual(self.client.users[1]["balance"], Decimal("2.300"))
        self.assertTrue(self.bot._state_failed)
        self.assertIn("待核对", await self.invoke("checkin", bot=self.reload()))
        self.assertEqual(len(self.client.operations), 1)

    async def test_commit_save_failure_never_reissues_reward(self):
        save = plugin.save_state
        def fail_commit(state, path):
            if any(tx["status"] == "completed" for tx in state["transactions"].values()):
                raise plugin.StateError("disk full")
            return save(state, path)
        with patch.object(plugin, "save_state", side_effect=fail_commit):
            await self.invoke("checkin")
        pending = next(iter(plugin.load_state(str(self.path))["transactions"].values()))
        self.assertEqual((pending["status"], pending["completed_steps"]), ("pending", 1))
        self.assertIn("待核对", await self.invoke("checkin", bot=self.reload()))
        self.assertEqual(len(self.client.operations), 1)

    async def test_cancel_after_write_preserves_pending_and_releases_lock(self):
        self.client.plans = ["cancel_after"]
        event = Event()
        with self.assertRaises(asyncio.CancelledError):
            await self.invoke("checkin", event)
        self.assertTrue(event.stopped)
        self.assertFalse(self.bot._lock.locked())
        self.assertIn("待核对", await self.invoke("checkin", bot=self.reload()))
        self.assertEqual(len(self.client.operations), 1)

    async def test_successful_robbery_conserves_total_and_starts_cooldown(self):
        with patch.object(plugin.random, "random", return_value=0.9):
            result = await self.invoke("rob", Event(targets=("99999", "10002")))
            again = await self.invoke("rob", Event(targets=("10002",)))
        self.assertIn("打劫成功", result)
        self.assertIn("从 user2@example.test 抢到 0.300", result)
        self.assertNotIn("2.300", result)
        self.assertNotIn("1.700", result)
        self.assertEqual(self.recall.messages, [])
        self.assertIn("冷却", again)
        self.assertEqual(self.client.users[1]["balance"], Decimal("2.300"))
        self.assertEqual(self.client.users[2]["balance"], Decimal("1.700"))
        self.assertEqual(sum(u["balance"] for u in self.client.users.values()), Decimal("6"))
        self.assertEqual(self.transactions()[0]["completed_steps"], 2)

    async def test_failed_robbery_compensates_exactly_half(self):
        with patch.object(plugin.random, "random", return_value=0.1):
            result = await self.invoke("rob", Event(targets=("10002",)))
        self.assertIn("向 user2@example.test 赔偿了 0.500", result)
        self.assertNotIn("1.500", result)
        self.assertNotIn("2.500", result)
        self.assertEqual(self.recall.messages, [])
        self.assertEqual(self.client.users[1]["balance"], Decimal("1.500"))
        self.assertEqual(self.client.users[2]["balance"], Decimal("2.500"))

    async def test_negative_robbery_balances_are_rejected_without_randomness_or_mutation(self):
        cases = (
            ("-1.000", "2.000", "你的余额为负", [1]),
            ("-0.0001", "2.000", "你的余额为负", [1]),
            ("2.000", "-1.000", "对方余额为负", [1, 2]),
            ("2.000", "-0.0001", "对方余额为负", [1, 2]),
            ("-0.0001", "-2.000", "你的余额为负", [1]),
        )
        before = copy.deepcopy(self.bot.state)
        for own, target, expected, reads in cases:
            with self.subTest(own=own, target=target):
                self.client.users[1]["balance"] = Decimal(own)
                self.client.users[2]["balance"] = Decimal(target)
                self.client.reads.clear()
                with patch.object(plugin.random, "random") as chance, patch.object(
                    self.bot, "_rand_amount",
                ) as amount, patch.object(plugin, "save_state", wraps=plugin.save_state) as save:
                    result = await self.invoke("rob", Event(targets=("10002",)))
                self.assertIn(expected, result)
                self.assertIn("本次未扣款", result)
                self.assertIn("不计入冷却", result)
                if expected.startswith("你的"):
                    self.assertNotIn("对方余额为负", result)
                chance.assert_not_called()
                amount.assert_not_called()
                save.assert_not_called()
                self.assertEqual(self.client.reads, reads)
                self.assertEqual(self.client.operations, [])
                self.assertEqual(self.client.searches, [])
                self.assertEqual(self.bot.state, before)
                self.assertEqual(self.bot.state["transactions"], {})
                self.assertEqual(self.bot.state["robbery_ts"], {})
                self.assertEqual(self.recall.messages, [])
                self.assertFalse(self.path.exists())

    async def test_negative_checkin_is_reported_before_existing_pending_lock(self):
        self.client.users[1]["balance"] = Decimal("-0.0001")
        self.bot.state["transactions"]["already-pending"] = {
            "id": "already-pending", "status": "pending", "uids": [1],
        }
        before = copy.deepcopy(self.bot.state)
        with patch.object(plugin.random, "random") as chance, patch.object(
            self.bot, "_rand_amount",
        ) as amount, patch.object(plugin, "save_state", wraps=plugin.save_state) as save:
            result = await self.invoke("checkin")
        self.assertIn("你的余额为负", result)
        self.assertIn("本次未扣款", result)
        self.assertIn("不计入签到记录", result)
        self.assertNotIn("待核对", result)
        chance.assert_not_called()
        amount.assert_not_called()
        save.assert_not_called()
        self.assertEqual(self.client.reads, [1])
        self.assertEqual(self.client.operations, [])
        self.assertEqual(self.bot.state, before)
        self.assertEqual(self.bot.state["checkin"], {})
        self.assertEqual(self.recall.messages, [])
        self.assertFalse(self.path.exists())

    async def test_negative_target_is_reported_before_existing_pending_lock(self):
        self.client.users[2]["balance"] = Decimal("-0.0001")
        self.bot.state["transactions"]["already-pending"] = {
            "id": "already-pending", "status": "pending", "uids": [1, 2],
        }
        before = copy.deepcopy(self.bot.state)
        with patch.object(plugin.random, "random") as chance, patch.object(
            self.bot, "_rand_amount",
        ) as amount, patch.object(plugin, "save_state", wraps=plugin.save_state) as save:
            result = await self.invoke("rob", Event(targets=("10002",)))
        self.assertIn("对方余额为负", result)
        self.assertIn("本次未扣款", result)
        self.assertIn("不计入冷却", result)
        self.assertNotIn("待核对", result)
        chance.assert_not_called()
        amount.assert_not_called()
        save.assert_not_called()
        self.assertEqual(self.client.reads, [1, 2])
        self.assertEqual(self.client.operations, [])
        self.assertEqual(self.bot.state, before)
        self.assertEqual(self.bot.state["robbery_ts"], {})
        self.assertEqual(self.recall.messages, [])
        self.assertFalse(self.path.exists())

    async def test_low_robber_balance_is_rejected_before_target_lookup_or_randomness(self):
        for own in ("0.000", "0.4999"):
            with self.subTest(own=own):
                self.client.users[1]["balance"] = Decimal(own)
                self.client.reads.clear()
                before = copy.deepcopy(self.bot.state)
                with patch.object(plugin.random, "random") as chance, patch.object(
                    self.bot, "_rand_amount",
                ) as amount, patch.object(plugin, "save_state", wraps=plugin.save_state) as save:
                    result = await self.invoke("rob", Event(targets=("10002",)))
                self.assertIn("不足以承担 0.500 赔款", result)
                self.assertIn("本次未扣款", result)
                self.assertIn("不计入冷却", result)
                chance.assert_not_called()
                amount.assert_not_called()
                save.assert_not_called()
                self.assertEqual(self.client.reads, [1])
                self.assertEqual(self.client.operations, [])
                self.assertEqual(self.bot.state, before)
                self.assertEqual(self.bot.state["robbery_ts"], {})
                self.assertEqual(self.recall.messages, [])
                self.assertFalse(self.path.exists())

    async def test_exact_compensation_threshold_is_allowed(self):
        self.client.users[1]["balance"] = Decimal("0.500")
        with patch.object(plugin.random, "random", return_value=0.1) as chance:
            result = await self.invoke("rob", Event(targets=("10002",)))
        self.assertIn("赔偿了 0.500", result)
        chance.assert_called_once_with()
        self.assertEqual(self.client.reads, [1, 2])
        self.assertEqual(len(self.client.operations), 2)
        self.assertEqual(self.client.users[1]["balance"], Decimal("0.000"))
        self.assertEqual(self.client.users[2]["balance"], Decimal("2.500"))

    async def test_zero_victim_balance_still_follows_normal_rules(self):
        cases = (
            ("2.000", "0.000", 0.9, "打劫成功", "2.000", "0.000", 0),
            ("2.000", "0.000", 0.1, "打劫失败", "1.500", "0.500", 2),
        )
        initial_state = copy.deepcopy(self.bot.state)
        for own, target, roll, expected, own_after, target_after, writes in cases:
            with self.subTest(own=own, target=target, roll=roll):
                self.bot.state = copy.deepcopy(initial_state)
                self.client = FakeClient()
                self.bot.client = self.client
                self.client.users[1]["balance"] = Decimal(own)
                self.client.users[2]["balance"] = Decimal(target)
                with patch.object(plugin.random, "random", return_value=roll) as chance:
                    result = await self.invoke("rob", Event(targets=("10002",)))
                self.assertIn(expected, result)
                self.assertNotIn("余额为负", result)
                chance.assert_called_once_with()
                self.assertEqual(self.client.reads, [1, 2])
                self.assertEqual(len(self.client.operations), writes)
                self.assertEqual(self.client.users[1]["balance"], Decimal(own_after))
                self.assertEqual(self.client.users[2]["balance"], Decimal(target_after))
                self.assertEqual(len(self.bot.state["transactions"]), 1 if writes else 0)
                # v1.6.2 起：零余额成功不进入冷却；只有真实转账（成功抢到/失败赔款）才写冷却。
                self.assertEqual("10001" in self.bot.state["robbery_ts"], writes > 0)
                self.assertEqual(self.recall.messages, [])

    async def test_insufficient_compensation_is_rejected_before_randomness(self):
        self.client.users[1]["balance"] = Decimal("0.4999")
        with patch.object(plugin.random, "random") as chance, patch.object(plugin, "save_state", wraps=plugin.save_state) as save:
            result = await self.invoke("rob", Event(targets=("10002",)))
        self.assertIn("不足以承担 0.500 赔款", result)
        self.assertIn("本次未扣款", result)
        self.assertIn("不计入冷却", result)
        chance.assert_not_called()
        save.assert_not_called()
        self.assertEqual(self.recall.messages, [])
        self.assertEqual(self.client.operations, [])
        self.assertEqual(self.client.reads, [1])
        self.assertNotIn("10001", self.bot.state["robbery_ts"])

    async def test_sub_milli_victim_balance_is_never_rounded_up(self):
        with patch.object(plugin.random, "random", return_value=0.9):
            self.client.users[2]["balance"] = Decimal("0.0019")
            await self.invoke("rob", Event(targets=("10002",)))
            self.assertEqual(self.client.operations[0][1], Decimal("0.001"))
            self.assertEqual(self.client.users[2]["balance"], Decimal("0.0009"))
            self.bot.state["robbery_ts"].clear()
            result = await self.invoke("rob", Event(targets=("10002",)))
        self.assertIn("打劫成功", result)
        self.assertIn("对方无可转移余额，本次获得 0.000", result)
        self.assertNotIn("0.0009", result)
        self.assertNotIn("2.001", result)
        self.assertEqual(self.recall.messages, [])
        self.assertEqual(len(self.client.operations), 2)

    async def test_two_robbers_cannot_overdraw_same_victim(self):
        self.client.users[2]["balance"] = Decimal("0.300")
        with patch.object(plugin.random, "random", return_value=0.9):
            await asyncio.gather(
                self.invoke("rob", Event(qq="10001", targets=("10002",))),
                self.invoke("rob", Event(qq="10003", targets=("10002",))),
            )
        self.assertEqual(self.client.users[2]["balance"], Decimal(0))
        self.assertEqual(sum(u["balance"] for u in self.client.users.values()), Decimal("4.300"))
        self.assertEqual(len(self.client.operations), 2)

    async def test_second_leg_rejection_locks_both_accounts_without_compensation(self):
        self.client.plans = [None, "reject"]
        with patch.object(plugin.random, "random", return_value=0.9):
            result = await self.invoke("rob", Event(targets=("10002",)))
        self.assertIn("待核对", result)
        self.assertEqual(self.client.users[2]["balance"], Decimal("1.700"))
        self.assertEqual(self.client.users[1]["balance"], Decimal("2"))
        self.assertIn("10001", self.bot.state["robbery_ts"])
        tx = self.transactions()[0]
        self.assertEqual((tx["status"], tx["completed_steps"]), ("pending", 1))
        for qq in ("10001", "10002"):
            self.assertIn("待核对", await self.invoke("checkin", Event(qq=qq), bot=self.reload()))
        self.assertEqual(len(self.client.operations), 2)

    async def test_second_leg_unknown_after_credit_is_not_repeated(self):
        self.client.plans = [None, "unknown_after"]
        with patch.object(plugin.random, "random", return_value=0.9):
            await self.invoke("rob", Event(targets=("10002",)))
        self.assertEqual(self.client.users[1]["balance"], Decimal("2.300"))
        self.assertEqual(self.client.users[2]["balance"], Decimal("1.700"))
        self.assertIn("待核对", await self.invoke("rob", Event(targets=("10002",)), bot=self.reload()))
        self.assertEqual(len(self.client.operations), 2)

    async def test_first_leg_unknown_never_starts_second_leg(self):
        self.client.plans = ["unknown_after"]
        with patch.object(plugin.random, "random", return_value=0.9):
            await self.invoke("rob", Event(targets=("10002",)))
        self.assertEqual(len(self.client.operations), 1)
        self.assertEqual(self.client.users[1]["balance"], Decimal("2"))
        self.assertIn("待核对", await self.invoke("checkin", Event(qq="10002")))

    async def test_first_leg_rejection_releases_cooldown(self):
        self.client.plans = ["reject"]
        with patch.object(plugin.random, "random", return_value=0.9):
            await self.invoke("rob", Event(targets=("10002",)))
        self.assertEqual(self.transactions()[0]["status"], "failed")
        self.assertNotIn("10001", self.bot.state["robbery_ts"])
        self.assertEqual(len(self.client.operations), 1)

    async def test_same_wallet_and_invalid_targets_do_not_mutate(self):
        for targets in ((), ("all",), ("99999",), ("10001",), ("10002", "10003"), ("10004",)):
            with self.subTest(targets=targets):
                await self.invoke("rob", Event(targets=targets))
        self.bot.state["bindings"]["10002"] = dict(self.bot.state["bindings"]["10001"])
        self.assertIn("同一账号", await self.invoke("rob", Event(targets=("10002",))))
        self.assertEqual(self.client.operations, [])

    async def test_robbery_identifies_unbound_target_without_blame_on_sender(self):
        for state in ("missing", "not_approved"):
            with self.subTest(state=state):
                self.bindings()
                if state == "missing":
                    self.bot.state["bindings"].pop("10002")
                else:
                    self.bot.state["bindings"]["10002"]["approved"] = False
                result = await self.invoke("rob", Event(targets=("10002",)))
                self.assertIn("对方尚未绑定", result)
                self.assertNotIn("你尚未完成账号绑定", result)
        self.assertEqual(self.client.operations, [])
        self.assertEqual(self.client.reads, [])
        self.assertEqual(self.client.searches, [])

    async def test_robbery_identifies_unbound_sender_before_target_account_lookup(self):
        for state in ("missing", "not_approved"):
            with self.subTest(state=state):
                self.bindings()
                if state == "missing":
                    self.bot.state["bindings"].pop("10001")
                else:
                    self.bot.state["bindings"]["10001"]["approved"] = False
                result = await self.invoke("rob", Event(targets=("10002",)))
                self.assertIn("你尚未完成账号绑定", result)
                self.assertNotIn("对方尚未绑定", result)
        self.assertEqual(self.client.operations, [])
        self.assertEqual(self.client.reads, [])
        self.assertEqual(self.client.searches, [])

    async def test_account_status_role_and_frozen_balance_checked_at_use(self):
        for field, value in (("status", "disabled"), ("role", "admin"), ("role", "unexpected"), ("frozen_balance", "0.001")):
            with self.subTest(field=field, value=value):
                original = self.client.users[1][field]
                self.client.users[1][field] = value
                await self.invoke("checkin")
                await self.invoke("rob", Event(targets=("10002",)))
                self.client.users[1][field] = original
        self.assertEqual(self.client.operations, [])

    async def test_query_deleted_or_changed_account_requires_rebinding(self):
        del self.client.users[1]
        self.assertIn("重新绑定", await self.invoke("query"))
        self.assertEqual(self.client.searches, [])
        self.client.users[1] = {**self.client.users[2], "id": 1}
        self.assertIn("身份发生变化", await self.invoke("query"))
        self.assertEqual(self.bot.state["bindings"]["10001"]["uid"], 1)

    async def test_pending_account_cannot_be_rebound_away(self):
        self.client.plans = ["unknown_after"]
        await self.invoke("checkin")
        result = await self.invoke("bind", Event(text="/绑定 user3@example.test"))
        self.assertIn("待核对", result)
        self.assertEqual(self.bot.state["bindings"]["10001"]["uid"], 1)

    async def test_pending_destination_account_cannot_be_bound(self):
        self.client.plans = ["unknown_after"]
        await self.invoke("checkin")
        result = await self.invoke("bind", Event(qq="10004", text="/绑定 user1@example.test"))
        self.assertIn("待核对", result)
        self.assertNotIn("10004", self.bot.state["bindings"])
        self.assertEqual(len(self.client.operations), 1)

    async def test_query_keeps_public_reply_hidden_and_sends_only_current_balance_for_recall(self):
        self.client.users[1]["balance"] = "123.456"
        self.client.users[1]["frozen_balance"] = "7.890"
        self.client.users[1]["total_recharged"] = "987.654"
        event = Event(text="/查询")
        public = []
        async for result in self.bot.query(event):
            self.assertFalse(event.stopped)
            public.append(result)
        self.assertEqual(len(public), 1)
        self.assertTrue(event.stopped)
        self.assertIn("已绑定 user1@example.test", public[0])
        self.assertIn("状态正常", public[0])
        self.assertEqual(len(self.recall.messages), 1)
        sent_event, quota_text = self.recall.messages[0]
        self.assertIs(sent_event, event)
        for amount in ("123.456", "7.890", "987.654"):
            self.assertNotIn(amount, public[0])
        self.assertIn("当前余额：123.456", quota_text)
        self.assertIn("秒后自动撤回", quota_text)
        self.assertNotIn("\n", quota_text)
        for hidden in ("7.890", "987.654", "冻结", "累计"):
            self.assertNotIn(hidden, quota_text)
        self.assertNotRegex(public[0], r"\d+\.\d+")
        self.assertEqual(self.client.reads, [1])
        self.assertEqual(self.client.operations, [])

    async def test_query_recall_failure_never_exposes_amounts_or_repeats_account_read(self):
        self.client.users[1]["balance"] = "123.456"
        self.client.users[1]["frozen_balance"] = "7.890"
        self.client.users[1]["total_recharged"] = "987.654"
        self.recall.send_result = False
        event = Event(text="/查询")
        public = [result async for result in self.bot.query(event)]
        self.assertTrue(event.stopped)
        self.assertEqual(len(public), 1)
        self.assertIn("管理员", public[0])
        self.assertNotRegex(public[0], r"\d+\.\d+")
        self.assertEqual(len(self.recall.messages), 1)
        for amount in ("123.456", "7.890", "987.654"):
            self.assertNotIn(amount, public[0])
        self.assertIn("123.456", self.recall.messages[0][1])
        self.assertIn("秒后自动撤回", self.recall.messages[0][1])
        self.assertNotIn("\n", self.recall.messages[0][1])
        for hidden in ("7.890", "987.654", "冻结", "累计"):
            self.assertNotIn(hidden, self.recall.messages[0][1])
        self.assertEqual(self.client.reads, [1])
        self.assertEqual(self.client.searches, [])
        self.assertEqual(self.client.operations, [])

    def test_cooldown_ceils_last_fraction_of_second(self):
        self.bot.state["robbery_ts"]["10001"] = 1000
        with patch.object(plugin.time, "time", return_value=1599.999):
            self.assertEqual(self.bot._remaining_cooldown("10001"), 1)
        with patch.object(plugin.time, "time", return_value=1600):
            self.assertEqual(self.bot._remaining_cooldown("10001"), 0)

    async def test_legacy_state_keeps_history_and_requires_explicit_rebinding(self):
        legacy = {"bindings": {"10001": {"uid": 1, "email": "User1@example.test"}},
                  "checkin": {"10001": "2026-09-15"}, "robbery_ts": {"10001": 1000}}
        self.path.write_text(json.dumps(legacy), encoding="utf-8")
        state = plugin.load_state(str(self.path))
        self.assertFalse(state["bindings"]["10001"]["approved"])
        self.assertEqual(state["checkin_uid"]["1"], "2026-09-15")
        self.assertIn("10001", state["binding_requests"])
        self.assertEqual(state["robbery_ts"]["10001"], 1000)
        self.assertEqual(state["protections"], {})  # 非金融可选键：legacy 无该键时回填空表
        plugin.save_state(state, str(self.path))
        self.assertEqual(plugin.load_state(str(self.path)), state)
        self.bot = self.reload()
        self.assertIn("尚未完成账号绑定", await self.invoke("checkin"))
        self.assertEqual(self.client.searches, [])
        self.assertIn("绑定成功", await self.invoke("bind", Event(text="/绑定 user1@example.test")))
        self.assertTrue(self.bot.state["bindings"]["10001"]["approved"])
        self.assertNotIn("10001", self.bot.state["binding_requests"])
        self.assertEqual(self.bot.state["checkin_uid"]["1"], "2026-09-15")
        self.assertEqual(self.bot.state["robbery_ts"]["10001"], 1000)
        self.assertEqual(self.client.operations, [])

    async def test_unbind_removes_binding_keeps_checkin_uid_and_robbery_ts(self):
        self.bot.state["robbery_ts"]["10001"] = 1234
        self.assertIn("签到成功", await self.invoke("checkin"))
        result = await self.invoke("unbind", Event(text="/解绑"))
        self.assertIn("已解绑 user1@example.test", result)
        self.assertNotIn("10001", self.bot.state["bindings"])
        saved = plugin.load_state(str(self.path))
        self.assertNotIn("10001", saved["bindings"])
        self.assertEqual(saved["checkin"].get("10001"), saved["checkin_uid"]["1"])
        self.assertEqual(saved["robbery_ts"].get("10001"), 1234)
        self.assertEqual(len(self.client.operations), 1)  # 仅签到那一笔；解绑零 API 操作
        self.assertEqual(self.client.searches, [])

    async def test_unbind_blocked_while_pending_transaction_exists(self):
        self.client.plans = ["unknown_after"]
        self.assertIn("待核对", await self.invoke("checkin"))
        result = await self.invoke("unbind", Event(text="/解绑"))
        self.assertIn("待核对", result)
        self.assertIn("流水", result)
        self.assertIn("10001", self.bot.state["bindings"])
        self.assertEqual(len(self.client.operations), 1)

    async def test_unbind_requires_binding_and_ignores_repeat(self):
        self.bot.state["bindings"].pop("10001")
        self.assertIn("尚未完成账号绑定", await self.invoke("unbind", Event(text="/解绑")))
        self.assertIn("已解绑", await self.invoke("unbind", Event(qq="10002", text="/解绑")))
        again = await self.invoke("unbind", Event(qq="10002", text="/解绑"))
        self.assertIn("尚未完成账号绑定", again)
        self.assertEqual(set(self.bot.state["bindings"]), {"10003"})
        self.assertEqual(self.client.operations, [])
        self.assertEqual(self.client.searches, [])

    async def test_unbind_then_rebind_cannot_farm_checkin(self):
        self.bot.state["bindings"].pop("10003")
        self.assertIn("签到成功", await self.invoke("checkin"))
        self.assertIn("已解绑", await self.invoke("unbind", Event(text="/解绑")))
        self.assertIn("绑定成功", await self.invoke("bind", Event(text="/绑定 user1@example.test")))
        self.assertIn("已经签过", await self.invoke("checkin"))
        self.assertIn("已解绑", await self.invoke("unbind", Event(text="/解绑")))
        self.assertIn("绑定成功", await self.invoke("bind", Event(text="/绑定 user3@example.test")))
        self.assertIn("已经签过", await self.invoke("checkin"))
        self.assertEqual(len(self.client.operations), 1)  # 重复签到不再写入

    async def test_protection_toggle_requires_binding_and_persists(self):
        self.bot.state["bindings"].pop("10001")
        self.assertIn("尚未完成", await self.invoke("protect", Event(text="/保护")))
        self.assertNotIn("10001", self.bot.state.get("protections", {}))
        text = await self.invoke("protect", Event(qq="10002", text="/保护"))
        self.assertIn("免打劫保护已开启", text)
        self.assertIn("再次切换需等 24 小时", text)
        self.assertIs(self.bot.state["protections"]["10002"]["enabled"], True)
        self.assertEqual(plugin.load_state(str(self.path))["protections"]["10002"],
                         self.bot.state["protections"]["10002"])
        self.assertEqual(self.client.operations, [])
        self.assertEqual(self.client.reads, [])
        self.assertEqual(self.client.searches, [])

    async def test_protection_switch_cooldown_blocks_and_expires(self):
        text = await self.invoke("protect", Event(text="/保护"))
        self.assertIn("免打劫保护已开启", text)
        stamp = self.bot.state["protections"]["10001"]["updated_at"]
        blocked = await self.invoke("protect", Event(text="/保护"))
        self.assertIn("还需等待 24 小时", blocked)
        self.assertIs(self.bot.state["protections"]["10001"]["enabled"], True)
        self.assertEqual(self.bot.state["protections"]["10001"]["updated_at"], stamp)
        # 冷却期内打劫拦截文案带剩余时间
        rob = await self.invoke("rob", Event(targets=("10002",)))
        self.assertIn("还需等待", rob)
        # 回拨切换时间到冷却之外,过期后可正常关闭
        self.bot.state["protections"]["10001"]["updated_at"] = (
            plugin.time.time() - self.bot.cfg["protection_switch_cooldown"] - 1)
        off = await self.invoke("protect", Event(text="/保护"))
        self.assertIn("免打劫保护已关闭", off)
        self.assertIs(self.bot.state["protections"]["10001"]["enabled"], False)
        self.assertEqual(self.client.operations, [])
        self.assertEqual(self.client.reads, [])
        self.assertEqual(self.client.searches, [])

    async def test_protected_robber_cannot_rob_without_side_effects(self):
        self.assertIn("免打劫保护已开启", await self.invoke("protect", Event(text="/保护")))
        before = copy.deepcopy(self.bot.state)
        with patch.object(plugin.random, "random") as chance, patch.object(
                self.bot, "_rand_amount") as amount, patch.object(
                plugin, "save_state", wraps=plugin.save_state) as save:
            result = await self.invoke("rob", Event(targets=("10002",)))
        self.assertIn("免打劫保护", result)
        chance.assert_not_called()
        amount.assert_not_called()
        save.assert_not_called()
        self.assertEqual(self.client.reads, [])
        self.assertEqual(self.client.operations, [])
        self.assertEqual(self.bot.state, before)
        self.assertEqual(self.bot.state["robbery_ts"], {})
        self.assertEqual(self.recall.messages, [])
        self.assertEqual(self.transactions(), [])

    async def test_protected_victim_cannot_be_robbed_and_toggles_back(self):
        self.assertIn("免打劫保护已开启",
                      await self.invoke("protect", Event(qq="10002", text="/保护")))
        before = copy.deepcopy(self.bot.state)
        result = await self.invoke("rob", Event(targets=("10002",)))
        self.assertIn("对方已开启免打劫保护", result)
        self.assertEqual(self.bot.state, before)
        self.assertEqual(self.client.reads, [])
        self.assertEqual(self.client.operations, [])
        # 回拨切换时间越过 24 小时冷却,验证"关闭后可再被抢"的完整回路
        self.bot.state["protections"]["10002"]["updated_at"] = (
            plugin.time.time() - self.bot.cfg["protection_switch_cooldown"] - 1)
        self.assertIn("免打劫保护已关闭",
                      await self.invoke("protect", Event(qq="10002", text="/保护")))
        with patch.object(plugin.random, "random", return_value=0.9):
            robbed = await self.invoke("rob", Event(targets=("10002",)))
        self.assertIn("打劫成功", robbed)
        self.assertEqual(len(self.client.operations), 2)

    async def test_protection_check_order_keeps_existing_message_priority(self):
        self.assertIn("免打劫保护已开启", await self.invoke("protect", Event(text="/保护")))
        self.bot.state["bindings"].pop("10002")
        unbound_target = await self.invoke("rob", Event(targets=("10002",)))
        self.assertIn("对方尚未绑定", unbound_target)
        self.assertNotIn("免打劫保护", unbound_target)
        self.bot.state["bindings"].pop("10001")
        unbound_sender = await self.invoke("rob", Event(targets=("10002",)))
        self.assertIn("你尚未完成账号绑定", unbound_sender)
        self.assertEqual(self.client.reads, [])

    async def test_protection_survives_unbind_and_reload(self):
        self.assertIn("免打劫保护已开启", await self.invoke("protect", Event(text="/保护")))
        self.assertIn("已解绑", await self.invoke("unbind", Event(text="/解绑")))
        self.bot = self.reload()
        self.assertIs(self.bot.state["protections"]["10001"]["enabled"], True)
        self.assertIn("绑定成功", await self.invoke("bind", Event(text="/绑定 user1@example.test")))
        result = await self.invoke("rob", Event(targets=("10002",)))
        self.assertIn("免打劫保护", result)
        self.assertEqual(self.client.operations, [])
        self.assertEqual(self.client.reads, [])

    def test_load_state_backfills_and_validates_protections(self):
        base = {"version": 2, "bindings": {}, "checkin": {}, "robbery_ts": {},
                "checkin_uid": {}, "binding_requests": {}, "transactions": {}}
        missing = copy.deepcopy(base)
        self.path.write_text(json.dumps(missing), encoding="utf-8")
        self.assertEqual(plugin.load_state(str(self.path))["protections"], {})
        good = copy.deepcopy(base)
        good["protections"] = {"10001": {"enabled": True, "updated_at": 123.5}}
        self.path.write_text(json.dumps(good), encoding="utf-8")
        self.assertEqual(plugin.load_state(str(self.path))["protections"], good["protections"])
        no_stamp = copy.deepcopy(base)
        no_stamp["protections"] = {"10001": {"enabled": False}}
        self.path.write_text(json.dumps(no_stamp), encoding="utf-8")
        self.assertEqual(plugin.load_state(str(self.path))["protections"]["10001"]["updated_at"], 0)
        broken_entries = ({"enabled": 1, "updated_at": 0},
                          {"enabled": True, "updated_at": -1},
                          {"enabled": True, "updated_at": "x"},
                          {"updated_at": 0})
        for broken_entry in broken_entries:
            sample = copy.deepcopy(base)
            sample["protections"] = {"10001": broken_entry}
            self.path.write_text(json.dumps(sample), encoding="utf-8")
            with self.subTest(entry=broken_entry), self.assertRaises(plugin.StateError):
                plugin.load_state(str(self.path))
        sample = copy.deepcopy(base)
        sample["protections"] = {"not-a-qq": {"enabled": True, "updated_at": 0}}
        self.path.write_text(json.dumps(sample), encoding="utf-8")
        with self.assertRaises(plugin.StateError):
            plugin.load_state(str(self.path))

    def make_terminal_tx(self, txid, status="completed", age_days=0):
        stamp = plugin.time.time() - age_days * 86400
        tx = {"id": txid, "kind": "checkin", "qq": "10001", "uids": [1], "date": "2026-09-15",
              "amount": "0.300", "steps": [{"uid": 1, "operation": "add"}],
              "balances_before": {"1": "2.000"}, "status": status,
              "phase": "committed" if status == "completed" else "intent",
              "completed_steps": 1 if status == "completed" else 0, "created_at": stamp}
        if status == "completed":
            tx["completed_at"] = stamp
        if status == "resolved":
            tx["resolution_note"] = "管理员核账：测试。"
        return tx

    def test_load_state_archives_only_stale_terminal_transactions(self):
        state = copy.deepcopy(self.bot.state)
        state["transactions"] = {
            "a" * 32: self.make_terminal_tx("a" * 32, "completed", age_days=40),
            "b" * 32: self.make_terminal_tx("b" * 32, "resolved", age_days=40),
            "c" * 32: self.make_terminal_tx("c" * 32, "failed", age_days=40),
            "d" * 32: self.make_terminal_tx("d" * 32, "completed", age_days=1),
            "e" * 32: self.make_terminal_tx("e" * 32, "pending", age_days=40),
        }
        self.path.write_text(json.dumps(state), encoding="utf-8")
        loaded = plugin.load_state(str(self.path), 30)
        self.assertEqual(set(loaded["transactions"]), {"d" * 32, "e" * 32})
        archive = json.loads(self.path.with_name("ledger_archive.json").read_text(encoding="utf-8"))
        self.assertEqual(set(archive["transactions"]), {"a" * 32, "b" * 32, "c" * 32})
        # 磁盘 state.json 未动；重复加载幂等（按 txid 覆盖，不产生重复条目）
        self.assertEqual(set(plugin.load_state(str(self.path))["transactions"]),
                         {"a" * 32, "b" * 32, "c" * 32, "d" * 32, "e" * 32})
        again = plugin.load_state(str(self.path), 30)
        self.assertEqual(set(again["transactions"]), {"d" * 32, "e" * 32})
        archive = json.loads(self.path.with_name("ledger_archive.json").read_text(encoding="utf-8"))
        self.assertEqual(len(archive["transactions"]), 3)
        self.assertEqual(archive["version"], 1)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_load_state_fails_closed_on_corrupt_archive(self):
        state = copy.deepcopy(self.bot.state)
        state["transactions"] = {"a" * 32: self.make_terminal_tx("a" * 32, "completed", age_days=40)}
        self.path.write_text(json.dumps(state), encoding="utf-8")
        for content in ("broken", "[]", '{"version": 2, "transactions": {}}',
                        '{"version": 1, "transactions": {"x": {"id": "y"}}}'):
            with self.subTest(content=content):
                self.path.with_name("ledger_archive.json").write_text(content, encoding="utf-8")
                with self.assertRaises(plugin.StateError):
                    plugin.load_state(str(self.path), 30)
        self.assertEqual(self.path.with_name("ledger_archive.json").read_text(encoding="utf-8"),
                         '{"version": 1, "transactions": {"x": {"id": "y"}}}')

    def test_corrupt_state_fails_closed_without_overwriting(self):
        for content in ("broken json", "null", "[]", '{"bindings":{}}'):
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                with self.assertRaises(plugin.StateError):
                    self.reload()
                self.assertEqual(self.path.read_text(encoding="utf-8"), content)

    def test_v2_missing_journal_is_corruption(self):
        state = copy.deepcopy(self.bot.state)
        for field in ("transactions", "binding_requests", "checkin_uid"):
            broken = copy.deepcopy(state)
            del broken[field]
            self.path.write_text(json.dumps(broken), encoding="utf-8")
            with self.subTest(field=field), self.assertRaises(plugin.StateError):
                self.reload()

    async def test_inconsistent_journal_cannot_unlock_account(self):
        await self.invoke("checkin")
        original = copy.deepcopy(self.bot.state)
        for updates in ({"status": "failed"}, {"completed_steps": 0}, {"kind": "unknown"}, {"qq": ""}, {"date": "bad"}):
            broken = copy.deepcopy(original)
            next(iter(broken["transactions"].values())).update(updates)
            self.path.write_text(json.dumps(broken), encoding="utf-8")
            with self.subTest(updates=updates), self.assertRaises(plugin.StateError):
                self.reload()

    def test_read_permission_error_does_not_become_empty_state(self):
        with patch("builtins.open", side_effect=PermissionError("simulated")):
            with self.assertRaises(plugin.StateError):
                plugin.load_state(str(self.path))

    def test_atomic_save_keeps_old_state_when_replace_fails(self):
        plugin.save_state(self.bot.state, str(self.path))
        previous = self.path.read_bytes()
        with patch.object(plugin.os, "replace", side_effect=OSError("simulated")):
            with self.assertRaises(plugin.StateError):
                plugin.save_state({"new": "state"}, str(self.path))
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_defaults_do_not_embed_credentials_and_config_validates(self):
        self.assertEqual(self.bot.cfg["admin_email"], "")
        self.assertEqual(self.bot.cfg["admin_password"], "")
        self.assertEqual(self.bot.cfg["read_timeout_seconds"], 30)
        for config in ({"robbery_cooldown": -1}, {"robbery_cooldown": True}, {"allow_bind_admin": "false"},
                       {"write_timeout_seconds": 4}, {"write_timeout_seconds": 121},
                       {"write_timeout_seconds": "30"}, {"write_timeout_seconds": True},
                       {"read_timeout_seconds": 4}, {"read_timeout_seconds": 121},
                       {"read_timeout_seconds": "30"}, {"read_timeout_seconds": True},
                       {"transaction_retention_days": 0}, {"transaction_retention_days": 3651},
                       {"transaction_retention_days": True}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                plugin.Sub2ApiPlugin(object(), config)

    async def test_status_command_needs_no_binding_and_never_waits_for_wallet_lock(self):
        self.bot.client = FakeOpsClient(
            [{"id": 1, "name": "Alpha", "status": "active", "platform": "openai"}],
            snapshots={1: ops_snapshot(100, 99)},
        )
        before = copy.deepcopy(self.bot.state)
        with patch.object(plugin, "render_group_cards", return_value="/fake/status.png") as render:
            async with self.bot._lock:  # 账务指令进行中：/状态 只读，不应排队等锁
                result = await asyncio.wait_for(
                    self.invoke("status", Event(qq="10009", text="/状态")), 1)
        self.assertEqual(result, "[image] /fake/status.png")
        rows, generated = render.call_args.args
        self.assertEqual([row["name"] for row in rows], ["Alpha"])
        self.assertRegex(generated, r"\A\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\Z")
        self.assertEqual(self.bot.state, before)
        self.assertFalse(self.path.exists())

    async def test_status_command_replies_text_without_public_groups_or_on_failure(self):
        self.bot.client = FakeOpsClient(
            [{"id": 5, "name": "VIP", "status": "active", "is_exclusive": True},
             {"id": 4, "name": "Retired", "status": "disabled"}])
        with patch.object(plugin, "render_group_cards") as render:
            self.assertEqual(await self.invoke("status", Event(text="/状态")), "当前没有可展示的公开分组。")
        render.assert_not_called()
        self.assertEqual(self.bot.client.snapshot_ids(), [])
        self.bot.client.get_ops_list = AsyncMock(side_effect=plugin.ApiError("sub2api 监控数据响应异常，请联系管理员。"))
        self.assertEqual(await self.invoke("status", Event(text="/状态")), "sub2api 监控数据响应异常，请联系管理员。")
        self.bot.client.get_ops_list = AsyncMock(side_effect=RuntimeError("SIMULATED_PRIVATE_DETAIL"))
        self.assertEqual(await self.invoke("status", Event(text="/状态")), "监控卡片生成失败，请稍后重试或联系管理员。")


class GroupStatusTests(unittest.IsolatedAsyncioTestCase):
    """/状态 卡片与 LLM 工具的脱敏口径（AGENTS.md 红线 6）：只有分组级百分比，没有规模数据。"""

    GROUPS = [
        {"id": 1, "name": "Alpha", "status": "active", "platform": "openai", "rate_multiplier": 1.5},
        {"id": 2, "name": "Beta", "status": "active", "platform": "anthropic"},
        {"id": 3, "name": "Gamma", "status": "active", "platform": "gemini"},
        {"id": 4, "name": "Retired", "status": "disabled", "platform": "openai"},
        {"id": 5, "name": "VIP", "status": "active", "platform": "anthropic", "is_exclusive": True},
        {"id": 6, "name": "Flaky", "status": "active", "platform": "openai"},
    ]

    def ops(self, groups=None):
        return FakeOpsClient(self.GROUPS if groups is None else groups, snapshots={
            1: ops_snapshot(1234, 1220, points=((5, 0), (0, 0), (3, 1))),
            2: ops_snapshot(12, 9, health=40),
            3: ops_snapshot(0, 0),
            5: ops_snapshot(5000, 4999),
        }, availability={
            "1": {"total_accounts": 8, "available_count": 6, "rate_limit_count": 0, "error_count": 0},
            "2": {"total_accounts": 3, "available_count": 1, "rate_limit_count": 2, "error_count": 0},
        }, failing={6})

    def public_groups(self):
        return [group for group in self.GROUPS if not group.get("is_exclusive")]

    async def test_success_rate_text_is_whitelisted_percentages_only(self):
        client = self.ops(self.public_groups())
        text = await plugin.collect_group_success_rates(client)
        header, *lines = text.splitlines()
        self.assertIn("仅百分比", header)
        self.assertEqual(lines, ["Alpha:98.9%", "Beta:75.0%(样本少)",
                                 "Gamma:近一小时无请求或数据暂缺", "Flaky:近一小时无请求或数据暂缺"])
        for line in lines:
            self.assertRegex(line, r"\A[^:]+:(\d{1,3}\.\d%(\(样本少\))?|近一小时无请求或数据暂缺)\Z")
        for secret in ("1234", "1220", "987654", "12.5", "37", "SIMULATED", "Retired"):
            self.assertNotIn(secret, text)
        self.assertNotIn(4, client.snapshot_ids())

    async def test_success_rate_keyword_filter_matches_case_insensitively(self):
        client = self.ops(self.public_groups())
        filtered = await plugin.collect_group_success_rates(client, "  ALP ")
        self.assertEqual(filtered.splitlines()[1:], ["Alpha:98.9%"])
        self.assertEqual(client.snapshot_ids(), [1])
        self.assertTrue((await plugin.collect_group_success_rates(client, "不存在")).endswith("（没有匹配的启用分组）"))

    async def test_llm_tool_caches_per_keyword_for_sixty_seconds_and_never_caches_failures(self):
        bot = plugin.Sub2ApiPlugin.__new__(plugin.Sub2ApiPlugin)
        bot.client = self.ops(self.public_groups())
        now = [1000.0]
        with patch.object(plugin, "time", types.SimpleNamespace(time=lambda: now[0])):
            first = await bot.query_group_success_rate(Event(), "")
            calls = len(bot.client.calls)
            now[0] = 1059.0
            self.assertEqual(await bot.query_group_success_rate(Event(), "  "), first)
            self.assertEqual(len(bot.client.calls), calls)
            self.assertEqual((await bot.query_group_success_rate(Event(), "alp")).splitlines()[1:], ["Alpha:98.9%"])
            self.assertGreater(len(bot.client.calls), calls)
            now[0] = 2000.0
            bot.client.get_ops_list = AsyncMock(side_effect=plugin.ApiError("sub2api 监控数据响应异常，请联系管理员。"))
            self.assertEqual(await bot.query_group_success_rate(Event(), ""),
                             "查询失败：sub2api 监控数据响应异常，请联系管理员。")
            bot.client.get_ops_list = AsyncMock(side_effect=RuntimeError("SIMULATED_PRIVATE_DETAIL"))
            self.assertEqual(await bot.query_group_success_rate(Event(), ""), "查询失败，请稍后再试。")
            del bot.client.get_ops_list
            self.assertEqual(await bot.query_group_success_rate(Event(), ""), first)

    async def test_status_rows_only_collect_public_active_groups(self):
        client = self.ops()
        rows = await plugin.build_group_rows(client)
        self.assertEqual([row["name"] for row in rows], ["Beta", "Gamma", "Alpha", "Flaky"])
        # 专属与停用分组连快照都不请求，数据不会进入卡片渲染流程。
        self.assertEqual(client.snapshot_ids(), [1, 2, 3, 6])
        beta, gamma, alpha, flaky = rows
        self.assertEqual((alpha["rate"], alpha["health"], alpha["cells"]), (98.9, 90, ["ok", "none", "fail"]))
        self.assertEqual((alpha["total"], alpha["avail"], alpha["multiplier"]), (8, 6, 1.5))
        self.assertEqual((beta["rate"], beta["limit"]), (75.0, 2))
        self.assertEqual((gamma["rate"], gamma["total"]), (None, 0))
        self.assertEqual((flaky["rate"], flaky["health"], flaky["cells"]), (None, None, []))

    def test_missing_metrics_are_muted_instead_of_alarming(self):
        self.assertEqual(plugin._pct_color(None), plugin._MUTED)
        self.assertEqual(plugin._health_color(None), plugin._MUTED)
        self.assertEqual([plugin._health_color(score) for score in (80, 79, 50, 49, 0)],
                         [plugin._GREEN, plugin._AMBER, plugin._AMBER, plugin._RED, plugin._RED])

    def test_long_group_names_are_truncated_to_fit(self):
        draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        font = ImageFont.load_default(size=23)
        self.assertEqual(plugin._fit_text(draw, "Alpha", font, 400), "Alpha")
        name = "超长分组名称Long" * 20
        fitted = plugin._fit_text(draw, name, font, 300)
        self.assertTrue(fitted.endswith("…"))
        self.assertTrue(name.startswith(fitted[:-1]))
        self.assertLessEqual(draw.textlength(fitted, font=font), 300)

    @staticmethod
    def card_rows():
        def row(name, total, avail, limit, error, rate, health, cells, multiplier=None):
            return {"platform": "openai", "name": name, "multiplier": multiplier, "total": total,
                    "avail": avail, "limit": limit, "error": error, "rate": rate, "health": health,
                    "cells": cells}
        return [  # 覆盖 正常 / 限流 / 无渠道 / 异常 四种状态胶囊与超长分组名
            row("超长分组名称Long" * 20, 8, 6, 0, 0, 98.9, 90, ["ok", "none", "fail"] * 20, 1.5),
            row("Beta", 3, 1, 2, 0, 75.0, 40, []),
            row("Gamma", 0, 0, 0, 0, None, None, []),
            row("Delta", 4, 2, 0, 1, 50.0, 20, ["fail"] * 60, 2),
        ]

    @contextlib.contextmanager
    def card_sandbox(self):
        """图片写入临时目录；测试机不一定有 Noto CJK，改用 Pillow 内置字体。"""
        ticks = iter(range(1, 100))
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(plugin, "__file__", str(Path(directory) / "main.py")), \
                patch.object(plugin, "time", types.SimpleNamespace(time=lambda: next(ticks))), \
                patch.object(plugin, "_card_font",
                             side_effect=lambda size, bold=True: ImageFont.load_default(size=size)):
            yield Path(directory) / "status"

    def test_status_card_renders_png_and_keeps_three_latest(self):
        with self.card_sandbox() as status_dir:
            paths = [plugin.render_group_cards(self.card_rows(), "2026-11-25 12:00:00") for _ in range(4)]
            empty = plugin.render_group_cards([], "2026-11-25 12:00:00")
            with Image.open(paths[-1]) as image:
                self.assertEqual((image.format, image.size), ("PNG", (1180, 64 + 84 + 2 * 262 + 24 + 56)))
            with Image.open(empty) as image:
                self.assertEqual(image.size, (1180, 64 + 84 + 56))
            self.assertEqual(sorted(path.name for path in status_dir.iterdir()),
                             sorted(Path(path).name for path in [*paths[-2:], empty]))

    def test_status_card_header_chips_do_not_overlap(self):
        placed = []
        chip = plugin._chip

        def spy(draw, x, y, *args):
            advance = chip(draw, x, y, *args)
            placed.append((y, x, advance))
            return advance

        with self.card_sandbox(), patch.object(plugin, "_chip", side_effect=spy):
            plugin.render_group_cards(self.card_rows(), "2026-11-25 12:00:00")
        lines = {}
        for y, x, advance in placed:
            lines.setdefault(y, []).append((x, advance))
        self.assertEqual([len(chips) for chips in lines.values()], [2, 2])
        for chips in lines.values():
            for (left, advance), (right, _) in zip(chips, chips[1:]):
                # _chip 返回"胶囊宽度 + 8px 间距"；后一个胶囊必须从前一个的右边缘之后开始。
                self.assertGreaterEqual(right, left + advance - 8)


class RepositoryConsistencyTests(unittest.TestCase):
    """AGENTS.md「修改代码的约定」：配置项与版本号多处声明必须同步。"""

    root = Path(__file__).resolve().parents[1]

    def test_config_schema_defaults_and_readme_table_stay_in_sync(self):
        schema = json.loads((self.root / "_conf_schema.json").read_text(encoding="utf-8"))
        readme = (self.root / "README.md").read_text(encoding="utf-8")
        self.assertEqual(set(schema), set(plugin.DEFAULT_CONFIG))
        schema_types = {"string": str, "int": int, "bool": bool}
        for key, default in plugin.DEFAULT_CONFIG.items():
            with self.subTest(key=key):
                self.assertEqual(schema[key]["default"], default)
                self.assertIs(type(default), schema_types[schema[key]["type"]])
                self.assertTrue(f"| `{key}` |" in readme, f"README 配置表缺少 {key}")

    def test_metadata_version_matches_register_decorator(self):
        metadata = (self.root / "metadata.yaml").read_text(encoding="utf-8")
        source = (self.root / "main.py").read_text(encoding="utf-8")
        declared = re.search(r"^version:\s*v(\S+)\s*$", metadata, re.M)
        registered = re.search(r'@register\([^)]*"(\d+\.\d+\.\d+)"\)', source)
        self.assertIsNotNone(declared)
        self.assertIsNotNone(registered)
        self.assertEqual(declared.group(1), registered.group(1))


if __name__ == "__main__":
    unittest.main()
