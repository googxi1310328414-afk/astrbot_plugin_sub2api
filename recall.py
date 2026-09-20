"""Send only temporary quota details and recall their own OneBot receipts."""

import asyncio
import json
import logging
import math
import os
from pathlib import Path
import re
import tempfile
import time


logger = logging.getLogger(__name__)
ERRORS = {"identity_mismatch", "platform_unavailable", "api_failed", "state_write_failed"}


def _message_id(value):
    if type(value) is int:
        number = value
    elif isinstance(value, str) and re.fullmatch(r"-?\d{1,20}", value):
        number = int(value)
    else:
        raise ValueError("Invalid receipt")
    if not -(2**63) <= number < 2**63:
        raise ValueError("Invalid receipt")
    return number


def _qq(value):
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r"[1-9]\d{0,19}", value):
        raise ValueError("Invalid QQ identifier")
    return value


def _key(record):
    return json.dumps([record["platform_id"], record["self_id"], record["message_id"]])


class QuotaRecallManager:
    MAX_ATTEMPTS = 3
    RETRY_DELAY = 2
    API_TIMEOUT = 5
    _sleep = staticmethod(asyncio.sleep)
    _now = staticmethod(time.time)

    def __init__(self, context, path, delay_seconds=5):
        if (isinstance(delay_seconds, bool) or not isinstance(delay_seconds, (int, float))
                or not math.isfinite(delay_seconds) or delay_seconds < 0):
            raise ValueError("Recall delay must be a finite nonnegative number")
        self.context, self.path = context, Path(path)
        self.delay_seconds = delay_seconds
        self.pending = {}
        self.last_error = None
        self._valid_state = True
        self._closed = False
        self._lock = asyncio.Lock()
        self._workers = {}
        self._bots = {}
        self._load()

    def _load(self):
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if state["version"] != 1 or not isinstance(state["pending"], list):
                raise ValueError
            records = {}
            for item in state["pending"]:
                platform_id = item["platform_id"]
                if (not isinstance(platform_id, str) or not 1 <= len(platform_id) <= 200
                        or any(ord(char) < 32 for char in platform_id)
                        or type(item["due"]) not in (int, float) or not math.isfinite(item["due"])
                        or type(item["attempts"]) is not int or item["attempts"] < 0
                        or item.get("last_error") not in ERRORS | {None}):
                    raise ValueError
                record = {"platform_id": platform_id, "self_id": _qq(item["self_id"]),
                          "message_id": _message_id(item["message_id"]), "due": item["due"],
                          "attempts": item["attempts"], "last_error": item.get("last_error")}
                if _key(record) in records:
                    raise ValueError
                records[_key(record)] = record
            self.pending = records
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError):
            self._valid_state = False
            self.last_error = "state_read_failed"
            logger.warning("quota_recall: state_read_failed; temporary details disabled")

    def _save(self):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".quota-recall-", delete=False) as stream:
                temporary = stream.name
                json.dump({"version": 1, "pending": list(self.pending.values())}, stream,
                          ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            if os.name != "nt":
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            return True
        except (OSError, ValueError, TypeError):
            self.last_error = "state_write_failed"
            logger.warning("quota_recall: state_write_failed")
            return False
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    async def initialize(self):
        if self._valid_state and not self._closed:
            for key in list(self.pending):
                self._schedule(key)

    async def send(self, event, text):
        """True means a receipt was safely scheduled/handled, not a recall guarantee.

        False must never cause the caller to resend the quota text: the send may
        have reached QQ even if its response was missing or malformed.
        """
        if self._closed or not self._valid_state or not isinstance(text, str) or not text:
            return False
        try:
            if event.get_platform_name() != "aiocqhttp":
                return False
            bot = getattr(event, "bot", None)
            if not callable(getattr(bot, "call_action", None)):
                return False
            platform_id = event.platform_meta.id
            if (not isinstance(platform_id, str) or not 1 <= len(platform_id) <= 200
                    or any(ord(char) < 32 for char in platform_id)):
                return False
            self_id, sender = _qq(event.get_self_id()), _qq(event.get_sender_id())
            group = event.get_group_id()
            message = [{"type": "text", "data": {"text": text}}]
            params = {"message": message, "self_id": int(self_id)}
            if group:
                message.insert(0, {"type": "at", "data": {"qq": sender}})
                action, params["group_id"] = "send_group_msg", int(_qq(group))
            else:
                action, params["user_id"] = "send_private_msg", int(sender)
            receipt = await asyncio.wait_for(bot.call_action(action, **params), self.API_TIMEOUT)
            if not isinstance(receipt, dict):
                raise ValueError("Missing receipt")
            message_id = _message_id(receipt["message_id"])
        except asyncio.CancelledError:
            self.last_error = "send_outcome_unknown"
            logger.warning("quota_recall: send_outcome_unknown; quota text will not be resent")
            raise
        except Exception:
            self.last_error = "send_receipt_unavailable"
            logger.warning("quota_recall: send_receipt_unavailable; quota text will not be resent")
            return False
        record = {"platform_id": platform_id, "self_id": self_id, "message_id": message_id,
                  "due": self._now() + self.delay_seconds, "attempts": 0, "last_error": None}
        key = _key(record)
        async with self._lock:
            self.pending[key] = record
            self._bots[key] = bot
            saved = self._save()
        if not saved or self._closed:
            # The temporary message is already visible. Prefer immediate recall
            # if its durable cleanup record could not be written.
            if await self._attempt(key):
                return True
            if not self._closed:
                self._schedule(key)
            return False
        self._schedule(key)
        return True

    def _schedule(self, key):
        if self._closed or key in self._workers:
            return
        task = asyncio.create_task(self._run(key))
        self._workers[key] = task

        def finished(worker):
            if self._workers.get(key) is worker:
                self._workers.pop(key, None)

        task.add_done_callback(finished)

    async def _resolve_bot(self, key, record):
        bot = self._bots.get(key)
        if bot is None:
            try:
                platform = self.context.get_platform_inst(platform_id=record["platform_id"])
                if platform is None or platform.meta().name != "aiocqhttp":
                    return None, "platform_unavailable"
                bot = getattr(platform, "bot", None)
            except Exception:
                return None, "platform_unavailable"
        if not callable(getattr(bot, "call_action", None)):
            return None, "platform_unavailable"
        try:
            info = await asyncio.wait_for(bot.call_action("get_login_info", self_id=int(record["self_id"])), self.API_TIMEOUT)
            if not isinstance(info, dict) or _qq(info.get("user_id")) != record["self_id"]:
                return None, "identity_mismatch"
        except asyncio.CancelledError:
            raise
        except Exception:
            return None, "api_failed"
        return bot, None

    async def _attempt(self, key):
        record = self.pending.get(key)
        if record is None:
            return True
        bot, error = await self._resolve_bot(key, record)
        if bot is not None:
            try:
                # CQHttp returns only data; None or {} is a successful response.
                await asyncio.wait_for(bot.call_action("delete_msg", message_id=record["message_id"],
                                                      self_id=int(record["self_id"])), self.API_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except Exception:
                error = "api_failed"
        async with self._lock:
            if error is None:
                self.pending.pop(key, None)
                self._bots.pop(key, None)
                self._save()
                return True
            record["attempts"] += 1
            record["last_error"] = error
            record["due"] = self._now() + self.RETRY_DELAY
            self.last_error = error
            self._save()
            logger.warning("quota_recall: %s; receipt retained for recovery", error)
            return False

    async def _run(self, key):
        try:
            for _ in range(self.MAX_ATTEMPTS):
                record = self.pending.get(key)
                if record is None or self._closed:
                    return
                delay = max(0, record["due"] - self._now())
                if delay:
                    await self._sleep(delay)
                if await self._attempt(key):
                    return
                if self.pending.get(key, {}).get("last_error") == "identity_mismatch":
                    return
        except asyncio.CancelledError:
            raise

    async def terminate(self):
        self._closed = True
        tasks = list(self._workers.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()
        # Bounded API attempts; do not wait the remaining five seconds at unload.
        await asyncio.gather(*(self._attempt(key) for key in list(self.pending)))
