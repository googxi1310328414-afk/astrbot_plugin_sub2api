"""Offline command and durable-state simulations; no real account or network access."""
import asyncio
import copy
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch


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
        self.assertIn("未识别的插件指令", text)
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
        self.assertIn("0.300", quota_text)
        self.assertIn("2.300", quota_text)
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
        self.assertIn("本次", result)
        self.assertIn("0.300", result)
        self.assertIn("user2@example.test", result)
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
        self.assertIn("赔偿了 0.500", result)
        self.assertIn("user2@example.test", result)
        self.assertNotIn("1.500", result)
        self.assertNotIn("2.500", result)
        self.assertEqual(self.recall.messages, [])
        self.assertEqual(self.client.users[1]["balance"], Decimal("1.500"))
        self.assertEqual(self.client.users[2]["balance"], Decimal("2.500"))

    async def test_negative_robbery_balances_are_rejected_without_randomness_or_mutation(self):
        cases = (
            ("-1.000", "2.000", "你的账号余额为负", [1]),
            ("-0.0001", "2.000", "你的账号余额为负", [1]),
            ("2.000", "-1.000", "对方账号余额为负", [1, 2]),
            ("2.000", "-0.0001", "对方账号余额为负", [1, 2]),
            ("-0.0001", "-2.000", "你的账号余额为负", [1]),
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
                    self.assertNotIn("对方账号余额为负", result)
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
        self.assertIn("你的账号余额为负", result)
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
        self.assertIn("对方账号余额为负", result)
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
                self.assertIn("不足以承担打劫失败时的 0.500 赔款", result)
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
                self.assertIn("10001", self.bot.state["robbery_ts"])
                self.assertEqual(self.recall.messages, [])

    async def test_insufficient_compensation_is_rejected_before_randomness(self):
        self.client.users[1]["balance"] = Decimal("0.4999")
        with patch.object(plugin.random, "random") as chance, patch.object(plugin, "save_state", wraps=plugin.save_state) as save:
            result = await self.invoke("rob", Event(targets=("10002",)))
        self.assertIn("不足以承担打劫失败时的 0.500 赔款", result)
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
        self.assertIn("本次获得 0.000", result)
        self.assertIn("user2@example.test", result)
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
        self.assertIn("user1@example.test", public[0])
        self.assertEqual(len(self.recall.messages), 1)
        sent_event, quota_text = self.recall.messages[0]
        self.assertIs(sent_event, event)
        for amount in ("123.456", "7.890", "987.654"):
            self.assertNotIn(amount, public[0])
        self.assertIn("123.456", quota_text)
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
        for config in ({"robbery_cooldown": -1}, {"robbery_cooldown": True}, {"allow_bind_admin": "false"}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                plugin.Sub2ApiPlugin(object(), config)


if __name__ == "__main__":
    unittest.main()
