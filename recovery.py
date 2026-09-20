"""Recover journaled balance operations from durable, positive evidence.

An unknown write is only observed through balance history; it is never replayed.
This module does not read database credentials or create a privileged service.
"""
import asyncio
import hashlib
import json
import logging
import math
import os
import re
import tempfile
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


logger = logging.getLogger(__name__)
MILLI = Decimal("0.001")
_TXID = re.compile(r"[0-9a-f]{32}\Z")
_QQ = re.compile(r"[1-9][0-9]{4,11}\Z")
_KEY = re.compile(r"[0-9a-f]{64}\Z")
_STATES = {"not_sent", "inflight", "applied", "not_applied", "unknown"}
_NOT_APPLIED_PROOFS = {
    "request_rejected", "http_rejected", "connector_not_sent",
    "balance_negative_rejected", "request_not_applied", "auth_not_sent", "validation_rejected",
}
_RESULTS = {
    "checking", "completed", "not_sent", "not_applied", "waiting_evidence",
    "completing", "refunding", "refunded", "invalid_transaction",
    "conflicting_evidence", "waiting_retry",
}
_ERRORS = {
    "history_read_failed", "history_invalid", "prefix_inconsistent",
    "invalid_transaction", "request_unknown", "request_not_applied",
    "response_invalid", "storage_failed", "connector_not_sent",
    "balance_negative", "balance_negative_rejected", "auth_rejected",
    "validation_rejected", "http_rejected", "connection_not_sent",
    "invalid_request", "not_applied", "request_rejected", "auth_not_sent",
}


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _uid(value):
    return type(value) is int and value > 0


def _amount(value):
    amount = Decimal(str(value))
    if not amount.is_finite() or amount <= 0 or amount != amount.quantize(MILLI):
        raise ValueError("invalid amount")
    return amount


def operation_notes(tx, index):
    """Keep the original request notes byte-for-byte; index is zero-based."""
    return f"QQ互动 tx={tx['id']} step={index + 1} kind={tx['kind']} qq={tx['qq']}"


def operation_key(uid, amount, op, notes):
    value = _amount(amount)
    return hashlib.sha256(json.dumps(
        [uid, op, f"{value:.3f}", notes], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def _refund_notes(tx):
    return f"QQ互动 tx={tx['id']} recovery=refund step=1"


def _prefix(outcomes):
    count = 0
    for outcome in outcomes:
        if outcome["status"] != "applied":
            break
        count += 1
    return count


def _valid_outcome(outcome):
    if not isinstance(outcome, dict) or outcome.get("status") not in _STATES:
        return False
    if outcome["status"] == "not_applied":
        category = outcome.get("category")
        if not isinstance(category, str) or category not in _NOT_APPLIED_PROOFS:
            return False
        code = outcome.get("code")
        if code is not None and (type(code) is not int or not 100 <= code <= 599):
            return False
        if category in ("connector_not_sent", "validation_rejected") and code is not None:
            return False
        if category == "balance_negative_rejected" and code != 500:
            return False
        if category == "http_rejected" and code not in (400, 401, 403, 405, 413, 415, 422, 429):
            return False
        if category in ("request_rejected", "request_not_applied") and code not in (None, 400, 403, 422, 429):
            return False
    if "evidence" in outcome and not isinstance(outcome["evidence"], dict):
        return False
    return True


def valid_transaction(tx) -> bool:
    """Validate money-moving intent before any recovery action is considered."""
    try:
        if not isinstance(tx, dict) or not _TXID.fullmatch(tx.get("id", "")):
            return False
        kind = tx.get("kind")
        if kind not in ("checkin", "rob_success", "rob_failure"):
            return False
        if not _QQ.fullmatch(tx.get("qq", "")):
            return False
        if tx.get("status") not in ("pending", "completed", "failed", "resolved"):
            return False
        if not _number(tx.get("created_at")) or tx["created_at"] > time.time() + 300:
            return False
        amount = _amount(tx["amount"])
        steps = tx.get("steps")
        if not isinstance(steps, list) or len(steps) != (1 if kind == "checkin" else 2):
            return False
        if not all(isinstance(step, dict) and _uid(step.get("uid")) for step in steps):
            return False
        expected_ops = ["add"] if kind == "checkin" else ["subtract", "add"]
        if [step.get("operation") for step in steps] != expected_ops:
            return False
        uids = tx.get("uids")
        if (not isinstance(uids, list) or not all(_uid(uid) for uid in uids)
                or len(set(uids)) != len(uids) or set(uids) != {step["uid"] for step in steps}):
            return False
        if kind == "checkin":
            day = tx.get("date")
            if not isinstance(day, str) or datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d") != day:
                return False
            if not Decimal("0.100") <= amount <= Decimal("0.500"):
                return False
        else:
            if len(uids) != 2 or not _QQ.fullmatch(tx.get("target_qq", "")) or tx["target_qq"] == tx["qq"]:
                return False
            if kind == "rob_failure" and amount != Decimal("0.500"):
                return False
            if kind == "rob_success" and not MILLI <= amount <= Decimal("0.500"):
                return False
        completed = tx.get("completed_steps")
        if type(completed) is not int or not 0 <= completed <= len(steps):
            return False
        phase = tx.get("phase")
        if phase == "intent":
            if completed != 0:
                return False
        elif phase == "committed":
            if completed != len(steps) or tx["status"] != "completed":
                return False
        elif isinstance(phase, str):
            match = re.fullmatch(r"step_([12])_(intent|unknown|rejected|applied)", phase)
            if not match:
                return False
            number = int(match[1])
            if number > len(steps) or completed != number - (match[2] != "applied"):
                return False
        else:
            return False
        if tx["status"] == "completed" and completed != len(steps):
            return False
        if tx["status"] == "failed" and completed != 0:
            return False
        if tx["status"] == "resolved" and not isinstance(tx.get("resolution_note"), str):
            return False
        if tx["status"] == "resolved" and not tx["resolution_note"].strip():
            return False
        if ("request_keys" in tx) != ("step_outcomes" in tx):
            return False
        if "request_keys" in tx:
            keys, outcomes = tx["request_keys"], tx["step_outcomes"]
            if not isinstance(keys, list) or not isinstance(outcomes, list):
                return False
            if len(keys) != len(steps) or len(outcomes) != len(steps):
                return False
            for index, (step, key, outcome) in enumerate(zip(steps, keys, outcomes)):
                if not isinstance(key, str) or not _KEY.fullmatch(key) or not _valid_outcome(outcome):
                    return False
                if key != operation_key(step["uid"], amount, step["operation"], operation_notes(tx, index)):
                    return False
            if _prefix(outcomes) != completed:
                return False
            if any(outcome["status"] == "applied" for outcome in outcomes[completed:]):
                return False
            if completed == len(steps):
                expected_phase = "committed" if tx["status"] == "completed" else f"step_{completed}_applied"
            else:
                next_status = outcomes[completed]["status"]
                if next_status == "not_sent":
                    expected_phase = "intent" if completed == 0 else f"step_{completed}_applied"
                else:
                    suffix = {"inflight": "intent", "not_applied": "rejected", "unknown": "unknown"}[next_status]
                    expected_phase = f"step_{completed + 1}_{suffix}"
            if phase != expected_phase:
                return False
            if tx["status"] == "failed" and any(outcome["status"] not in ("not_sent", "not_applied") for outcome in outcomes):
                return False
        recovery = tx.get("recovery", {})
        if not isinstance(recovery, dict):
            return False
        if "result" in recovery and not isinstance(recovery["result"], str):
            return False
        if recovery.get("last_error") is not None and not isinstance(recovery["last_error"], str):
            return False
        for field in ("checked_at", "next_check_at"):
            if field in recovery and not _number(recovery[field]):
                return False
        if "attempts" in recovery and (type(recovery["attempts"]) is not int or recovery["attempts"] < 0):
            return False
        decision = recovery.get("decision")
        if decision is not None:
            if decision not in ("complete", "refund") or kind == "checkin" or completed < 1:
                return False
        refund = recovery.get("refund")
        if refund is not None:
            if decision != "refund" or not _valid_outcome(refund) or not _number(refund.get("created_at")):
                return False
            if refund["created_at"] < tx["created_at"] or refund["created_at"] > time.time() + 300:
                return False
            notes = _refund_notes(tx)
            if refund.get("notes") != notes or refund.get("key") != operation_key(steps[0]["uid"], amount, "add", notes):
                return False
        if decision == "refund":
            if completed != 1 or "step_outcomes" not in tx or tx["step_outcomes"][1]["status"] != "not_applied":
                return False
        return True
    except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation, OverflowError):
        return False


def _safe_category(exc):
    category = getattr(exc, "category", None)
    if getattr(exc, "known_not_applied", False) is True:
        return category if isinstance(category, str) and category in _NOT_APPLIED_PROOFS else "request_not_applied"
    if isinstance(category, str) and category in _ERRORS:
        return category
    return "request_unknown"


class LedgerRecovery:
    def __init__(self, owner):
        self.owner = owner
        self._worker = None
        self._run_lock = asyncio.Lock()
        self.status_path = Path(owner.state_path).with_name("recovery_status.json")

    def _interval(self):
        value = self.owner.cfg.get("recovery_interval_seconds", 30)
        return min(900, max(1, value)) if type(value) is int else 30

    def _enabled(self):
        return self.owner.cfg.get("recovery_enabled", True) is True

    async def initialize(self):
        if self._enabled() and (self._worker is None or self._worker.done()):
            self._worker = asyncio.create_task(self._loop(), name="sub2api-ledger-recovery")

    async def terminate(self):
        task, self._worker = self._worker, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _loop(self):
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("ledger_recovery category=worker_failed")
            await asyncio.sleep(self._interval())

    async def run_once(self, force=False):
        """Process at most five due transactions and return the privacy-safe status."""
        async with self._run_lock:
            error = None
            snapshot = None
            try:
                if self._enabled():
                    async with self.owner._lock:
                        if self.owner._state_failed:
                            # The in-memory state may already contain a successful write.
                            # Never replace it with the older on-disk state.
                            self.owner._save()
                            self.owner._state_failed = False
                        candidates = []
                        now = time.time()
                        for txid, tx in self.owner.state["transactions"].items():
                            if not isinstance(tx, dict) or tx.get("status") != "pending":
                                continue
                            recovery = tx.get("recovery", {})
                            next_check = recovery.get("next_check_at", 0) if isinstance(recovery, dict) else 0
                            if force or not _number(next_check) or next_check <= now:
                                candidates.append(txid)
                            if len(candidates) >= 5:
                                break
                    for txid in candidates:
                        async with self.owner._lock:
                            if self.owner._state_failed:
                                raise RuntimeError("ledger storage is unavailable")
                            tx = self.owner.state["transactions"].get(txid)
                            if isinstance(tx, dict) and tx.get("status") == "pending":
                                await self._recover(tx)
            except BaseException as exc:
                error = exc
            try:
                async with self.owner._lock:
                    snapshot = self._snapshot()
                self._write_status(snapshot)
            except Exception:
                logger.warning("ledger_recovery category=status_write_failed")
                if error is None:
                    raise
            if error is not None:
                raise error
            return snapshot

    def _prepare(self, tx):
        recovery = tx.setdefault("recovery", {})
        recovery["attempts"] = recovery.get("attempts", 0) + 1
        recovery["checked_at"] = time.time()
        delay = min(900, self._interval() * 2 ** min(recovery["attempts"] - 1, 10))
        recovery["next_check_at"] = recovery["checked_at"] + delay
        recovery["result"] = "checking"
        recovery["last_error"] = None
        if "step_outcomes" not in tx:
            recovery["original_phase"] = tx["phase"]
            # Every leg saves its own sending intent first. A phase referring to
            # leg N therefore proves that later legs were never sent, even if
            # leg N itself is still unknown (including legacy "rejected").
            last_attempted = 0 if tx["phase"] == "intent" else int(tx["phase"].split("_")[1])
            tx["request_keys"] = [operation_key(
                step["uid"], tx["amount"], step["operation"], operation_notes(tx, index)
            ) for index, step in enumerate(tx["steps"])]
            tx["step_outcomes"] = [
                {"status": "applied" if index < tx["completed_steps"] else
                 ("not_sent" if index >= last_attempted else "unknown")}
                for index in range(len(tx["steps"]))
            ]
            self._sync(tx)
        self.owner._save()
        return recovery

    @staticmethod
    def _sync(tx):
        outcomes = tx["step_outcomes"]
        completed = _prefix(outcomes)
        tx["completed_steps"] = completed
        if completed == len(outcomes):
            tx["phase"] = f"step_{completed}_applied"
        else:
            status = outcomes[completed]["status"]
            if status == "not_sent":
                tx["phase"] = "intent" if completed == 0 else f"step_{completed}_applied"
            else:
                suffix = {"inflight": "intent", "not_applied": "rejected", "unknown": "unknown"}[status]
                tx["phase"] = f"step_{completed + 1}_{suffix}"

    async def _recover(self, tx):
        if not valid_transaction(tx):
            # Do not rewrite malformed financial intent just to make it executable.
            logger.warning("ledger_recovery tx=%s category=invalid_transaction", self._status_id(tx))
            return
        recovery = self._prepare(tx)
        if recovery.get("decision") == "refund":
            await self._refund(tx)
            return
        for index, (step, outcome) in enumerate(zip(tx["steps"], tx["step_outcomes"])):
            if outcome["status"] not in ("inflight", "unknown"):
                continue
            found = await self._lookup(tx, step["uid"], step["operation"], operation_notes(tx, index), tx["created_at"])
            if found is False:
                return
            if found is not None:
                if index != tx["completed_steps"]:
                    recovery.update(result="conflicting_evidence", last_error="prefix_inconsistent")
                    self.owner._save()
                    return
                outcome.update(status="applied", evidence=found)
                outcome.pop("category", None)
            else:
                outcome["status"] = "unknown"
            self._sync(tx)
            self.owner._save()
        if tx["completed_steps"] == len(tx["steps"]):
            self._complete(tx)
            return
        outcomes = tx["step_outcomes"]
        if tx["completed_steps"] == 0 and all(o["status"] in ("not_sent", "not_applied") for o in outcomes):
            tx["status"] = "failed"
            recovery["result"] = "not_sent" if all(o["status"] == "not_sent" for o in outcomes) else "not_applied"
            self._clear_old_cooldown(tx)
            self.owner._save()
            return
        if tx["kind"] == "checkin" or tx["completed_steps"] != 1:
            recovery["result"] = "waiting_evidence"
            self.owner._save()
            return
        second = outcomes[1]
        decision = recovery.get("decision")
        if decision is None:
            if second["status"] == "not_sent":
                recovery["decision"] = "complete"
            elif second["status"] == "not_applied":
                recovery["decision"] = "refund"
            else:
                recovery["result"] = "waiting_evidence"
                self.owner._save()
                return
            self.owner._save()
        if recovery["decision"] == "refund":
            await self._refund(tx)
        elif second["status"] in ("not_sent", "not_applied"):
            recovery["result"] = "completing"
            step = tx["steps"][1]
            await self._send(tx, second, step["uid"], step["operation"], operation_notes(tx, 1), original=True)
            if second["status"] == "applied":
                self._complete(tx)
            else:
                recovery["result"] = "waiting_retry" if second["status"] == "not_applied" else "waiting_evidence"
                self.owner._save()
        else:
            recovery["result"] = "waiting_evidence"
            self.owner._save()

    async def _lookup(self, tx, uid, operation, notes, created_at):
        try:
            found = await self.owner.client.lookup_balance_operation(
                uid, Decimal(tx["amount"]), operation, notes, created_at
            )
            if found is not None and (not isinstance(found, dict) or not found):
                raise ValueError("invalid history evidence")
            if found is None:
                return None
            if not _uid(found.get("id")) or "value" not in found or not Decimal(str(found["value"])).is_finite():
                raise ValueError("invalid history evidence")
            # Matching the full notes and account is the client's responsibility.
            # Persist only the audit reference and values needed for later review.
            evidence = {"source": "balance_history", "id": found["id"], "value": str(found["value"])}
            for field in ("used_at", "created_at"):
                if isinstance(found.get(field), str):
                    evidence[field] = found[field]
            return evidence
        except asyncio.CancelledError:
            raise
        except Exception:
            tx["recovery"].update(result="waiting_evidence", last_error="history_read_failed")
            self.owner._save()
            logger.warning("ledger_recovery tx=%s category=history_read_failed", tx["id"])
            return False

    async def _send(self, tx, outcome, uid, operation, notes, original=False):
        previous_outcome = dict(outcome)
        previous_phase, previous_completed = tx["phase"], tx["completed_steps"]
        outcome["status"] = "inflight"
        outcome.pop("category", None)
        outcome.pop("code", None)
        if original:
            self._sync(tx)
        try:
            self.owner._save()
        except BaseException:
            # No request was made. Retain this in-memory fact while storage is
            # unavailable; the next pass must save it before attempting a write.
            # If the process dies here, an on-disk inflight marker stays unknown.
            outcome.clear()
            outcome.update(previous_outcome)
            if original:
                tx["phase"], tx["completed_steps"] = previous_phase, previous_completed
            raise
        try:
            response = await self.owner.client.balance_op(uid, Decimal(tx["amount"]), operation, notes)
            if (not isinstance(response, dict) or not _uid(response.get("id")) or response["id"] != uid
                    or "balance" not in response or not Decimal(str(response["balance"])).is_finite()):
                raise ValueError("invalid balance response")
        except asyncio.CancelledError:
            # The already durable inflight marker is intentionally retained.
            raise
        except Exception as exc:
            known = getattr(exc, "known_not_applied", False) is True
            category, code = _safe_category(exc), getattr(exc, "code", None)
            if known and not _valid_outcome({"status": "not_applied", "category": category, "code": code}):
                known, category = False, "request_unknown"
            outcome["status"] = "not_applied" if known else "unknown"
            outcome["category"] = category
            outcome["code"] = code if type(code) is int and 100 <= code <= 599 else None
            tx["recovery"]["last_error"] = outcome["category"]
            logger.warning("ledger_recovery tx=%s category=%s", tx["id"], outcome["category"])
        else:
            outcome.update(status="applied", evidence={"source": "balance_response", "uid": uid})
            tx["recovery"]["last_error"] = None
        if original:
            self._sync(tx)
        self.owner._save()

    async def _refund(self, tx):
        recovery = tx["recovery"]
        refund = recovery.get("refund")
        uid = tx["steps"][0]["uid"]
        notes = _refund_notes(tx)
        if refund is None:
            refund = {
                "status": "not_sent", "notes": notes,
                "key": operation_key(uid, tx["amount"], "add", notes),
                "created_at": time.time(),
            }
            recovery["refund"] = refund
            self.owner._save()
        if refund["status"] in ("inflight", "unknown"):
            found = await self._lookup(tx, uid, "add", notes, refund["created_at"])
            if found is False:
                return
            if found is None:
                refund["status"] = "unknown"
                recovery["result"] = "waiting_evidence"
                self.owner._save()
                return
            refund.update(status="applied", evidence=found)
            refund.pop("category", None)
            self.owner._save()
        elif refund["status"] in ("not_sent", "not_applied"):
            recovery["result"] = "refunding"
            await self._send(tx, refund, uid, "add", notes)
        if refund["status"] == "applied":
            tx["status"] = "resolved"
            tx["resolution_note"] = "自动恢复：原扣款成功，收款步骤已确认未执行；已按原金额退回扣款账号，退款证据保存在 recovery.refund。"
            recovery.update(result="refunded", last_error=None)
            self.owner._save()
        else:
            recovery["result"] = "waiting_retry" if refund["status"] == "not_applied" else "waiting_evidence"
            self.owner._save()

    def _complete(self, tx):
        if tx["kind"] == "checkin":
            day, qq, uid = tx["date"], tx["qq"], str(tx["steps"][0]["uid"])
            self.owner.state["checkin"][qq] = max(self.owner.state["checkin"].get(qq, ""), day)
            self.owner.state["checkin_uid"][uid] = max(self.owner.state["checkin_uid"].get(uid, ""), day)
        tx["recovery"].update(result="completed", last_error=None)
        self.owner._complete_tx(tx)

    def _clear_old_cooldown(self, tx):
        if not tx["kind"].startswith("rob_"):
            return
        stamp = self.owner.state["robbery_ts"].get(tx["qq"])
        if _number(stamp) and stamp <= tx["created_at"]:
            self.owner.state["robbery_ts"].pop(tx["qq"], None)

    @staticmethod
    def _status_id(tx):
        value = tx.get("id")
        return value if isinstance(value, str) and _TXID.fullmatch(value) else "invalid"

    def _snapshot(self):
        now = time.time()
        items = []
        recovered = 0
        for tx in self.owner.state["transactions"].values():
            if not isinstance(tx, dict):
                continue
            recovery = tx.get("recovery", {})
            if not isinstance(recovery, dict):
                recovery = {}
            result = recovery.get("result")
            if tx.get("status") in ("completed", "failed", "resolved") and result in ("completed", "not_sent", "not_applied", "refunded"):
                recovered += 1
            if tx.get("status") != "pending":
                continue
            valid = valid_transaction(tx)
            created = tx.get("created_at")
            phase = tx.get("phase") if valid else "invalid"
            error = recovery.get("last_error")
            next_check = recovery.get("next_check_at")
            items.append({
                "id": self._status_id(tx),
                "kind": tx.get("kind") if tx.get("kind") in ("checkin", "rob_success", "rob_failure") else "invalid",
                "phase": phase,
                "age_seconds": max(0, int(now - created)) if _number(created) else 0,
                "result": (result if result in _RESULTS else "waiting_evidence") if valid else "invalid_transaction",
                "last_error": (error if error in _ERRORS else None) if valid else "invalid_transaction",
                "next_check_at": next_check if _number(next_check) else None,
            })
        return {
            "checked_at": now, "pending_count": len(items), "recovered_count": recovered,
            "storage_failed": bool(self.owner._state_failed),
            "worker_enabled": self._enabled(), "transactions": items,
        }

    def _write_status(self, snapshot):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.status_path.parent,
                prefix=".recovery-status-", suffix=".tmp", delete=False,
            ) as stream:
                temporary = stream.name
                json.dump(snapshot, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.status_path)
            if os.name != "nt":
                descriptor = os.open(self.status_path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
