"""Isolated wallet recovery tests; no external service, real funds or QQ sends."""

import asyncio
import copy
import hashlib
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


def load_wallet():
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
        on_platform_loaded=decorator, permission_type=decorator, llm_tool=decorator,
        PermissionType=types.SimpleNamespace(ADMIN="admin"),
    )
    modules["astrbot.api.star"].Star = Star
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].register = decorator
    modules["astrbot.api.message_components"].At = At
    directory = Path(__file__).resolve().parents[1]
    name = "_ledger_recovery_simulation"
    package = types.ModuleType(name)
    package.__path__ = [str(directory)]
    spec = importlib.util.spec_from_file_location(name + ".main", directory / "main.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {**modules, name: package, spec.name: module}):
        spec.loader.exec_module(module)
        recovery = sys.modules[name + ".recovery"]
    return module, recovery


wallet, recovery = load_wallet()


class EvidenceClient:
    """Track confirmed ledger rows separately from current account balances."""

    def __init__(self):
        self.users = {
            uid: {"id": uid, "email": f"user{uid}@example.test", "balance": Decimal("2.000"),
                  "status": "active", "role": "user", "frozen_balance": 0}
            for uid in (1, 2, 3)
        }
        self.ledger = []
        self.lookups = []
        self.posts = []
        self.reads = []
        self.plans = []
        self.lookup_error = None
        self.post_started = asyncio.Queue()
        self.post_release = None
        self.hidden_notes = set()
        self.response_overrides = {}
        self.now = lambda: 1000.0

    def record_commit(self, uid, amount, operation, notes, created_at=None):
        amount = Decimal(str(amount))
        self.users[uid]["balance"] += amount if operation == "add" else -amount
        record = {"id": len(self.ledger) + 1, "user_id": uid, "used_by": uid,
                  "amount": str(amount), "operation": operation, "notes": notes,
                  "created_at": self.now() if created_at is None else created_at,
                  "value": str(amount if operation == "add" else -amount),
                  "type": "admin_balance", "status": "used",
                  "balance": str(self.users[uid]["balance"])}
        self.ledger.append(record)
        return copy.deepcopy(record)

    async def get_user(self, uid):
        self.reads.append(uid)
        return copy.deepcopy(self.users[uid])

    async def lookup_balance_operation(self, uid, amount, operation, notes, created_at):
        self.lookups.append((uid, Decimal(str(amount)), operation, notes, created_at))
        if self.lookup_error is not None:
            raise self.lookup_error
        if notes in self.hidden_notes:
            return None
        matches = [record for record in self.ledger
                   if record["user_id"] == uid and Decimal(record["amount"]) == Decimal(str(amount))
                   and record["operation"] == operation and record["notes"] == notes]
        if len(matches) > 1:
            raise wallet.ApiError("模拟重复账务证据，必须人工核对。")
        return copy.deepcopy(matches[0]) if matches else None

    async def balance_op(self, uid, amount, operation, notes):
        self.posts.append((uid, Decimal(str(amount)), operation, notes))
        self.post_started.put_nowait(notes)
        if self.post_release is not None:
            await self.post_release.wait()
        plan = self.plans.pop(0) if self.plans else None
        if isinstance(plan, BaseException):
            raise plan
        self.record_commit(uid, amount, operation, notes)
        if plan == "drop_after_commit":
            raise wallet.OutcomeUnknown("模拟已记账后响应丢失。")
        if plan == "cancel_after_commit":
            raise asyncio.CancelledError
        return {**copy.deepcopy(self.users[uid]), **self.response_overrides}


class LedgerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="ledger-recovery-test-")
        self.addCleanup(self.directory.cleanup)
        self.state_path = Path(self.directory.name) / "state.json"
        self.state_patch = patch.object(wallet, "STATE_FILE", str(self.state_path))
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.recall = types.SimpleNamespace(initialize=AsyncMock(), terminate=AsyncMock(), send=AsyncMock(return_value=True))
        self.recall_patch = patch.object(wallet, "QuotaRecallManager", return_value=self.recall)
        self.recall_patch.start()
        self.addCleanup(self.recall_patch.stop)
        self.now = datetime(2026, 9, 20, 10, 0, tzinfo=wallet.TZ).timestamp()
        self.time_patch = patch.object(recovery.time, "time", side_effect=lambda: self.now)
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)
        self.context = types.SimpleNamespace(send_message=AsyncMock())
        self.owner = wallet.Sub2ApiPlugin(self.context)
        self.addAsyncCleanup(self.owner.terminate)
        self.client = EvidenceClient()
        self.client.now = lambda: self.now
        self.owner.client = self.client
        self.owner.cfg["recovery_enabled"] = True
        self.owner.cfg["recovery_interval_seconds"] = 30
        self.owner.state["bindings"] = {
            str(10000 + uid): {"uid": uid, "email": f"user{uid}@example.test", "approved": True}
            for uid in (1, 2, 3)
        }
        self.manager = recovery.LedgerRecovery(self.owner)
        self.addAsyncCleanup(self.manager.terminate)

    def transaction(self, kind="checkin", statuses=None, *, phase=None, decision=None):
        txid = f"{len(self.owner.state['transactions']) + 1:032x}"
        amount = "0.500" if kind == "rob_failure" else "0.300"
        if kind == "checkin":
            steps = [{"uid": 1, "operation": "add"}]
        else:
            source, destination = (1, 2) if kind == "rob_failure" else (2, 1)
            steps = [{"uid": source, "operation": "subtract"},
                     {"uid": destination, "operation": "add"}]
        statuses = statuses or (["unknown"] if kind == "checkin" else ["applied", "unknown"])
        completed = 0
        for status in statuses:
            if status != "applied":
                break
            completed += 1
        if phase is None:
            phase = (f"step_{completed}_applied" if completed == len(steps) or statuses[completed] == "not_sent"
                     else f"step_{completed + 1}_{'rejected' if statuses[completed] == 'not_applied' else 'unknown'}")
        tx = {"id": txid, "kind": kind, "qq": "10001", "uids": sorted({step["uid"] for step in steps}),
              "amount": amount, "steps": steps, "created_at": self.now - 60,
              "balances_before": {str(step["uid"]): "2.000" for step in steps},
              "status": "pending", "phase": phase, "completed_steps": completed,
              "step_outcomes": [{"status": status, **({"category": "balance_negative_rejected", "code": 500}
                                if status == "not_applied" else {})} for status in statuses]}
        if kind == "checkin":
            tx["date"] = "2026-09-19"
        else:
            tx["target_qq"] = "10002"
        tx["request_keys"] = [recovery.operation_key(step["uid"], Decimal(amount), step["operation"],
                                                   recovery.operation_notes(tx, index))
                              for index, step in enumerate(steps)]
        if decision:
            tx["recovery"] = {"decision": decision, "attempts": 0, "next_check_at": 0}
        self.assertTrue(recovery.valid_transaction(tx), tx)
        self.owner.state["transactions"][txid] = tx
        self.owner._save()
        return tx

    def commit_step(self, tx, index):
        step = tx["steps"][index]
        return self.client.record_commit(step["uid"], tx["amount"], step["operation"],
                                         recovery.operation_notes(tx, index), tx["created_at"])

    def reload_owner(self):
        self.owner = wallet.Sub2ApiPlugin(self.context)
        self.owner.client = self.client
        self.owner.cfg["recovery_enabled"] = True
        self.owner.cfg["recovery_interval_seconds"] = 30
        self.addAsyncCleanup(self.owner.terminate)
        self.manager = recovery.LedgerRecovery(self.owner)
        self.addAsyncCleanup(self.manager.terminate)
        return self.owner

    def pending_recovery_write(self, mode):
        tx = self.transaction("rob_success", ["applied", "not_sent" if mode == "complete" else "not_applied"])
        self.commit_step(tx, 0)
        if mode == "refund":
            notes = f"QQ互动 tx={tx['id']} recovery=refund step=1"
            tx["recovery"] = {"decision": "refund", "attempts": 1, "next_check_at": 0,
                              "refund": {"status": "not_applied", "category": "balance_negative_rejected",
                                         "code": 500, "created_at": self.now - 30, "notes": notes,
                                         "key": recovery.operation_key(2, tx["amount"], "add", notes)}}
            self.assertTrue(recovery.valid_transaction(tx))
            self.owner._save()
        return tx

    @staticmethod
    def recovery_write_outcome(tx, mode):
        return tx["step_outcomes"][1] if mode == "complete" else tx["recovery"]["refund"]

    async def test_operation_keys_keep_original_wallet_protocol_and_reject_tampering(self):
        tx = self.transaction()
        notes = f"QQ互动 tx={tx['id']} step=1 kind=checkin qq=10001"
        encoded = json.dumps([1, "add", "0.300", notes], ensure_ascii=False, separators=(",", ":")).encode()
        self.assertEqual(recovery.operation_notes(tx, 0), notes)
        self.assertEqual(tx["request_keys"], [hashlib.sha256(encoded).hexdigest()])
        original = copy.deepcopy(tx)
        for change in ("key", "amount", "shape", "phase"):
            with self.subTest(change=change):
                tx.clear()
                tx.update(copy.deepcopy(original))
                if change == "key":
                    tx["request_keys"][0] = "0" * 64
                elif change == "amount":
                    tx["amount"] = "0.0001"
                elif change == "shape":
                    tx["steps"][0]["operation"] = "subtract"
                else:
                    tx["phase"] = "unrecognized_phase"
                self.assertFalse(recovery.valid_transaction(tx))
                await self.manager.run_once(force=True)
                self.assertEqual(tx["status"], "pending")
        self.assertEqual(self.client.posts, [])

    async def test_committed_checkin_recovers_after_restart_using_original_day_once(self):
        tx = self.transaction()
        self.commit_step(tx, 0)
        txid = tx["id"]
        self.reload_owner()
        await self.manager.run_once(force=True)
        current = self.owner.state["transactions"][txid]
        self.assertEqual(current["status"], "completed")
        self.assertEqual(current["completed_steps"], 1)
        self.assertEqual(self.owner.state["checkin"]["10001"], "2026-09-19")
        self.assertEqual(self.owner.state["checkin_uid"]["1"], "2026-09-19")
        self.assertEqual(self.client.users[1]["balance"], Decimal("2.300"))
        self.assertEqual(self.client.posts, [])
        self.reload_owner()
        await self.manager.run_once(force=True)
        self.assertEqual(self.client.posts, [])
        self.assertEqual(len(self.client.ledger), 1)

    async def test_recovered_old_checkin_never_moves_newer_daily_markers_backwards(self):
        tx = self.transaction()
        self.commit_step(tx, 0)
        self.owner.state["checkin"]["10001"] = "2026-09-20"
        self.owner.state["checkin_uid"]["1"] = "2026-09-20"
        self.owner._save()
        await self.manager.run_once(force=True)
        self.assertEqual(tx["status"], "completed")
        self.assertEqual(self.owner.state["checkin"]["10001"], "2026-09-20")
        self.assertEqual(self.owner.state["checkin_uid"]["1"], "2026-09-20")
        self.assertEqual(self.client.posts, [])

    async def test_no_ledger_evidence_never_unlocks_or_replays_based_on_current_balance(self):
        tx = self.transaction()
        for value in ("2.000", "2.300", "1.700"):
            with self.subTest(balance=value):
                self.client.users[1]["balance"] = Decimal(value)
                await self.manager.run_once(force=True)
                self.assertEqual(tx["status"], "pending")
                with self.assertRaises(wallet.UserError):
                    self.owner._require_clear(1)
        self.assertEqual(self.client.posts, [])
        self.assertEqual(self.owner.state["checkin"], {})

    async def test_unsent_intent_is_failed_without_post_or_checkin_markers(self):
        tx = self.transaction(statuses=["not_sent"], phase="intent")
        await self.manager.run_once(force=True)
        self.assertEqual(tx["status"], "failed")
        self.assertEqual(self.client.posts, [])
        self.assertEqual(self.owner.state["checkin"], {})
        self.owner._require_clear(1)

    async def test_known_unsent_second_leg_completes_only_that_leg_once(self):
        tx = self.transaction("rob_success", ["applied", "not_sent"])
        self.commit_step(tx, 0)
        await self.manager.run_once(force=True)
        self.assertEqual(self.client.posts, [(1, Decimal("0.300"), "add", recovery.operation_notes(tx, 1))])
        self.assertEqual(tx["status"], "completed")
        self.assertEqual(tx["recovery"]["decision"], "complete")
        self.assertEqual(tx["completed_steps"], 2)
        self.assertEqual(sum(user["balance"] for user in self.client.users.values()), Decimal("6.000"))
        await self.manager.run_once(force=True)
        self.assertEqual(len(self.client.posts), 1)

    async def test_applied_second_leg_with_lost_response_is_confirmed_without_recredit(self):
        tx = self.transaction("rob_success", ["applied", "unknown"])
        self.commit_step(tx, 0)
        self.commit_step(tx, 1)
        await self.manager.run_once(force=True)
        self.assertEqual(tx["status"], "completed")
        self.assertEqual(tx["completed_steps"], 2)
        self.assertEqual(self.client.posts, [])
        self.assertEqual(self.client.users[1]["balance"], Decimal("2.300"))

    async def test_definitive_second_leg_rejection_refunds_original_source_once(self):
        tx = self.transaction("rob_success", ["applied", "not_applied"])
        self.commit_step(tx, 0)
        await self.manager.run_once(force=True)
        notes = f"QQ互动 tx={tx['id']} recovery=refund step=1"
        self.assertEqual(self.client.posts, [(2, Decimal("0.300"), "add", notes)])
        self.assertEqual(tx["status"], "resolved")
        self.assertTrue(tx.get("resolution_note"))
        self.assertEqual(tx["recovery"]["decision"], "refund")
        self.assertEqual(self.client.users[1]["balance"], Decimal("2.000"))
        self.assertEqual(self.client.users[2]["balance"], Decimal("2.000"))
        await self.manager.run_once(force=True)
        self.assertEqual(len(self.client.posts), 1)

    async def test_refund_committed_then_disconnected_is_recovered_read_only_after_restart(self):
        tx = self.transaction("rob_success", ["applied", "not_applied"])
        self.commit_step(tx, 0)
        notes = f"QQ互动 tx={tx['id']} recovery=refund step=1"
        self.client.plans = ["drop_after_commit"]
        self.client.hidden_notes.add(notes)
        await self.manager.run_once(force=True)
        self.assertEqual(tx["status"], "pending")
        self.assertEqual(tx["recovery"]["refund"]["status"], "unknown")
        self.assertEqual(len(self.client.posts), 1)
        self.reload_owner()
        self.client.hidden_notes.clear()
        await self.manager.run_once(force=True)
        self.assertEqual(self.owner.state["transactions"][tx["id"]]["status"], "resolved")
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(self.client.users[2]["balance"], Decimal("2.000"))

    async def test_unknown_refund_without_history_is_never_posted_again(self):
        tx = self.transaction("rob_success", ["applied", "not_applied"])
        self.commit_step(tx, 0)
        self.client.plans = [wallet.OutcomeUnknown("模拟退款响应未知。")]
        await self.manager.run_once(force=True)
        for _ in range(3):
            await self.manager.run_once(force=True)
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(tx["status"], "pending")
        self.assertEqual(tx["recovery"]["decision"], "refund")

    async def test_definitively_unapplied_refund_retries_after_backoff_with_identical_operation(self):
        tx = self.transaction("rob_success", ["applied", "not_applied"])
        self.commit_step(tx, 0)
        self.client.plans = [wallet.ApiError("模拟明确未退款。", code=500, category="balance_negative_rejected")]
        await self.manager.run_once(force=True)
        self.assertEqual(tx["recovery"]["refund"]["status"], "not_applied")
        self.assertEqual(tx["recovery"]["decision"], "refund")
        self.assertEqual(len(self.client.posts), 1)
        self.assertGreaterEqual(tx["recovery"]["next_check_at"] - self.now, 30)
        self.assertLessEqual(tx["recovery"]["next_check_at"] - self.now, 900)
        await self.manager.run_once()
        self.assertEqual(len(self.client.posts), 1)
        self.now = tx["recovery"]["next_check_at"] + 0.01
        await self.manager.run_once()
        self.assertEqual(tx["status"], "resolved")
        self.assertEqual(len(self.client.posts), 2)
        self.assertEqual(self.client.posts[0], self.client.posts[1])
        self.assertEqual(self.client.users[2]["balance"], Decimal("2.000"))
        refund_rows = [row for row in self.client.ledger if "recovery=refund" in row["notes"]]
        self.assertEqual(len(refund_rows), 1)

    async def test_completion_decision_does_not_switch_to_refund_after_explicit_rejection(self):
        tx = self.transaction("rob_success", ["applied", "not_sent"])
        self.commit_step(tx, 0)
        self.client.plans = [wallet.ApiError("模拟第二步明确拒绝。", code=422, category="request_rejected")]
        await self.manager.run_once(force=True)
        self.assertEqual(tx["status"], "pending")
        self.assertEqual(tx["recovery"]["decision"], "complete")
        await self.manager.run_once(force=True)
        self.assertEqual(tx["status"], "completed")
        self.assertEqual(tx["recovery"]["decision"], "complete")
        self.assertEqual(len(self.client.posts), 2)
        self.assertEqual(self.client.posts[0], self.client.posts[1])
        self.assertTrue(all(item[0] == 1 and "recovery=refund" not in item[3] for item in self.client.posts))
        self.assertEqual(self.client.users[1]["balance"], Decimal("2.300"))
        self.assertEqual(self.client.users[2]["balance"], Decimal("1.700"))

    async def test_legacy_rejected_phase_without_explicit_metadata_never_triggers_refund(self):
        tx = self.transaction("rob_success", ["applied", "unknown"])
        self.commit_step(tx, 0)
        tx.pop("request_keys")
        tx.pop("step_outcomes")
        tx["phase"] = "step_2_rejected"
        self.owner._save()
        self.reload_owner()
        await self.manager.run_once(force=True)
        current = self.owner.state["transactions"][tx["id"]]
        self.assertEqual(current["status"], "pending")
        self.assertEqual(self.client.posts, [])
        self.assertEqual(self.client.users[2]["balance"], Decimal("1.700"))

    async def test_duplicate_ledger_evidence_stays_locked_without_post(self):
        tx = self.transaction()
        row = self.commit_step(tx, 0)
        self.client.ledger.append({**row, "id": 2})
        await self.manager.run_once(force=True)
        self.assertEqual(tx["status"], "pending")
        self.assertEqual(self.client.posts, [])
        with self.assertRaises(wallet.UserError):
            self.owner._require_clear(1)

    async def test_storage_failure_is_saved_before_unlocking_and_never_recredits(self):
        tx = self.transaction()
        self.commit_step(tx, 0)
        with patch.object(wallet, "save_state", side_effect=wallet.StateError("模拟存储故障。")):
            with self.assertRaises(wallet.StateError):
                await self.manager.run_once(force=True)
        status = json.loads(self.manager.status_path.read_text(encoding="utf-8"))
        self.assertTrue(status["storage_failed"])
        self.assertTrue(self.owner._state_failed)
        self.assertEqual(wallet.load_state(str(self.state_path))["transactions"][tx["id"]]["status"], "pending")
        self.assertEqual(self.client.posts, [])
        await self.manager.run_once(force=True)
        self.assertFalse(self.owner._state_failed)
        self.assertEqual(tx["status"], "completed")
        stored = wallet.load_state(str(self.state_path))
        self.assertEqual(stored["transactions"][tx["id"]]["status"], "completed")
        self.assertEqual(stored["checkin"]["10001"], tx["date"])
        self.assertEqual(self.client.posts, [])
        self.assertEqual(self.client.users[1]["balance"], Decimal("2.300"))

    async def test_post_commit_storage_failure_never_reposts_with_or_without_restart(self):
        for restart in (False, True):
            with self.subTest(restart=restart):
                tx = self.transaction("rob_success", ["applied", "not_sent"])
                self.commit_step(tx, 0)
                initial_posts = len(self.client.posts)
                expected_balance = self.client.users[1]["balance"] + Decimal(tx["amount"])
                real_save = wallet.save_state

                def fail_after_post(state, path):
                    if len(self.client.posts) > initial_posts:
                        raise wallet.StateError("模拟记账后无法保存。")
                    return real_save(state, path)

                with patch.object(wallet, "save_state", side_effect=fail_after_post):
                    with self.assertRaises(wallet.StateError):
                        await self.manager.run_once(force=True)
                self.assertTrue(self.owner._state_failed)
                self.assertEqual(len(self.client.posts), initial_posts + 1)
                stored = wallet.load_state(str(self.state_path))["transactions"][tx["id"]]
                self.assertEqual(stored["step_outcomes"][1]["status"], "inflight")
                if restart:
                    self.reload_owner()
                await self.manager.run_once(force=True)
                self.assertEqual(self.owner.state["transactions"][tx["id"]]["status"], "completed")
                self.assertFalse(self.owner._state_failed)
                self.assertEqual(len(self.client.posts), initial_posts + 1)
                self.assertEqual(self.client.users[1]["balance"], expected_balance)

    async def test_pre_send_save_failure_restores_known_unsent_outcome_before_safe_retry(self):
        for mode in ("complete", "refund"):
            for persisted_inflight in (False, True):
                with self.subTest(mode=mode, persisted_inflight=persisted_inflight):
                    tx = self.pending_recovery_write(mode)
                    previous = copy.deepcopy(self.recovery_write_outcome(tx, mode))
                    old_phase, old_completed = tx["phase"], tx["completed_steps"]
                    initial_posts = len(self.client.posts)
                    real_save = wallet.save_state
                    failed_save_count = 0

                    def fail_before_post(state, path):
                        nonlocal failed_save_count
                        failed_save_count += 1
                        candidate = state["transactions"][tx["id"]]
                        if self.recovery_write_outcome(candidate, mode)["status"] == "inflight":
                            if persisted_inflight:
                                real_save(state, path)
                            raise wallet.StateError("模拟发送前持久化失败。")
                        return real_save(state, path)

                    with patch.object(wallet, "save_state", side_effect=fail_before_post):
                        with self.assertRaises(wallet.StateError):
                            await self.manager.run_once(force=True)
                    if mode == "complete":
                        self.assertEqual(failed_save_count, 3)
                    self.assertTrue(self.owner._state_failed)
                    self.assertEqual(len(self.client.posts), initial_posts)
                    self.assertEqual(self.recovery_write_outcome(tx, mode), previous)
                    self.assertEqual((tx["phase"], tx["completed_steps"]), (old_phase, old_completed))
                    if persisted_inflight:
                        stored = wallet.load_state(str(self.state_path))["transactions"][tx["id"]]
                        self.assertEqual(self.recovery_write_outcome(stored, mode)["status"], "inflight")

                    saved_outcomes = []

                    def track_repaired_save(state, path):
                        saved_outcomes.append(copy.deepcopy(self.recovery_write_outcome(state["transactions"][tx["id"]], mode)))
                        return real_save(state, path)

                    original_post = self.client.balance_op

                    async def inspect_durable_intent(*args):
                        self.assertEqual(saved_outcomes[0], previous)
                        stored = wallet.load_state(str(self.state_path))["transactions"][tx["id"]]
                        self.assertEqual(self.recovery_write_outcome(stored, mode)["status"], "inflight")
                        return await original_post(*args)

                    with patch.object(wallet, "save_state", side_effect=track_repaired_save), patch.object(
                        self.client, "balance_op", side_effect=inspect_durable_intent,
                    ):
                        await self.manager.run_once(force=True)
                    self.assertFalse(self.owner._state_failed)
                    self.assertEqual(len(self.client.posts), initial_posts + 1)
                    self.assertEqual(tx["status"], "completed" if mode == "complete" else "resolved")

    async def test_crash_after_failed_inflight_save_keeps_disk_intent_unknown_without_post(self):
        for mode in ("complete", "refund"):
            with self.subTest(mode=mode):
                tx = self.pending_recovery_write(mode)
                initial_posts = len(self.client.posts)
                real_save = wallet.save_state

                def replace_succeeded_but_sync_failed(state, path):
                    candidate = state["transactions"][tx["id"]]
                    real_save(state, path)
                    if self.recovery_write_outcome(candidate, mode)["status"] == "inflight":
                        raise wallet.StateError("模拟replace完成但目录同步失败。")

                with patch.object(wallet, "save_state", side_effect=replace_succeeded_but_sync_failed):
                    with self.assertRaises(wallet.StateError):
                        await self.manager.run_once(force=True)
                self.assertEqual(len(self.client.posts), initial_posts)
                self.assertNotEqual(self.recovery_write_outcome(tx, mode)["status"], "inflight")
                self.reload_owner()
                await self.manager.run_once(force=True)
                current = self.owner.state["transactions"][tx["id"]]
                self.assertEqual(current["status"], "pending")
                self.assertEqual(self.recovery_write_outcome(current, mode)["status"], "unknown")
                self.assertEqual(len(self.client.posts), initial_posts)

    async def test_initial_wallet_post_is_not_attempted_when_inflight_save_fails(self):
        tx = self.transaction(statuses=["not_sent"], phase="intent")
        real_save = wallet.save_state

        def fail_initial_inflight(state, path):
            if state["transactions"][tx["id"]]["step_outcomes"][0]["status"] == "inflight":
                raise wallet.StateError("模拟原始请求发送前保存失败。")
            return real_save(state, path)

        with patch.object(wallet, "save_state", side_effect=fail_initial_inflight):
            with self.assertRaises(wallet.StateError):
                await self.owner._execute_tx(tx)
        self.assertEqual(self.client.posts, [])
        self.assertTrue(self.owner._state_failed)
        self.assertEqual(tx["step_outcomes"], [{"status": "not_sent"}])
        self.assertEqual(tx["phase"], "intent")
        await self.manager.run_once(force=True)
        self.assertFalse(self.owner._state_failed)
        self.assertEqual(tx["status"], "failed")
        self.assertEqual(self.client.posts, [])
        new_tx = self.transaction(statuses=["not_sent"], phase="intent")
        await self.owner._execute_tx(new_tx)
        self.owner._complete_tx(new_tx)
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(self.client.users[1]["balance"], Decimal("2.300"))

    async def test_untrusted_not_applied_category_never_authorizes_refund(self):
        tx = self.transaction("rob_success", ["applied", "not_applied"])
        self.commit_step(tx, 0)
        for category, code in (("outcome_unknown", None), ("history_read_failed", None),
                               ("unexpected_upstream_reason", None), ("http_rejected", 500),
                               ("http_rejected", 404), ("balance_negative_rejected", 422)):
            with self.subTest(category=category, code=code):
                tx["step_outcomes"][1]["category"] = category
                tx["step_outcomes"][1]["code"] = code
                self.assertFalse(recovery.valid_transaction(tx))
                await self.manager.run_once(force=True)
                self.assertEqual(tx["status"], "pending")
        self.assertEqual(self.client.posts, [])

    async def test_legacy_first_applied_can_finish_but_second_intent_cannot_be_assumed_unsent(self):
        for phase, expected_status, new_posts in (("step_1_applied", "completed", 1),
                                                   ("step_2_intent", "pending", 0)):
            with self.subTest(phase=phase):
                tx = self.transaction("rob_success", ["applied", "unknown"])
                self.commit_step(tx, 0)
                tx.pop("request_keys")
                tx.pop("step_outcomes")
                tx["phase"] = phase
                self.owner._save()
                self.reload_owner()
                initial_posts = len(self.client.posts)
                await self.manager.run_once(force=True)
                current = self.owner.state["transactions"][tx["id"]]
                self.assertEqual(current["status"], expected_status)
                self.assertEqual(len(self.client.posts) - initial_posts, new_posts)
                if new_posts:
                    self.assertEqual(self.client.posts[-1], (1, Decimal("0.300"), "add",
                                                            recovery.operation_notes(tx, 1)))

    async def test_legacy_unknown_first_leg_is_confirmed_then_only_unsent_second_leg_runs(self):
        for phase in ("step_1_unknown", "step_1_intent", "step_1_rejected"):
            with self.subTest(phase=phase):
                tx = self.transaction("rob_success", ["unknown", "not_sent"])
                self.commit_step(tx, 0)
                tx.pop("request_keys")
                tx.pop("step_outcomes")
                tx["phase"] = phase
                self.owner._save()
                self.reload_owner()
                initial_posts = len(self.client.posts)
                await self.manager.run_once(force=True)
                current = self.owner.state["transactions"][tx["id"]]
                self.assertEqual(current["status"], "completed")
                self.assertEqual(current["completed_steps"], 2)
                self.assertEqual(self.client.posts[initial_posts:], [
                    (1, Decimal("0.300"), "add", recovery.operation_notes(tx, 1)),
                ])
                await self.manager.run_once(force=True)
                self.assertEqual(len(self.client.posts), initial_posts + 1)

    async def test_boolean_response_id_requires_ledger_confirmation_and_evidence_is_sanitized(self):
        tx = self.transaction("rob_success", ["applied", "not_sent"])
        self.commit_step(tx, 0)
        self.client.response_overrides = {"id": True}
        await self.manager.run_once(force=True)
        self.assertEqual(tx["status"], "pending")
        self.assertEqual(tx["step_outcomes"][1]["status"], "unknown")
        self.assertEqual(len(self.client.posts), 1)
        self.client.ledger[-1].update(code="SIMULATION_PRIVATE_REDEEM_CODE",
                                      private_error="SIMULATION_PRIVATE_DIAGNOSTIC")
        await self.manager.run_once(force=True)
        self.assertEqual(tx["status"], "completed")
        self.assertEqual(len(self.client.posts), 1)
        evidence = tx["step_outcomes"][1]["evidence"]
        self.assertTrue(set(evidence).issubset({"source", "id", "value", "used_at", "created_at"}))
        serialized = self.state_path.read_text(encoding="utf-8")
        self.assertNotIn("SIMULATION_PRIVATE_REDEEM_CODE", serialized)
        self.assertNotIn("SIMULATION_PRIVATE_DIAGNOSTIC", serialized)

    async def test_cancellation_during_completion_preserves_unknown_and_releases_locks(self):
        tx = self.transaction("rob_success", ["applied", "not_sent"])
        self.commit_step(tx, 0)
        self.client.post_release = asyncio.Event()
        task = asyncio.create_task(self.manager.run_once(force=True))
        try:
            await asyncio.wait_for(self.client.post_started.get(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            self.client.post_release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(self.owner._lock.locked())
        stored = wallet.load_state(str(self.state_path))["transactions"][tx["id"]]
        self.assertIn(stored["step_outcomes"][1]["status"], ("inflight", "unknown"))
        await self.manager.run_once(force=True)
        self.assertEqual(tx["status"], "pending")
        self.assertEqual(len(self.client.posts), 1)

    async def test_cancellation_after_commit_is_recovered_after_restart_without_reposting(self):
        tx = self.transaction("rob_success", ["applied", "not_sent"])
        self.commit_step(tx, 0)
        self.client.plans = ["cancel_after_commit"]
        with self.assertRaises(asyncio.CancelledError):
            await self.manager.run_once(force=True)
        self.assertFalse(self.owner._lock.locked())
        self.reload_owner()
        await self.manager.run_once(force=True)
        self.assertEqual(self.owner.state["transactions"][tx["id"]]["status"], "completed")
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(len(self.client.ledger), 2)

    async def test_concurrent_recovery_runs_post_the_missing_leg_only_once(self):
        tx = self.transaction("rob_success", ["applied", "not_sent"])
        self.commit_step(tx, 0)
        self.client.post_release = asyncio.Event()
        first = asyncio.create_task(self.manager.run_once(force=True))
        second = None
        try:
            await asyncio.wait_for(self.client.post_started.get(), timeout=2)
            second = asyncio.create_task(self.manager.run_once(force=True))
            await asyncio.sleep(0)
            self.assertEqual(len(self.client.posts), 1)
        finally:
            self.client.post_release.set()
            await asyncio.wait_for(asyncio.gather(first, *([second] if second else [])), timeout=2)
        self.assertEqual(tx["status"], "completed")
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(len(self.client.ledger), 2)

    async def test_batch_limit_backoff_and_disabled_mode_prevent_unbounded_recovery(self):
        transactions = [self.transaction() for _ in range(6)]
        self.owner.cfg["recovery_enabled"] = False
        await self.manager.run_once(force=True)
        self.assertEqual(self.client.lookups, [])
        self.assertEqual(self.client.posts, [])
        self.owner.cfg["recovery_enabled"] = True
        await self.manager.run_once(force=True)
        self.assertEqual(len(self.client.lookups), 5)
        for tx in transactions[:5]:
            delay = tx["recovery"]["next_check_at"] - self.now
            self.assertGreaterEqual(delay, 30)
            self.assertLessEqual(delay, 900)
        await self.manager.run_once()
        self.assertEqual(len(self.client.lookups), 6)
        await self.manager.run_once()
        self.assertEqual(len(self.client.lookups), 6)
        self.assertEqual(self.client.posts, [])

    async def test_initialize_creates_one_worker_and_terminate_cancels_wait_safely(self):
        asleep = asyncio.Queue()

        async def controlled_sleep(seconds):
            asleep.put_nowait(seconds)
            await asyncio.get_running_loop().create_future()

        with patch.object(self.manager, "run_once", new=AsyncMock(return_value={})) as run, patch.object(
            recovery.asyncio, "sleep", new=controlled_sleep,
        ):
            await self.manager.initialize()
            first = self.manager._worker
            await self.manager.initialize()
            self.assertIs(first, self.manager._worker)
            delay = await asyncio.wait_for(asleep.get(), timeout=2)
            self.assertGreaterEqual(delay, 30)
            self.assertEqual(run.await_count, 1)
            await self.manager.terminate()
            self.assertTrue(first.done())
            self.assertTrue(self.manager._worker is None or self.manager._worker.done())


if __name__ == "__main__":
    unittest.main()
