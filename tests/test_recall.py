"""Quota recall tests with fake OneBot receipts and a virtual five-second clock."""

import asyncio
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


source = Path(__file__).resolve().parents[1] / "recall.py"
spec = importlib.util.spec_from_file_location("_quota_recall_test", source)
recall = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recall)
PRIVATE_QUOTA = "SIMULATION_ONLY_QUOTA_DETAIL_123.456"
PRIVATE_ERROR = "SIMULATION_ONLY_PRIVATE_UPSTREAM_ERROR"


class Clock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class FakeBot:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.user_id = "999999"
        self.send_response = {"message_id": -12345}
        self.send_error = None
        self.delete_plan = []
        self.login_error = None

    async def call_action(self, action, **params):
        self.calls.append({"action": action, "params": copy.deepcopy(params), "time": self.clock.now})
        if action in {"send_group_msg", "send_private_msg"}:
            if self.send_error:
                raise self.send_error
            return copy.deepcopy(self.send_response)
        if action == "get_login_info":
            if self.login_error:
                raise self.login_error
            return {"user_id": self.user_id}
        if action == "delete_msg":
            value = self.delete_plan.pop(0) if self.delete_plan else None
            if isinstance(value, Exception):
                raise value
            return value
        raise AssertionError("Unexpected fake action: " + action)

    def calls_for(self, action):
        return [item for item in self.calls if item["action"] == action]


def make_event(bot, group="200001"):
    return SimpleNamespace(
        bot=bot, platform_meta=SimpleNamespace(id="simulation-platform"),
        get_platform_name=lambda: "aiocqhttp", get_self_id=lambda: "999999",
        get_sender_id=lambda: "100001", get_group_id=lambda: group,
    )


class QuotaRecallTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="quota-recall-test-")
        self.path = Path(self.directory.name) / "pending-recalls.json"
        self.clock = Clock()
        self.bot = FakeBot(self.clock)
        self.lookups = []
        self.platform_name = "aiocqhttp"

        def get_platform_inst(platform_id):
            self.lookups.append(platform_id)
            if platform_id != "simulation-platform":
                return None
            return SimpleNamespace(bot=self.bot, meta=lambda: SimpleNamespace(name=self.platform_name))

        self.context = SimpleNamespace(get_platform_inst=get_platform_inst)
        self.managers = []

    async def asyncTearDown(self):
        for manager in self.managers:
            await manager.terminate()
        self.directory.cleanup()

    def manager(self, **kwargs):
        manager = recall.QuotaRecallManager(self.context, self.path, **kwargs)
        manager._now = lambda: self.clock.now
        manager._sleep = self.clock.sleep
        self.managers.append(manager)
        return manager

    async def drain(self, manager):
        tasks = list(manager._workers.values())
        if tasks:
            await asyncio.gather(*tasks)
        await asyncio.sleep(0)

    def write_pending(self, **overrides):
        record = {"platform_id": "simulation-platform", "self_id": "999999",
                  "message_id": -12345, "due": 100.0, "attempts": 0, "last_error": None}
        record.update(overrides)
        self.path.write_text(json.dumps({"version": 1, "pending": [record]}), encoding="utf-8")

    async def test_group_detail_is_recalled_after_five_seconds_using_own_receipt(self):
        manager = self.manager()
        self.assertTrue(await manager.send(make_event(self.bot), PRIVATE_QUOTA))
        send = self.bot.calls_for("send_group_msg")
        self.assertEqual(len(send), 1)
        self.assertEqual(send[0]["params"], {
            "group_id": 200001, "self_id": 999999,
            "message": [{"type": "at", "data": {"qq": "100001"}},
                        {"type": "text", "data": {"text": PRIVATE_QUOTA}}],
        })
        self.assertEqual(self.bot.calls_for("delete_msg"), [])
        saved_text = self.path.read_text(encoding="utf-8")
        self.assertNotIn(PRIVATE_QUOTA, saved_text)
        self.assertEqual(json.loads(saved_text)["pending"][0]["due"], 105.0)
        await self.drain(manager)
        self.assertEqual(self.clock.sleeps, [5.0])
        self.assertEqual(self.bot.calls_for("delete_msg"), [{
            "action": "delete_msg", "params": {"message_id": -12345, "self_id": 999999}, "time": 105.0,
        }])
        self.assertEqual(manager.pending, {})
        self.assertEqual(json.loads(self.path.read_text())["pending"], [])

    async def test_private_detail_uses_private_target_and_signed_string_receipt(self):
        self.bot.send_response = {"message_id": "-76543"}
        self.bot.user_id = 999999
        self.bot.delete_plan = [{}]
        manager = self.manager()
        self.assertTrue(await manager.send(make_event(self.bot, group=""), PRIVATE_QUOTA))
        await self.drain(manager)
        self.assertEqual(self.bot.calls_for("send_private_msg")[0]["params"]["user_id"], 100001)
        self.assertEqual(self.bot.calls_for("send_private_msg")[0]["params"]["message"], [
            {"type": "text", "data": {"text": PRIVATE_QUOTA}},
        ])
        self.assertEqual(self.bot.calls_for("delete_msg")[0]["params"]["message_id"], -76543)
        self.assertEqual(manager.pending, {})

    async def test_no_bot_or_wrong_platform_does_not_send_quota(self):
        manager = self.manager()
        event = make_event(None)
        self.assertFalse(await manager.send(event, PRIVATE_QUOTA))
        event = make_event(self.bot)
        event.get_platform_name = lambda: "another-platform"
        self.assertFalse(await manager.send(event, PRIVATE_QUOTA))
        self.assertEqual(self.bot.calls, [])

    async def test_bad_receipts_never_delete_guessed_id_or_resend_body(self):
        manager = self.manager()
        for receipt in (None, {}, {"message_id": None}, {"message_id": True},
                        {"message_id": 1.5}, {"message_id": "not-an-id"}, {"message_id": [12]}):
            with self.subTest(receipt=receipt):
                self.bot.send_response = receipt
                before = len(self.bot.calls_for("send_group_msg"))
                self.assertFalse(await manager.send(make_event(self.bot), PRIVATE_QUOTA))
                self.assertEqual(len(self.bot.calls_for("send_group_msg")), before + 1)
        self.assertEqual(self.bot.calls_for("delete_msg"), [])
        self.assertEqual(manager.pending, {})

    async def test_send_exception_is_not_retried_and_logs_no_quota_or_error_body(self):
        manager = self.manager()
        self.bot.send_error = RuntimeError(PRIVATE_ERROR)
        with self.assertLogs(recall.logger, level="WARNING") as logs:
            self.assertFalse(await manager.send(make_event(self.bot), PRIVATE_QUOTA))
        self.assertEqual(len(self.bot.calls_for("send_group_msg")), 1)
        self.assertEqual(self.bot.calls_for("delete_msg"), [])
        self.assertNotIn(PRIVATE_ERROR, str(logs.output))
        self.assertNotIn(PRIVATE_QUOTA, str(logs.output))

    async def test_delete_failures_retry_only_own_id_with_bounded_virtual_delays(self):
        manager = self.manager()
        self.bot.delete_plan = [RuntimeError(PRIVATE_ERROR), RuntimeError(PRIVATE_ERROR), {}]
        with self.assertLogs(recall.logger, level="WARNING") as logs:
            self.assertTrue(await manager.send(make_event(self.bot), PRIVATE_QUOTA))
            await self.drain(manager)
        deletes = self.bot.calls_for("delete_msg")
        self.assertEqual([item["time"] for item in deletes], [105.0, 107.0, 109.0])
        self.assertEqual([item["params"]["message_id"] for item in deletes], [-12345] * 3)
        self.assertEqual(len(self.bot.calls_for("send_group_msg")), 1)
        self.assertEqual(manager.pending, {})
        self.assertNotIn(PRIVATE_ERROR, str(logs.output))

    async def test_exhausted_delete_failures_remain_durable_for_restart(self):
        manager = self.manager()
        self.bot.delete_plan = [RuntimeError(PRIVATE_ERROR)] * manager.MAX_ATTEMPTS
        self.assertTrue(await manager.send(make_event(self.bot), PRIVATE_QUOTA))
        await self.drain(manager)
        self.assertEqual(len(self.bot.calls_for("delete_msg")), 3)
        saved = json.loads(self.path.read_text())["pending"]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["attempts"], 3)
        self.assertEqual(saved[0]["last_error"], "api_failed")
        self.assertNotIn(PRIVATE_QUOTA, self.path.read_text())
        self.assertNotIn(PRIVATE_ERROR, self.path.read_text())
        restored = self.manager()
        await restored.initialize()
        await self.drain(restored)
        self.assertEqual(restored.pending, {})
        self.assertIn("simulation-platform", self.lookups)
        manager.pending.clear()  # The old process no longer owns the restored record.

    async def test_initialize_recovers_expired_record_after_identity_check(self):
        self.write_pending(due=90.0, attempts=2)
        manager = self.manager()
        await manager.initialize()
        await manager.initialize()  # Must not duplicate the worker.
        await self.drain(manager)
        self.assertEqual(self.lookups, ["simulation-platform"])
        self.assertEqual([call["action"] for call in self.bot.calls], ["get_login_info", "delete_msg"])
        self.assertEqual(self.bot.calls_for("delete_msg")[0]["time"], 100.0)
        self.assertEqual(manager.pending, {})

    async def test_initialize_never_deletes_after_logged_in_qq_changes(self):
        self.write_pending()
        self.bot.user_id = "888888"
        manager = self.manager()
        await manager.initialize()
        await self.drain(manager)
        self.assertEqual(self.bot.calls_for("delete_msg"), [])
        saved = json.loads(self.path.read_text())["pending"][0]
        self.assertEqual(saved["message_id"], -12345)
        self.assertEqual(saved["last_error"], "identity_mismatch")

    async def test_missing_or_different_platform_keeps_record_without_delete(self):
        self.write_pending(platform_id="missing-platform")
        manager = self.manager()
        await manager.initialize()
        await self.drain(manager)
        self.assertEqual(self.bot.calls, [])
        self.assertEqual(len(manager.pending), 1)
        self.assertEqual(next(iter(manager.pending.values()))["last_error"], "platform_unavailable")
        self.write_pending()
        self.platform_name = "another-platform"
        other = self.manager()
        await other.initialize()
        await self.drain(other)
        self.assertEqual(self.bot.calls_for("delete_msg"), [])

    async def test_save_failure_immediately_recalls_and_true_does_not_mean_resend(self):
        manager = self.manager()
        with patch.object(manager, "_save", return_value=False):
            self.assertTrue(await manager.send(make_event(self.bot), PRIVATE_QUOTA))
        self.assertEqual(len(self.bot.calls_for("send_group_msg")), 1)
        self.assertEqual(self.bot.calls_for("delete_msg")[0]["time"], 100.0)
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(manager.pending, {})

    async def test_atomic_write_error_triggers_immediate_recall_and_no_text_file(self):
        manager = self.manager()
        with patch.object(recall.os, "replace", side_effect=OSError(PRIVATE_ERROR)):
            self.assertTrue(await manager.send(make_event(self.bot), PRIVATE_QUOTA))
        self.assertEqual(self.bot.calls_for("delete_msg")[0]["time"], 100.0)
        self.assertEqual(list(self.path.parent.iterdir()), [])

    async def test_save_and_immediate_delete_failure_returns_false_without_resend(self):
        manager = self.manager()
        self.bot.delete_plan = [RuntimeError(PRIVATE_ERROR)]
        with patch.object(manager, "_save", return_value=False):
            self.assertFalse(await manager.send(make_event(self.bot), PRIVATE_QUOTA))
        self.assertEqual(len(self.bot.calls_for("send_group_msg")), 1)
        self.assertEqual(len(manager.pending), 1)
        await self.drain(manager)
        self.assertEqual(manager.pending, {})
        self.assertEqual(len(self.bot.calls_for("send_group_msg")), 1)

    async def test_terminate_recalls_immediately_instead_of_waiting_five_seconds(self):
        manager = self.manager()
        sleeping = asyncio.Event()

        async def blocked_sleep(seconds):
            sleeping.set()
            await asyncio.Event().wait()

        manager._sleep = blocked_sleep
        self.assertTrue(await manager.send(make_event(self.bot), PRIVATE_QUOTA))
        await sleeping.wait()
        await manager.terminate()
        self.assertEqual(self.bot.calls_for("delete_msg")[0]["time"], 100.0)
        self.assertEqual(manager.pending, {})
        self.assertEqual(manager._workers, {})
        self.assertFalse(await manager.send(make_event(self.bot), PRIVATE_QUOTA))

    async def test_terminate_failure_retains_record(self):
        self.write_pending(due=105.0)
        manager = self.manager()
        self.bot.delete_plan = [RuntimeError(PRIVATE_ERROR)]
        await manager.terminate()
        self.assertEqual(len(manager.pending), 1)
        self.assertEqual(json.loads(self.path.read_text())["pending"][0]["last_error"], "api_failed")

    async def test_corrupt_state_is_preserved_and_disables_sending(self):
        self.path.write_text("not-json", encoding="utf-8")
        manager = self.manager()
        await manager.initialize()
        self.assertFalse(await manager.send(make_event(self.bot), PRIVATE_QUOTA))
        self.assertEqual(self.path.read_text(), "not-json")
        self.assertEqual(self.bot.calls, [])

    async def test_multiple_sent_receipts_only_delete_their_own_ids(self):
        manager = self.manager()
        self.bot.send_response = {"message_id": -100}
        self.assertTrue(await manager.send(make_event(self.bot), PRIVATE_QUOTA))
        self.bot.send_response = {"message_id": 200}
        self.assertTrue(await manager.send(make_event(self.bot), PRIVATE_QUOTA))
        await self.drain(manager)
        self.assertEqual({item["params"]["message_id"] for item in self.bot.calls_for("delete_msg")}, {-100, 200})
        self.assertEqual(len(self.bot.calls_for("send_group_msg")), 2)
        self.assertEqual(manager.pending, {})


if __name__ == "__main__":
    unittest.main()
