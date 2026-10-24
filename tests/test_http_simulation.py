"""Exercise the real HTTP client against an in-process loopback API.

Run from the project root with ``python -m unittest tests.test_http_simulation -v``.
The fake credentials below belong only to this test server. No environment file,
AstrBot instance, external network endpoint, or production balance is accessed.
"""

import asyncio
import importlib.util
import socket
import sys
import time
import types
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from aiohttp import web


def _load_client_module():
    """Import the actual plugin without installing or booting AstrBot."""
    modules = {
        name: types.ModuleType(name)
        for name in (
            "astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star",
            "astrbot.api.message_components",
        )
    }

    def decorator(*args, **kwargs):
        return lambda function: function

    class Star:
        def __init__(self, context):
            self.context = context

    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].filter = types.SimpleNamespace(
        command=decorator,
        custom_filter=decorator,
        CustomFilter=object,
        on_platform_loaded=decorator,
        llm_tool=decorator,
        permission_type=decorator,
        PermissionType=types.SimpleNamespace(ADMIN="admin"),
    )
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].Star = Star
    modules["astrbot.api.star"].register = decorator
    modules["astrbot.api.message_components"].At = type("At", (), {})
    source = Path(__file__).resolve().parents[1] / "main.py"
    package_name = "_sub2api_http_simulation"
    package = types.ModuleType(package_name)
    package.__path__ = [str(source.parent)]
    spec = importlib.util.spec_from_file_location(package_name + ".main", source)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {**modules, package_name: package, spec.name: module}):
        spec.loader.exec_module(module)
    return module


plugin = _load_client_module()
SECRET = "SIMULATION_ONLY_UPSTREAM_PRIVATE_DIAGNOSTIC"
EMAIL = "simulation-admin@example.test"
PASSWORD = "simulation-only-password"


class HttpSimulationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.login_count = 0
        self.mutations = []
        self.idempotency = {}
        self.plans = {name: [] for name in ("login", "search", "user", "balance", "history")}
        self.history = []
        self.history_pages = {}
        self.transaction_time = float(int(time.time()) - 60)
        self.operation_notes = "simulation-transaction-1:debit"
        self.users = {
            7: {"id": 7, "email": "alice@example.test", "balance": 5.0,
                "status": "active", "role": "user"},
            8: {"id": 8, "email": "bob@example.test", "balance": 2.0,
                "status": "active", "role": "user"},
        }
        app = web.Application()
        app.router.add_post("/api/v1/auth/login", self._login)
        app.router.add_get("/api/v1/admin/users", self._search)
        app.router.add_get("/api/v1/admin/users/{uid}", self._user)
        app.router.add_get("/api/v1/admin/users/{uid}/balance-history", self._history)
        app.router.add_post("/api/v1/admin/users/{uid}/balance", self._balance)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            self.site = web.SockSite(self.runner, listener)
            await self.site.start()
        except BaseException:
            listener.close()
            await self.runner.cleanup()
            raise
        self.client = plugin.Sub2ApiClient(
            f"http://127.0.0.1:{port}/api/v1", EMAIL, PASSWORD,
        )

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def _record(self, request):
        item = {
            "method": request.method,
            "path": request.path,
            "raw_path": request.raw_path,
            "query": dict(request.query),
            "authorization": request.headers.get("Authorization"),
            "idempotency_key": request.headers.get("Idempotency-Key"),
            "body": await request.json() if request.can_read_body else None,
        }
        self.requests.append(item)
        return item

    def _take_plan(self, name):
        return self.plans[name].pop(0) if self.plans[name] else None

    def _failure_response(self, plan):
        if isinstance(plan, dict) and "http_status" in plan:
            if "payload" in plan:
                return web.json_response(plan["payload"], status=plan["http_status"])
            return web.Response(
                text=plan.get("text", SECRET), status=plan["http_status"],
                content_type=plan.get("content_type", "text/plain"),
            )
        if isinstance(plan, int):
            return web.json_response({"message": SECRET, "password": PASSWORD}, status=plan)
        if plan == "non_json":
            return web.Response(text=SECRET, content_type="text/plain")
        if plan == "broken_json":
            return web.Response(text='{"private":"' + SECRET, content_type="application/json")
        if plan == "missing_data":
            return web.json_response({"data": {"private": SECRET}})
        return None

    def _authenticated(self, request):
        return request.headers.get("Authorization") == f"Bearer simulation-token-{self.login_count}"

    async def _login(self, request):
        item = await self._record(request)
        self.login_count += 1
        planned = self._failure_response(self._take_plan("login"))
        if planned is not None:
            return planned
        if item["body"] != {"email": EMAIL, "password": PASSWORD}:
            return web.json_response({"message": "Incorrect simulation credentials"}, status=401)
        return web.json_response({"data": {
            "access_token": f"simulation-token-{self.login_count}", "expires_in": 3600,
        }})

    async def _search(self, request):
        await self._record(request)
        planned = self._failure_response(self._take_plan("search"))
        if planned is not None:
            return planned
        if not self._authenticated(request):
            return web.json_response({"message": SECRET}, status=401)
        email = request.query.get("search", "").lower()
        items = [dict(user) for user in self.users.values() if email in user["email"].lower()]
        return web.json_response({"data": {"items": items, "total": len(items)}})

    async def _user(self, request):
        await self._record(request)
        plan = self._take_plan("user")
        if plan == "drop":
            # 传输层中断：连接在响应写回前被服务端关闭。注意 aiohttp 对幂等 GET 的
            # ServerDisconnectedError 会在单次 session.request 内部自动重发一次。
            request.transport.close()
            return web.Response()
        if plan == "partial":
            # 响应头 200 后传输中途断开：客户端读体时抛 ClientPayloadError
            # （非超时传输层失败，aiohttp 不会对其内部重试）。
            response = web.StreamResponse(headers={"Content-Type": "application/json"})
            await response.prepare(request)
            await response.write(b'{"data": {"id": ')
            request.transport.close()
            return response
        if plan == "sleep":
            # 模拟上游账号查询被 IO 争用拖慢（超时类传输层失败）。
            await asyncio.sleep(1.2)
        planned = self._failure_response(plan)
        if planned is not None:
            return planned
        if not self._authenticated(request):
            return web.json_response({"message": SECRET}, status=401)
        user = self.users.get(int(request.match_info["uid"]))
        if user is None:
            return web.json_response({"message": SECRET}, status=404)
        return web.json_response({"data": dict(user)})

    async def _balance(self, request):
        item = await self._record(request)
        key = item["idempotency_key"]
        if not key:
            return web.json_response({"message": "Idempotency-Key is required"}, status=400)
        plan = self._take_plan("balance")
        if plan == "sleep":
            # 模拟写请求超时：客户端断开后服务端仍在处理。
            await asyncio.sleep(1.2)
        if isinstance(plan, dict):
            return self._failure_response(plan)
        if isinstance(plan, int) and not 200 <= plan < 300:
            return self._failure_response(plan)
        if not self._authenticated(request):
            return web.json_response({"message": SECRET}, status=401)
        user = self.users[int(request.match_info["uid"])]
        body = item["body"]
        fingerprint = (user["id"], body["balance"], body["operation"], body["notes"])
        cached = self.idempotency.get(key)
        if cached is not None:
            if cached["fingerprint"] != fingerprint:
                return web.json_response({"message": "Idempotency payload conflict"}, status=409)
            return web.json_response({"data": cached["data"]})
        delta = Decimal(str(body["balance"]))
        if body["operation"] == "subtract":
            delta = -delta
        elif body["operation"] != "add":
            return web.json_response({"message": SECRET}, status=400)
        user["balance"] = float(Decimal(str(user["balance"])) + delta)
        self.mutations.append((user["id"], body["operation"], str(body["balance"])))
        self.idempotency[key] = {"fingerprint": fingerprint, "data": dict(user)}
        if plan == "commit_then_drop":
            # The balance changed, but the client cannot know that the write committed.
            request.transport.close()
            return web.Response()
        if plan == "commit_then_404":
            return web.json_response({"message": SECRET}, status=404)
        planned = self._failure_response(plan)
        if planned is not None:
            return planned
        return web.json_response({"data": dict(user)})

    async def _history(self, request):
        await self._record(request)
        planned = self._failure_response(self._take_plan("history"))
        if planned is not None:
            return planned
        if not self._authenticated(request):
            return web.json_response({"message": SECRET}, status=401)
        page = int(request.query.get("page", 1))
        size = int(request.query.get("page_size", 100))
        data = self.history_pages.get(page)
        if data is None:
            data = {
                "items": self.history[(page - 1) * size:page * size],
                "total": len(self.history), "page": page, "page_size": size,
                "pages": max(1, (len(self.history) + size - 1) // size),
                "total_recharged": 0,
            }
        return web.json_response({"data": data})

    def _ledger_record(self, **changes):
        timestamp = datetime.fromtimestamp(self.transaction_time + 1, timezone.utc).isoformat()
        record = {
            "id": 301, "used_by": 7, "type": "admin_balance", "status": "used",
            "value": "-0.125", "notes": self.operation_notes,
            "created_at": timestamp, "used_at": timestamp,
        }
        record.update(changes)
        return record

    async def _lookup(self, *, amount=0.125, op="subtract"):
        return await self.client.lookup_balance_operation(
            7, amount, op, self.operation_notes, self.transaction_time,
        )

    def _calls(self, suffix):
        return [item for item in self.requests if item["path"].endswith(suffix)]

    def _assert_sanitized(self, error):
        for private in (SECRET, PASSWORD, EMAIL, "simulation-token-"):
            self.assertNotIn(private, str(error))

    async def test_login_search_query_and_transfer_use_real_http_in_order(self):
        found = await self.client.find_user_by_email("ALICE@example.test")
        self.assertEqual(found["id"], 7)
        self.assertEqual((await self.client.get_user(7))["balance"], 5.0)
        debit = await self.client.balance_op(7, 0.125, "subtract", "simulation debit")
        credit = await self.client.balance_op(8, 0.125, "add", "simulation credit")
        self.assertEqual(debit["balance"], 4.875)
        self.assertEqual(credit["balance"], 2.125)
        self.assertEqual(sum(user["balance"] for user in self.users.values()), 7.0)
        self.assertEqual(self.login_count, 1)
        self.assertEqual([(r["method"], r["path"]) for r in self.requests], [
            ("POST", "/api/v1/auth/login"),
            ("GET", "/api/v1/admin/users"),
            ("GET", "/api/v1/admin/users/7"),
            ("POST", "/api/v1/admin/users/7/balance"),
            ("POST", "/api/v1/admin/users/8/balance"),
        ])
        for request in self.requests[1:]:
            self.assertEqual(request["authorization"], "Bearer simulation-token-1")
        self.assertEqual(self._calls("/7/balance")[0]["body"], {
            "balance": 0.125, "operation": "subtract", "notes": "simulation debit",
        })
        keys = [r["idempotency_key"] for r in self._calls("/balance")]
        self.assertTrue(all(keys))
        self.assertEqual(len(set(keys)), 2)

    async def test_search_preserves_plus_ampersand_and_hash_in_email(self):
        email = "alice+audit&tag#case@example.test"
        self.users[7]["email"] = email
        user = await self.client.find_user_by_email(email)
        self.assertIsNotNone(user)
        self.assertEqual(user["id"], 7)
        request = self._calls("/admin/users")[0]
        self.assertEqual(set(request["query"]), {"page", "page_size", "search"})
        self.assertEqual(request["query"]["page"], "1")
        self.assertGreater(int(request["query"]["page_size"]), 0)
        self.assertEqual(request["query"]["search"], email)
        for encoded in ("%2b", "%26", "%23"):
            self.assertIn(encoded, request["raw_path"].lower())

    async def test_search_returns_none_for_unmatched_email(self):
        self.assertIsNone(await self.client.find_user_by_email("absent@example.test"))

    async def test_get_401_refreshes_once_and_recovers(self):
        self.plans["user"] = [401]
        self.assertEqual((await self.client.get_user(7))["id"], 7)
        self.assertEqual(self.login_count, 2)
        self.assertEqual(len(self._calls("/users/7")), 2)
        self.assertEqual(self._calls("/users/7")[-1]["authorization"], "Bearer simulation-token-2")

    async def test_get_persistent_401_terminates_after_one_refresh(self):
        self.plans["user"] = [401, 401]
        with self.assertRaises(plugin.ApiError) as raised:
            await self.client.get_user(7)
        self.assertEqual(raised.exception.code, 401)
        self._assert_sanitized(raised.exception)
        self.assertEqual(self.login_count, 2)
        self.assertEqual(len(self._calls("/users/7")), 2)

    async def test_balance_401_refreshes_once_without_duplicate_credit(self):
        self.plans["balance"] = [401]
        user = await self.client.balance_op(7, 0.125, "add", "simulation")
        self.assertEqual(user["balance"], 5.125)
        self.assertEqual(self.login_count, 2)
        self.assertEqual(len(self._calls("/7/balance")), 2)
        self.assertEqual(len(self.mutations), 1)
        keys = [r["idempotency_key"] for r in self._calls("/7/balance")]
        self.assertTrue(keys[0])
        self.assertEqual(keys[0], keys[1])

    async def test_explicit_replay_of_same_operation_does_not_credit_twice(self):
        first = await self.client.balance_op(7, 0.125, "add", "transaction-1:credit")
        second = await self.client.balance_op(7, 0.125, "add", "transaction-1:credit")
        self.assertEqual(first, second)
        self.assertEqual(second["balance"], 5.125)
        self.assertEqual(self.users[7]["balance"], 5.125)
        self.assertEqual(len(self._calls("/7/balance")), 2)
        self.assertEqual(len(self.mutations), 1)
        keys = [r["idempotency_key"] for r in self._calls("/7/balance")]
        self.assertTrue(keys[0])
        self.assertEqual(keys[0], keys[1])

    async def test_idempotency_keys_distinguish_user_amount_operation_and_notes(self):
        operations = (
            (7, 0.125, "add", "transaction-1:leg"),
            (8, 0.125, "add", "transaction-1:leg"),
            (7, 0.250, "add", "transaction-1:leg"),
            (7, 0.125, "subtract", "transaction-1:leg"),
            (7, 0.125, "add", "transaction-2:leg"),
        )
        for arguments in operations:
            await self.client.balance_op(*arguments)
        keys = [r["idempotency_key"] for r in self._calls("/balance")]
        self.assertTrue(all(keys))
        self.assertEqual(len(set(keys)), len(operations))
        self.assertEqual(len(self.mutations), len(operations))

    async def test_balance_persistent_401_terminates_without_mutation(self):
        self.plans["balance"] = [401, 401]
        with self.assertRaises(plugin.ApiError) as raised:
            await self.client.balance_op(7, 0.125, "add", "simulation")
        self.assertNotIsInstance(raised.exception, plugin.OutcomeUnknown)
        self.assertEqual(raised.exception.code, 401)
        self._assert_sanitized(raised.exception)
        self.assertEqual(self.login_count, 2)
        self.assertEqual(len(self._calls("/7/balance")), 2)
        self.assertEqual(self.mutations, [])

    async def test_balance_uncertain_statuses_are_unknown_and_never_retried(self):
        for status in (404, 408, 409, 500, 502, 503):
            with self.subTest(status=status):
                before = len(self._calls("/7/balance"))
                self.plans["balance"] = [status]
                with self.assertRaises(plugin.OutcomeUnknown) as raised:
                    await self.client.balance_op(7, 0.125, "add", "simulation")
                self._assert_sanitized(raised.exception)
                self.assertEqual(len(self._calls("/7/balance")), before + 1)
        self.assertEqual(self.users[7]["balance"], 5.0)
        self.assertEqual(self.mutations, [])

    async def test_non_200_success_after_commit_is_unknown_without_retry(self):
        for status in (201, 202, 204):
            with self.subTest(status=status):
                before = len(self._calls("/7/balance"))
                mutation_count = len(self.mutations)
                self.plans["balance"] = [status]
                with self.assertRaises(plugin.OutcomeUnknown) as raised:
                    await self.client.balance_op(7, 0.125, "add", f"simulation:http-{status}")
                self._assert_sanitized(raised.exception)
                self.assertEqual(len(self._calls("/7/balance")), before + 1)
                self.assertEqual(len(self.mutations), mutation_count + 1)
        self.assertEqual(self.users[7]["balance"], 5.375)

    async def test_invalid_success_responses_are_unknown_and_never_retried(self):
        for response in ("non_json", "broken_json", "missing_data"):
            with self.subTest(response=response):
                before = len(self._calls("/7/balance"))
                mutation_count = len(self.mutations)
                self.plans["balance"] = [response]
                with self.assertRaises(plugin.OutcomeUnknown) as raised:
                    await self.client.balance_op(7, 0.125, "add", f"simulation:{response}")
                self._assert_sanitized(raised.exception)
                self.assertEqual(len(self._calls("/7/balance")), before + 1)
                self.assertEqual(len(self.mutations), mutation_count + 1)
        self.assertEqual(self.users[7]["balance"], 5.375)

    async def test_disconnect_after_commit_is_unknown_without_duplicate_write(self):
        self.plans["balance"] = ["commit_then_drop"]
        with self.assertRaises(plugin.OutcomeUnknown) as raised:
            await self.client.balance_op(7, 0.125, "add", "simulation")
        self._assert_sanitized(raised.exception)
        self.assertEqual(self.users[7]["balance"], 5.125)
        self.assertEqual(len(self._calls("/7/balance")), 1)
        self.assertEqual(len(self.mutations), 1)

    async def test_balance_rejected_4xx_is_sanitized_and_not_retried(self):
        for status in (400, 403, 422, 429):
            with self.subTest(status=status):
                before = len(self._calls("/7/balance"))
                self.plans["balance"] = [status]
                with self.assertRaises(plugin.ApiError) as raised:
                    await self.client.balance_op(7, 0.125, "add", "simulation")
                self.assertNotIsInstance(raised.exception, plugin.OutcomeUnknown)
                self.assertEqual(raised.exception.code, status)
                self._assert_sanitized(raised.exception)
                self.assertEqual(len(self._calls("/7/balance")), before + 1)
        self.assertEqual(self.mutations, [])

    async def test_login_failure_never_sends_balance_request_or_exposes_body(self):
        for response in (401, 500, "non_json", "broken_json", "missing_data"):
            with self.subTest(response=response):
                self.plans["login"] = [response]
                with self.assertRaises(plugin.ApiError) as raised:
                    await self.client.balance_op(7, 0.125, "add", "simulation")
                self.assertNotIsInstance(raised.exception, plugin.OutcomeUnknown)
                self._assert_sanitized(raised.exception)
        self.assertEqual(self.login_count, 5)
        self.assertEqual(self._calls("/7/balance"), [])

    async def test_read_failures_do_not_expose_upstream_diagnostics(self):
        for response in (500, "non_json", "broken_json"):
            with self.subTest(response=response):
                self.plans["search"] = [response]
                with self.assertRaises(plugin.ApiError) as raised:
                    await self.client.find_user_by_email("alice@example.test")
                self._assert_sanitized(raised.exception)

    async def test_exact_negative_balance_rejection_is_known_not_applied(self):
        for current, result in (("0.10", "-0.02"), ("-12.34", "-12.47")):
            with self.subTest(current=current):
                message = (
                    f"balance cannot be negative, current balance: {current}, "
                    f"requested operation would result in: {result}"
                )
                self.plans["balance"] = [{
                    "http_status": 500, "payload": {"message": message, "private": SECRET},
                }]
                before = len(self._calls("/7/balance"))
                with self.assertRaises(plugin.ApiError) as raised:
                    await self.client.balance_op(7, 0.125, "subtract", "simulation negative")
                self.assertNotIsInstance(raised.exception, plugin.OutcomeUnknown)
                self.assertEqual(raised.exception.category, "balance_negative_rejected")
                self.assertTrue(raised.exception.known_not_applied)
                self.assertEqual(raised.exception.code, 500)
                self._assert_sanitized(raised.exception)
                self.assertNotIn(current, str(raised.exception))
                self.assertNotIn(result, str(raised.exception))
                self.assertEqual(len(self._calls("/7/balance")), before + 1)
        self.assertEqual(self.mutations, [])

    async def test_similar_negative_messages_remain_unknown_and_sanitized(self):
        exact = (
            "balance cannot be negative, current balance: 0.10, "
            "requested operation would result in: -0.02"
        )
        messages = (
            "error: " + exact, exact + " " + SECRET, exact + "\n",
            exact.replace("0.10", "0.100"), exact.replace("0.10", "0x10"),
            exact.replace("balance cannot", "Balance cannot"),
            exact.replace("0.10", "+0.10"), None,
        )
        plans = [{"http_status": 500, "payload": {"message": msg}} for msg in messages]
        plans.extend((
            {"http_status": 500, "payload": {"error": {"message": exact}}},
            {"http_status": 500, "text": exact},
            {"http_status": 502, "payload": {"message": exact}},
        ))
        for plan in plans:
            with self.subTest(plan=plan):
                before = len(self._calls("/7/balance"))
                self.plans["balance"] = [plan]
                with self.assertRaises(plugin.OutcomeUnknown) as raised:
                    await self.client.balance_op(7, 0.125, "subtract", "simulation ambiguous")
                self.assertFalse(raised.exception.known_not_applied)
                self._assert_sanitized(raised.exception)
                self.assertNotIn("0.10", str(raised.exception))
                self.assertNotIn("-0.02", str(raised.exception))
                self.assertEqual(len(self._calls("/7/balance")), before + 1)
        self.assertEqual(self.mutations, [])

    async def test_connection_refused_before_send_is_known_not_applied(self):
        await self.client._token_valid()
        # A bound but non-listening loopback port cannot receive an HTTP request.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as unavailable:
            unavailable.bind(("127.0.0.1", 0))
            self.client.base = f"http://127.0.0.1:{unavailable.getsockname()[1]}/api/v1"
            with self.assertRaises(plugin.ApiError) as raised:
                await self.client.balance_op(7, 0.125, "add", "simulation disconnected")
        self.assertNotIsInstance(raised.exception, plugin.OutcomeUnknown)
        self.assertEqual(raised.exception.category, "connector_not_sent")
        self.assertTrue(raised.exception.known_not_applied)
        self._assert_sanitized(raised.exception)
        self.assertEqual(self._calls("/7/balance"), [])
        self.assertEqual(self.mutations, [])

    async def test_404_after_commit_remains_unknown_without_resending(self):
        self.plans["balance"] = ["commit_then_404"]
        with self.assertRaises(plugin.OutcomeUnknown) as raised:
            await self.client.balance_op(7, 0.125, "add", "simulation committed before lookup")
        self.assertFalse(raised.exception.known_not_applied)
        self._assert_sanitized(raised.exception)
        self.assertEqual(self.users[7]["balance"], 5.125)
        self.assertEqual(len(self._calls("/7/balance")), 1)
        self.assertEqual(len(self.mutations), 1)

    async def test_history_returns_exact_signed_ledger_record_via_read_only_request(self):
        for op, value in (("subtract", "-0.125"), ("add", "0.125")):
            with self.subTest(op=op):
                record = self._ledger_record(value=value)
                self.history = [record]
                self.assertEqual(await self._lookup(op=op), record)
        calls = self._calls("/7/balance-history")
        self.assertEqual(len(calls), 2)
        for request in calls:
            self.assertEqual(request["method"], "GET")
            self.assertEqual(request["query"], {
                "type": "admin_balance", "page": "1", "page_size": "100",
            })
        self.assertEqual(self._calls("/7/balance"), [])
        self.assertEqual(self.mutations, [])

    async def test_history_absence_and_partial_notes_are_not_evidence(self):
        for history in ([], [self._ledger_record(notes=self.operation_notes + ":other")],
                        [self._ledger_record(notes="prefix:" + self.operation_notes)]):
            with self.subTest(history=history):
                self.history = history
                self.assertIsNone(await self._lookup())
        self.assertEqual(self.mutations, [])

    async def test_history_accepts_float_roundoff_within_absolute_tolerance(self):
        for value in ("-0.12500000000000003", "-0.1250001", "-0.1249999"):
            with self.subTest(value=value):
                self.history = [self._ledger_record(value=value)]
                self.assertEqual((await self._lookup())["id"], 301)

    async def test_history_matching_notes_with_conflicting_fields_are_rejected(self):
        cases = (
            {"used_by": 8}, {"used_by": "7"}, {"type": "balance"},
            {"status": "unused"}, {"id": 0}, {"id": True}, {"id": "301"},
            {"value": "0.125"}, {"value": "-0.12500011"},
            {"value": "-0.12499989"}, {"value": "NaN"}, {"value": None},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.history = [self._ledger_record(**changes)]
                with self.assertRaises(plugin.ApiError) as raised:
                    await self._lookup()
                self.assertEqual(raised.exception.category, "evidence_conflict")
                self._assert_sanitized(raised.exception)
        self.assertEqual(self._calls("/7/balance"), [])

    async def test_history_uses_timezone_aware_used_at_and_rejects_stale_or_future_evidence(self):
        stale = datetime.fromtimestamp(self.transaction_time - 6, timezone.utc).isoformat()
        future = datetime.fromtimestamp(time.time() + 120, timezone.utc).isoformat()
        naive = datetime.fromtimestamp(self.transaction_time + 1).isoformat()
        for timestamp in (stale, future, naive, "invalid", SECRET):
            with self.subTest(timestamp=timestamp):
                self.history = [self._ledger_record(used_at=timestamp)]
                with self.assertRaises(plugin.ApiError) as raised:
                    await self._lookup()
                self.assertEqual(raised.exception.category, "evidence_conflict")
                self._assert_sanitized(raised.exception)

    async def test_history_used_at_takes_priority_and_created_at_is_fallback(self):
        stale = datetime.fromtimestamp(self.transaction_time - 30, timezone.utc).isoformat()
        self.history = [self._ledger_record(created_at=stale)]
        self.assertEqual((await self._lookup())["id"], 301)
        fallback = self._ledger_record()
        fallback.pop("used_at")
        self.history = [fallback]
        self.assertEqual(await self._lookup(), fallback)
        boundary = datetime.fromtimestamp(self.transaction_time - 5, timezone.utc).isoformat()
        self.history = [self._ledger_record(used_at=boundary)]
        self.assertEqual((await self._lookup())["id"], 301)

    async def test_history_scans_later_pages_before_accepting_unique_match(self):
        self.history = [self._ledger_record(id=1000 + i, notes=f"other:{i}") for i in range(100)]
        self.history.append(self._ledger_record())
        self.assertEqual((await self._lookup())["id"], 301)
        self.assertEqual([r["query"]["page"] for r in self._calls("/7/balance-history")], ["1", "2"])

    async def test_history_rejects_duplicate_notes_across_pages(self):
        self.history = [self._ledger_record()]
        self.history.extend(self._ledger_record(id=1000 + i, notes=f"other:{i}") for i in range(99))
        self.history.append(self._ledger_record(id=302))
        with self.assertRaises(plugin.ApiError) as raised:
            await self._lookup()
        self.assertEqual(raised.exception.category, "evidence_conflict")
        self.assertEqual(len(self._calls("/7/balance-history")), 2)

    async def test_history_invalid_pagination_is_unavailable(self):
        valid = {"items": [], "total": 0, "page": 1, "page_size": 100, "pages": 1}
        cases = (
            {"items": {}}, {"total": -1}, {"total": "0"}, {"total": True},
            {"page": 2}, {"page_size": 0}, {"page_size": 101}, {"page_size": "100"},
            {"pages": -1}, {"pages": "1"}, {"total": 1}, {"total": 101, "pages": 2},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.history_pages = {1: {**valid, **changes}}
                with self.assertRaises(plugin.ApiError) as raised:
                    await self._lookup()
                self.assertEqual(raised.exception.category, "evidence_unavailable")
                self._assert_sanitized(raised.exception)

    async def test_history_exceeding_page_limit_does_not_accept_incomplete_evidence(self):
        self.history = [self._ledger_record()]
        self.history.extend(self._ledger_record(id=1000 + i, notes=f"other:{i}") for i in range(2000))
        with self.assertRaises(plugin.ApiError) as raised:
            await self._lookup()
        self.assertEqual(raised.exception.category, "evidence_unavailable")
        self.assertLessEqual(len(self._calls("/7/balance-history")), 20)
        self.assertEqual(self.mutations, [])

    async def test_history_http_and_malformed_response_errors_are_unavailable(self):
        for response in (403, 500, "non_json", "broken_json", "missing_data"):
            with self.subTest(response=response):
                self.plans["history"] = [response]
                with self.assertRaises(plugin.ApiError) as raised:
                    await self._lookup()
                self.assertEqual(raised.exception.category, "evidence_unavailable")
                self._assert_sanitized(raised.exception)
        self.assertEqual(self._calls("/7/balance"), [])

    async def test_client_timeout_splits_connect_total_and_read(self):
        default = plugin.Sub2ApiClient("http://127.0.0.1:1/api/v1", EMAIL, PASSWORD)
        self.assertEqual((default.timeout.total, default.timeout.connect), (30, 5))
        self.assertEqual((default.read_timeout.total, default.read_timeout.connect), (30, 5))
        tuned = plugin.Sub2ApiClient("http://127.0.0.1:1/api/v1", EMAIL, PASSWORD, timeout_seconds=7)
        self.assertEqual((tuned.timeout.total, tuned.read_timeout.total), (7, 7))
        split = plugin.Sub2ApiClient("http://127.0.0.1:1/api/v1", EMAIL, PASSWORD,
                                     timeout_seconds=7, read_timeout_seconds=9)
        self.assertEqual((split.timeout.total, split.read_timeout.total), (7, 9))
        self.assertEqual((split.timeout.connect, split.read_timeout.connect), (5, 5))
        with self.assertRaises(ValueError):
            plugin.Sub2ApiClient("http://127.0.0.1:1/api/v1", EMAIL, PASSWORD, timeout_seconds=0)
        with self.assertRaises(ValueError):
            plugin.Sub2ApiClient("http://127.0.0.1:1/api/v1", EMAIL, PASSWORD, read_timeout_seconds=0)

    async def test_history_lookup_records_total_for_near_limit_monitoring(self):
        self.history = [self._ledger_record(id=1000 + i, notes=f"other:{i}") for i in range(100)]
        self.history.append(self._ledger_record())
        self.assertIsNotNone(await self._lookup())
        self.assertEqual(self.client.history_totals, {7: 101})
        self.assertEqual(self.mutations, [])

    def _slow_client(self, *, write=None, read=None):
        return plugin.Sub2ApiClient(
            self.client.base, EMAIL, PASSWORD,
            timeout_seconds=5 if write is None else write,
            read_timeout_seconds=5 if read is None else read,
        )

    async def test_get_transport_failure_retries_once_and_recovers(self):
        # 签到前的只读账号预检 GET 在传输层断开时自动重试一次（幂等、无账务副作用）。
        self.plans["user"] = ["partial"]
        self.assertEqual((await self.client.get_user(7))["id"], 7)
        self.assertEqual(len(self._calls("/users/7")), 2)
        self.assertEqual(self.mutations, [])

    async def test_get_transport_failure_after_retry_keeps_default_message(self):
        self.plans["user"] = ["partial", "partial"]
        with self.assertRaises(plugin.ApiError) as raised:
            await self.client.get_user(7)
        self.assertEqual(str(raised.exception), "sub2api 服务暂时不可用，请稍后重试。")
        self.assertEqual(raised.exception.category, "request_rejected")
        self.assertTrue(raised.exception.known_not_applied)
        self._assert_sanitized(raised.exception)
        self.assertEqual(len(self._calls("/users/7")), 2)
        self.assertEqual(self.mutations, [])

    async def test_get_timeout_retries_once_and_recovers(self):
        # 重试命中已暖起来的路径（上游慢查询首次已把热数据读进缓存）即可救回。
        self.client = self._slow_client(read=0.4)
        self.plans["user"] = ["sleep"]
        self.assertEqual((await self.client.get_user(7))["id"], 7)
        self.assertEqual(len(self._calls("/users/7")), 2)

    async def test_get_timeout_after_retry_reports_read_timeout(self):
        self.client = self._slow_client(read=0.4)
        self.plans["user"] = ["sleep", "sleep"]
        with self.assertRaises(plugin.ApiError) as raised:
            await self.client.get_user(7)
        self.assertEqual(str(raised.exception), "账号数据查询超时，请稍后重试。")
        self.assertEqual(raised.exception.category, "read_timeout")
        self.assertTrue(raised.exception.known_not_applied)
        self._assert_sanitized(raised.exception)
        self.assertEqual(len(self._calls("/users/7")), 2)
        self.assertEqual(self.mutations, [])

    async def test_get_401_refresh_and_transport_retry_are_independent(self):
        # 401 刷新 token 一次与 GET 传输层重试一次互不挤占：401 后断开仍可重试成功。
        # 无论 aiohttp 是否在单次请求内对断开自动重发，本用例均为 3 个请求、2 次登录。
        self.plans["user"] = [401, "drop"]
        self.assertEqual((await self.client.get_user(7))["id"], 7)
        self.assertEqual(len(self._calls("/users/7")), 3)
        self.assertEqual(self.login_count, 2)

    async def test_write_timeout_is_unknown_and_never_retried(self):
        # 账务铁律：写请求传输层失败（含超时）只判结果未知，绝不自动重发。
        self.client = self._slow_client(write=0.4)
        self.plans["balance"] = ["sleep"]
        with self.assertRaises(plugin.OutcomeUnknown) as raised:
            await self.client.balance_op(7, 0.125, "add", "simulation write timeout")
        self.assertFalse(raised.exception.known_not_applied)
        self._assert_sanitized(raised.exception)
        self.assertEqual(len(self._calls("/7/balance")), 1)
        self.assertEqual(self.mutations, [])

    async def test_get_connection_refused_keeps_connector_message(self):
        await self.client._token_valid()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as unavailable:
            unavailable.bind(("127.0.0.1", 0))
            self.client.base = f"http://127.0.0.1:{unavailable.getsockname()[1]}/api/v1"
            with self.assertRaises(plugin.ApiError) as raised:
                await self.client.get_user(7)
        self.assertNotIsInstance(raised.exception, plugin.OutcomeUnknown)
        self.assertEqual(raised.exception.category, "connector_not_sent")
        self.assertTrue(raised.exception.known_not_applied)
        self._assert_sanitized(raised.exception)
        self.assertEqual(self._calls("/users/7"), [])


if __name__ == "__main__":
    unittest.main()
