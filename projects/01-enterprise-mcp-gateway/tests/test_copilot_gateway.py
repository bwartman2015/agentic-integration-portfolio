"""Milestone 6: the copilot on the gateway (McpToolSource), pgvector long-term memory and embeddings.

Stdlib only, no network or database: the gateway is a fake ToolSource over the seeded mock systems that
applies the gateway's region rule, and Postgres is a fake runner that evaluates the three memory queries
(cosine similarity in Python). The real MCP SDK path runs in copilot/tests (make copilot-test), and the
live path with `make ask`.
"""
import io
import json
import math
import os
import pathlib
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

os.environ.setdefault("MEMORY_SQLITE", ":memory:")
os.environ.setdefault("TRACE_FILE", os.devnull)
os.environ.setdefault("PROGRESS_DIR", tempfile.mkdtemp())

from agentcore import InMemorySink, MemoryItem, Percept, Tool, Tracer, current_trace_id
from agentcore.observe import _current_trace
from cs_copilot import ask
from cs_copilot.agent import guardrails as G
from cs_copilot.agent import memory as M
from cs_copilot.agent import tools as T
from cs_copilot.agent.build import build_cs_copilot, gateway_registry, learn_aliases, memory_audience, principal_for_role
from cs_copilot.agent.embeddings import DIM, EmbeddingError, HashingEmbedder, LlamaCppEmbedder, get_embedder
from cs_copilot.agent.mcp_client import (GatewayToolError, KeycloakTokens, McpToolSource, TokenError,
                                         request_headers)
from cs_copilot.agent.reason import scripted_llm
from cs_copilot.agent.state import GatewayStateProvider
from mock_systems import get_systems, reset_systems

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "db"))
import roles  # noqa: E402

REP, LEAD = principal_for_role("cs-rep"), principal_for_role("cs-lead")
DEMO_Q = ask.DEMO_QUESTION
SECRET = "s3cr3t-" + "x" * 20


# ---------------------------------------------------------------- fakes
class FakeGateway:
    """The gateway's contract over the seeded mock systems: its tool list (it also offers a tool the
    copilot does not know, and claims every tool is harmless), the region rule per record, and refusals
    as errors with the gateway's own wording. Records each call with the trace id it carried."""

    def __init__(self, principal, decision="approve"):
        self.principal, self.calls, self.decision = principal, [], decision

    def list_tools(self):
        specs = [{"name": t.name, "description": "harmless read", "inputSchema": {
            "type": "object", "properties": {k: {"type": v} for k, v in t.params.items()}, "required": t.required}}
            for t in T.TOOLS]
        scopes = G.TOOL_SCOPES
        visible = [t for t in specs if scopes.get(t["name"]) in self.principal.scopes]     # tools/list by scope
        return visible + [{"name": "debug_dump", "description": "", "inputSchema": {"type": "object"}}]

    def _visible(self, region):
        regions = self.principal.attr("regions")
        return "*" in regions or (region is not None and region in regions)

    def call_tool(self, name, args):
        self.calls.append((name, dict(args), current_trace_id()))
        systems = get_systems()
        if name == "search_accounts":
            hits = systems.crm.find_accounts(args["query"])
            visible = [h for h in hits if self._visible(h.get("region") or None)]
            out = {"query": args["query"], "matches": visible}
            if len(hits) > len(visible):
                out["hidden_out_of_region"] = len(hits) - len(visible)
            return out
        if name == "create_case" and self.decision != "approve":
            raise GatewayToolError("Error executing tool create_case: Access denied: a human approver rejected "
                                   "this create_case request. Nothing was written.")
        regions = G.resource_regions(name, args)
        if regions and not all(self._visible(r) for r in regions):
            raise GatewayToolError("Access denied: this record belongs to an account outside your region "
                                   f"({self.principal.id} may see: Central). Ask a CS lead.")
        handler = {t.name: t.handler for t in T.TOOLS}[name]
        try:
            return handler(**args)
        except KeyError as exc:
            raise GatewayToolError(f"{exc} was not found.") from None


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b))


class FakePg:
    """A runner for PgVectorMemory that evaluates its three statements like Postgres + pgvector would."""

    def __init__(self, fail=False):
        self.rows, self.fail, self.queries = [], fail, []

    async def __call__(self, method, query, params):
        self.queries.append((method, query, params))
        if self.fail:
            raise ConnectionRefusedError("db down")
        if query == M.INSERT:
            audience, kind, text, meta, model, vec = params
            self.rows.append(dict(audience=audience, kind=kind, text=text, meta=meta, model=model,
                                  vec=json.loads(vec)))
            return "INSERT 0 1"
        if query == M.ALIAS_EXISTS:
            audience, model, text = params
            return 1 if any(r["audience"] == audience and r["model"] == model and r["kind"] == "alias"
                            and r["text"] == text for r in self.rows) else None
        if query == M.RECALL:
            vec, audience, model, limit = json.loads(params[0]), params[1], params[2], params[3]
            hits = [dict(r, score=cosine(vec, r["vec"])) for r in self.rows
                    if r["audience"] == audience and r["model"] == model]
            return sorted(hits, key=lambda r: -r["score"])[:limit]
        raise AssertionError(f"unexpected query {query}")


def gateway_agent(script, principal, memory=None, decision="approve"):
    gw = FakeGateway(principal, decision)
    agent = build_cs_copilot(scripted_llm(script), source=gw)
    agent.tracer = Tracer(InMemorySink())
    agent.memory = memory or M.PgVectorMemory(FakePg(), HashingEmbedder())
    return agent, gw


# ---------------------------------------------------------------- embeddings
class EmbeddingTests(unittest.TestCase):
    def test_hashing_embedder_is_deterministic_unit_length_and_finds_shared_wording(self):
        e = HashingEmbedder()
        a, b = e.embed_query(DEMO_Q), e.embed_query(DEMO_Q)
        self.assertEqual(a, b)
        self.assertEqual(len(a), DIM)
        self.assertAlmostEqual(math.sqrt(sum(x * x for x in a)), 1.0, places=6)
        near = e.embed_document("Q: " + DEMO_Q + "\nA: SO-104233 shipped late.")
        far = e.embed_document("Q: What is the return policy for damaged pallets?")
        self.assertGreater(cosine(a, near), 0.5)
        self.assertGreater(cosine(a, near), cosine(a, far) + 0.3)

    def test_llamacpp_embedder_sends_nomic_task_prefixes_and_checks_the_vector(self):
        sent = []

        def fake_open(req, timeout):
            sent.append(json.loads(req.data))
            return io.BytesIO(json.dumps({"data": [{"embedding": [3.0] + [0.0] * (DIM - 1)}]}).encode())

        e = LlamaCppEmbedder("http://embeddings:8080/")
        with mock.patch("urllib.request.urlopen", fake_open):
            vec = e.embed_query("where is my order")
            e.embed_document("an answer")
        self.assertEqual([s["input"] for s in sent], [["search_query: where is my order"], ["search_document: an answer"]])
        self.assertEqual(vec[0], 1.0)                                  # normalized
        with mock.patch("urllib.request.urlopen", lambda req, timeout: io.BytesIO(b'{"data":[{"embedding":[1,2]}]}')):
            with self.assertRaises(EmbeddingError):
                e.embed_query("x")
        with mock.patch("urllib.request.urlopen", side_effect=ConnectionRefusedError()):
            with self.assertRaises(EmbeddingError):
                e.embed_query("x")

    def test_embedder_choice_follows_embeddings_url(self):
        with mock.patch.dict(os.environ, {"EMBEDDINGS_URL": ""}):
            self.assertIsInstance(get_embedder(), HashingEmbedder)
        with mock.patch.dict(os.environ, {"EMBEDDINGS_URL": "http://embeddings:8080"}):
            self.assertIsInstance(get_embedder(), LlamaCppEmbedder)


# ---------------------------------------------------------------- memory
class MemoryConfigTests(unittest.TestCase):
    def test_remote_url_needs_tls_and_drops_libpq_only_options(self):
        url = f"postgresql://copilot_memory:{SECRET}@ep-x.neon.example/neondb?sslmode=require&channel_binding=require"
        cfg = M.MemoryConfig.from_env({"MEMORY_DATABASE_URL": url})
        self.assertNotIn("channel_binding", cfg.dsn)
        self.assertNotIn(SECRET, repr(cfg))
        full = M.MemoryConfig.from_env({"MEMORY_DATABASE_URL": url.replace("require&", "verify-full&")})
        self.assertIn("sslrootcert=%2Fetc%2Fssl%2Fcerts%2Fca-certificates.crt", full.dsn)
        for bad in (url.replace("?sslmode=require&", "?"), "mysql://u@h/db", "postgresql://h/db", "postgresql://u@h:99999/db"):
            with self.assertRaises(M.MemoryConfigError) as ctx:
                M.MemoryConfig.from_env({"MEMORY_DATABASE_URL": bad})
            self.assertNotIn(SECRET, str(ctx.exception))
        self.assertTrue(M.MemoryConfig.from_env({"MEMORY_DATABASE_URL": "postgresql://u:p@db/ridgeline"}).dsn)

    def test_local_database_needs_the_role_password(self):
        cfg = M.MemoryConfig.from_env({"COPILOT_MEMORY_DB_PASSWORD": SECRET})
        self.assertEqual((cfg.host, cfg.database, cfg.user), ("db", "ridgeline", "copilot_memory"))
        for env in ({}, {"COPILOT_MEMORY_DB_PASSWORD": "short"},
                    {"COPILOT_MEMORY_DB_PASSWORD": SECRET, "MEMORY_DB_NAME": "x; drop"}):
            with self.assertRaises(M.MemoryConfigError):
                M.MemoryConfig.from_env(env)

    def test_backend_selection(self):
        with mock.patch.dict(os.environ, {"MEMORY_BACKEND": ""}):
            self.assertEqual(type(M.get_memory()).__name__, "SQLiteMemory")
        with mock.patch.dict(os.environ, {"MEMORY_BACKEND": "pgvector", "COPILOT_MEMORY_DB_PASSWORD": SECRET,
                                          "EMBEDDINGS_URL": "", "MEMORY_DATABASE_URL": ""}):
            self.assertIsInstance(M.get_memory(), M.PgVectorMemory)


class PgVectorMemoryTests(unittest.TestCase):
    def setUp(self):
        self.pg = FakePg()
        self.mem = M.PgVectorMemory(self.pg, HashingEmbedder())

    def test_writes_are_masked_tagged_and_recall_is_scoped_to_audience_and_model(self):
        self.mem.remember(MemoryItem("Q: where is SO-104233?\nA: call purchasing.summit@example.com", "episode",
                                     {"audience": "rep"}))
        self.assertNotIn("purchasing.summit@example.com", self.pg.rows[0]["text"])
        self.assertEqual((self.pg.rows[0]["audience"], self.pg.rows[0]["model"]), ("rep", "hashing-v1-768"))
        self.assertTrue(self.mem.recall("where is SO-104233?", audience="rep"))
        self.assertEqual(self.mem.recall("where is SO-104233?", audience="lead"), [])
        self.assertEqual(self.mem.recall("where is SO-104233?"), [])           # no audience, no recall
        recall = [q for q in self.pg.queries if q[1] == M.RECALL][0]
        self.assertEqual(recall[2][1:3], ("rep", "hashing-v1-768"))            # filtered in SQL

    def test_untagged_or_unknown_kinds_are_not_stored_and_aliases_once(self):
        self.mem.remember(MemoryItem("no audience", "episode", {}))
        self.mem.remember(MemoryItem("odd kind", "secret", {"audience": "rep"}))
        for _ in range(2):
            self.mem.remember(MemoryItem("Account alias: 'bbo' means Big Box Outdoor (ACCT-1001).", "alias",
                                         {"audience": "rep"}))
        self.assertEqual([r["kind"] for r in self.pg.rows], ["alias"])

    def test_low_similarity_is_not_recalled(self):
        self.mem.remember(MemoryItem("Q: what is the return policy for damaged pallets", "episode", {"audience": "rep"}))
        self.assertEqual(self.mem.recall("invoice INV-90388 total for Summit", audience="rep"), [])

    def test_database_failure_never_loses_the_answer(self):
        mem = M.PgVectorMemory(FakePg(fail=True), HashingEmbedder())
        with mock.patch("sys.stderr", io.StringIO()):
            mem.remember(MemoryItem("x", "episode", {"audience": "rep"}))
            self.assertEqual(mem.recall("x", audience="rep"), [])
        self.assertEqual(mem.last_error, "recall: ConnectionRefusedError")


# ---------------------------------------------------------------- MCP client
class FakeSession:
    def __init__(self, result=None, tools=None):
        self.result, self.tools, self.seen = result, tools or [], []

    def __call__(self, url, headers, timeout_s):
        self.seen.append((url, headers, timeout_s))
        outer = self

        class Ctx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def list_tools(self):
                return {"tools": outer.tools}

            async def call_tool(self, name, args):
                return outer.result
        return Ctx()


class McpToolSourceTests(unittest.TestCase):
    def test_call_sends_token_and_trace_id_and_returns_structured_content(self):
        session = FakeSession(result={"is_error": False, "structured_content": {"ok": 1}, "content": []})
        source = McpToolSource("http://gateway:8000/mcp", lambda: "tok", timeout_s=5, session=session)
        token = _current_trace.set("a1b2c3d4")
        try:
            self.assertEqual(source.call_tool("get_sales_order", {"sales_order": "SO-104233"}), {"ok": 1})
        finally:
            _current_trace.reset(token)
        self.assertEqual(session.seen[0][1], {"Authorization": "Bearer tok", "x-request-id": "a1b2c3d4"})

    def test_gateway_refusal_is_raised_with_its_message(self):
        session = FakeSession(result={"is_error": True, "content": [{"text": "Access denied: ask a CS lead."}]})
        source = McpToolSource("u", lambda: "tok", timeout_s=5, session=session)
        with self.assertRaisesRegex(GatewayToolError, "Access denied"):
            source.call_tool("track_shipment", {"sales_order": "SO-1"})

    def test_list_tools_in_mcp_shape_and_malformed_trace_ids_are_not_sent(self):
        session = FakeSession(tools=[{"name": "search_accounts", "description": "d", "input_schema": {"type": "object"}}])
        source = McpToolSource("u", lambda: "tok", timeout_s=5, session=session)
        self.assertEqual(source.list_tools(), [{"name": "search_accounts", "description": "d",
                                                "inputSchema": {"type": "object"}}])
        self.assertNotIn("x-request-id", request_headers("tok", "bad id\r\nx: y"))

    def test_write_timeout_outlasts_the_gateway_approval_wait(self):
        with mock.patch.dict(os.environ, {"APPROVAL_TIMEOUT_S": "120"}):
            self.assertGreater(McpToolSource("u", lambda: "t").timeout_s, 120)


class KeycloakTokenTests(unittest.TestCase):
    JWT = "aaa.bbb.ccc"

    def test_token_is_cached_until_near_expiry(self):
        calls = []

        def opener(req, timeout):
            calls.append(req)
            return io.BytesIO(json.dumps({"access_token": self.JWT, "expires_in": 300}).encode())

        tokens = KeycloakTokens("cs-rep", SECRET, "http://keycloak/token", opener=opener)
        self.assertEqual((tokens(), tokens()), (self.JWT, self.JWT))
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].get_header("Authorization").startswith("Basic "))

    def test_errors_never_show_the_secret(self):
        def refused(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b""))
        for opener in (refused, lambda req, timeout: io.BytesIO(b'{"access_token": "not a jwt"}')):
            with self.assertRaises(TokenError) as ctx:
                KeycloakTokens("cs-rep", SECRET, "http://keycloak/token", opener=opener)()
            self.assertNotIn(SECRET, str(ctx.exception))
        with self.assertRaises(TokenError):
            KeycloakTokens.for_role("cs-rep", {})
        with self.assertRaises(TokenError):
            KeycloakTokens.for_role("p01-auditor", {"OIDC_TOKEN_URL": "x"})


# ---------------------------------------------------------------- governance with the gateway backend
class GatewayRegistryTests(unittest.TestCase):
    def test_scopes_and_side_effects_come_from_the_copilots_policy_not_the_server(self):
        reg = gateway_registry(FakeGateway(LEAD), ["search_accounts", "create_case"])
        self.assertEqual(reg.get("create_case").side_effect, "write")      # the server called it harmless
        self.assertEqual(reg.get("create_case").scope, "crm.write")
        self.assertEqual(reg.get("search_accounts").scope, "crm.read")
        self.assertIsNone(reg.get("debug_dump"))                            # not asked for, not policy-known

    def test_an_empty_listing_fails_the_build(self):
        gw = FakeGateway(REP)
        gw.list_tools = lambda: []
        with self.assertRaisesRegex(RuntimeError, "lists none"):
            gateway_registry(gw, ["search_accounts"])

    def test_a_tool_not_listed_for_this_caller_is_declared_locally_and_never_called(self):
        gw = FakeGateway(REP)                                               # a rep's listing has no create_case
        self.assertNotIn("create_case", [t["name"] for t in gw.list_tools()])
        reg = gateway_registry(gw, ["search_accounts", "create_case"])
        self.assertEqual(reg.get("create_case").scope, "crm.write")
        with self.assertRaisesRegex(PermissionError, "does not offer create_case"):
            reg.get("create_case").handler(account_id="ACCT-1002", subject="s", summary="s")
        self.assertEqual(gw.calls, [])


class GatewayGuardrailTests(unittest.TestCase):
    def setUp(self):
        reset_systems()
        self.read = Tool("get_sales_order", "", handler=lambda **k: {}, scope="erp.read", category="mcp")
        self.write = Tool("create_case", "", handler=lambda **k: {}, scope="crm.write", side_effect="write", category="mcp")

    def test_region_rule_is_left_to_the_gateway_scope_and_restricted_stay_local(self):
        west_order = next(so for so, o in get_systems().dataset.orders.items()
                          if get_systems().dataset.accounts[o["account_id"]].get("region") == "West")
        self.assertFalse(G.policy_hook(REP, self.read, {"sales_order": west_order})[0])        # in-process
        self.assertTrue(G.policy_hook(REP, self.read, {"sales_order": west_order}, gateway=True)[0])
        self.assertFalse(G.policy_hook(REP, self.write, {"account_id": "ACCT-1002"}, gateway=True)[0])
        restricted = Tool("create_case", "", handler=lambda **k: {}, scope="crm.write", side_effect="write")
        ok, why = G.policy_hook(LEAD, restricted, {"account_id": "A", "credit_limit": 1}, gateway=True)
        self.assertFalse(ok)
        self.assertIn("human-only", why)

    def test_create_case_defers_to_the_gateways_human_approval_other_writes_refused(self):
        self.assertTrue(G.approval_hook(LEAD, self.write, {}, gateway=True))
        self.assertFalse(G.approval_hook(LEAD, self.write, {}))                 # in-process: no approver
        other = Tool("update_case", "", handler=lambda **k: {}, side_effect="write", category="mcp")
        self.assertFalse(G.approval_hook(LEAD, other, {}, gateway=True))
        local = Tool("create_case", "", handler=lambda **k: {}, side_effect="write", category="code")
        self.assertFalse(G.approval_hook(LEAD, local, {}, gateway=True))


class GatewayStateTests(unittest.TestCase):
    def setUp(self):
        reset_systems()

    def percept(self, principal, **entities):
        return Percept(kind="cs_request", content="q", principal=principal, entities=entities)

    def test_state_reads_through_the_gateway_once_per_record_and_by_scope(self):
        gw = FakeGateway(REP)
        snap = GatewayStateProvider(gw.call_tool).snapshot(
            self.percept(REP, account=["Summit Contractors LLC"], sales_order=["SO-104233", "SO-104233"]))
        self.assertEqual(snap["source"], "gateway")
        self.assertEqual(snap["accounts"][0]["account_id"], "ACCT-1002")
        self.assertIn("status", snap["orders"]["SO-104233"])
        self.assertEqual(snap["open_invoices"]["ACCT-1002"]["count"], len(get_systems().ar.open_items("ACCT-1002")))
        self.assertEqual(len(gw.calls), len({(n, json.dumps(a, sort_keys=True)) for n, a, _ in gw.calls}))
        no_ar = principal_for_role("cs-rep").__class__("r", frozenset({"crm.read"}), {"regions": ["Central"]})
        gw2 = FakeGateway(no_ar)
        GatewayStateProvider(gw2.call_tool).snapshot(self.percept(no_ar, account=["Summit Contractors LLC"],
                                                                  sales_order=["SO-104233"]))
        self.assertEqual({n for n, _, _ in gw2.calls}, {"search_accounts"})

    def test_refused_records_show_as_access_denied_without_contents(self):
        west = next((so, o) for so, o in get_systems().dataset.orders.items()
                    if get_systems().dataset.accounts[o["account_id"]].get("region") == "West")
        snap = GatewayStateProvider(FakeGateway(REP).call_tool).snapshot(self.percept(REP, sales_order=[west[0]]))
        self.assertEqual(snap["orders"][west[0]]["error"], "access denied: outside your region")
        self.assertNotIn("status", snap["orders"][west[0]])
        self.assertEqual(snap["shipments"][west[0]]["error"], "access denied: outside your region")


# ---------------------------------------------------------------- end to end, offline
class GatewayAgentTests(unittest.TestCase):
    def setUp(self):
        reset_systems()

    def test_demo_question_through_the_gateway_with_citations_then_recalled_from_memory(self):
        pg = FakePg()
        memory = M.PgVectorMemory(pg, HashingEmbedder())
        first, gw = gateway_agent("demo-late-and-billed", REP, memory)
        out = first.run(DEMO_Q, REP)
        self.assertEqual(out.status, "answered", out.answer)
        self.assertIn("[customer-service-policy.md#0]", out.answer)
        self.assertLessEqual(set(out.tools_used), {n for n, _, _ in gw.calls})
        self.assertEqual({t for _, _, t in gw.calls}, {out.trace_id})        # every call carried the trace id
        self.assertEqual(memory.last_recall, [])
        second, _ = gateway_agent("demo-late-and-billed", REP, memory)
        out2 = second.run(DEMO_Q, REP)
        self.assertEqual(out2.status, "answered")
        self.assertEqual(len(memory.last_recall), 1)
        self.assertEqual(memory.last_recall[0].meta["trace_id"], out.trace_id)
        self.assertGreater(memory.last_recall[0].score, 0.9)          # same question
        # A CS lead has another audience: the rep's answer is not recalled for it, and vice versa.
        self.assertEqual(memory.recall(DEMO_Q, audience=memory_audience(LEAD)), [])

    def test_out_of_region_refusal_comes_from_the_gateway_and_the_copilot_escalates(self):
        agent, gw = gateway_agent("out-of-region-order-denied", REP)
        out = agent.run("Where is order SO-104301 for Pinecrest Hardware?", REP)
        self.assertEqual(out.status, "escalated")
        self.assertIn("CS lead", out.answer)
        # The copilot no longer resolves regions itself: the gateway saw the calls (tools and state reads)
        # and refused them.
        self.assertLessEqual({"get_sales_order", "track_shipment"}, {n for n, _, _ in gw.calls})
        refused = [m["content"] for m in out.history if m["content"].startswith("Result of get_sales_order")]
        self.assertTrue(refused and "outside your region" in refused[0], refused)


class AskCaseRequestTests(unittest.TestCase):
    """make ask's case request: the scripted answer follows the real outcome of create_case."""

    def setUp(self):
        reset_systems()

    def test_approved_answer_quotes_the_case_id_the_crm_assigned(self):
        agent, gw = gateway_agent(ask.script_for(ask.CASE_REQUEST), LEAD)
        out = agent.run(ask.CASE_REQUEST, LEAD)
        self.assertEqual(out.status, "answered", out.answer)
        case_id = get_systems().crm.cases[-1]["case_id"]
        self.assertIn(f"Case {case_id} was created", out.answer)

    def test_rejected_answer_says_nothing_was_written_with_the_gateways_reason(self):
        agent, _ = gateway_agent(ask.script_for(ask.CASE_REQUEST), LEAD, decision="reject")
        out = agent.run(ask.CASE_REQUEST, LEAD)
        self.assertEqual(out.status, "answered", out.answer)
        self.assertIn("was not created. Reason: Access denied: a human approver rejected", out.answer)
        self.assertNotIn("GatewayToolError", out.answer)
        self.assertEqual(get_systems().crm.cases, [])

    def test_a_rep_is_refused_by_scope_before_the_gateway(self):
        # As live: the gateway does not list create_case for a rep, and the copilot's scope check refuses it.
        agent, gw = gateway_agent(ask.script_for(ask.CASE_REQUEST), REP)
        out = agent.run(ask.CASE_REQUEST, REP)
        self.assertIn("lacks scope crm.write", out.answer)
        self.assertNotIn("create_case", [n for n, _, _ in gw.calls])


class AliasLearningTests(unittest.TestCase):
    def test_a_search_under_another_name_becomes_an_alias_memory_for_that_audience(self):
        from agentcore import Outcome, ToolResult
        mem = M.PgVectorMemory(FakePg(), HashingEmbedder())
        hook = learn_aliases(lambda: mem)
        percept = Percept(kind="cs_request", content="q", principal=REP, entities={})
        outcome = Outcome("answered", "a", 1, 1, 0.0, "t1")
        one = {"query": "Summit Co", "matches": [{"name": "Summit Contractors LLC", "account_id": "ACCT-1002"}]}
        known = {"query": "summit contractors", "matches": [{"name": "Summit Contractors LLC", "account_id": "ACCT-1002"}]}
        many = {"query": "outdoor", "matches": [{"name": "A", "account_id": "ACCT-1"}, {"name": "B", "account_id": "ACCT-2"}]}
        hook(percept, outcome, [ToolResult("search_accounts", True, one), ToolResult("search_accounts", True, known),
                                ToolResult("search_accounts", True, many), ToolResult("search_accounts", False, one)])
        rows = mem.runner.rows
        self.assertEqual([r["text"] for r in rows],
                         ["Account alias: 'summit co' means Summit Contractors LLC (ACCT-1002)."])
        self.assertEqual(rows[0]["audience"], memory_audience(REP))
        self.assertTrue(mem.recall("summit co", audience=memory_audience(REP)))


# ---------------------------------------------------------------- make ask, make db-roles
class AskTests(unittest.TestCase):
    def test_known_questions_map_to_scripted_plans(self):
        self.assertEqual(ask.script_for(DEMO_Q), "demo-late-and-billed")
        self.assertEqual(ask.script_for("  " + DEMO_Q.upper().replace("?", "") + " "), "demo-late-and-billed")
        self.assertIsNone(ask.script_for("What is the meaning of life?"))
        for script in ask.known_questions().values():
            self.assertTrue((ROOT / "fixtures" / "llm" / f"{script}.json").is_file(), script)

    def test_unknown_question_with_the_scripted_model_explains_itself(self):
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "scripted"}), mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(ask.main(["What is the meaning of life?"]), 2)
        self.assertIn("--list", err.getvalue())


class DbRolesEnvTests(unittest.TestCase):
    def test_write_env_replaces_in_place_appends_missing_and_keeps_the_rest(self):
        lines = ["# comment", "DATABASE_URL=old", "OTHER=1", "DATABASE_URL=dup"]
        out = roles.write_env(lines, {"DATABASE_URL": "new", "MEMORY_DATABASE_URL": "mem"})
        self.assertEqual(out, ["# comment", 'DATABASE_URL="new"', "OTHER=1", "DATABASE_URL=dup",
                               'MEMORY_DATABASE_URL="mem"'])
        self.assertEqual(roles.read_env_value(['X="a=b"'], "X"), "a=b")

    def test_test_target_refuses_a_main_database_endpoint(self):
        main = "postgresql://o:p@ep-main-123-pooler.c-1.db.example/neondb?sslmode=require"
        direct = "postgresql://o:p@ep-main-123.c-1.db.example/neondb?sslmode=require"
        test = "postgresql://o:p@ep-test-456.c-1.db.example/neondb?sslmode=require"
        lines = [f"DATABASE_URL={main}"]
        self.assertIsInstance(roles.target_plan(lines + [f"DATABASE_ADMIN_URL_TEST={test}"], "test"), tuple)
        refused = roles.target_plan(lines + [f"DATABASE_ADMIN_URL_TEST={direct}"], "test")     # pooled vs direct
        self.assertIsInstance(refused, str)
        self.assertIn("same database", refused)
        self.assertIn("TARGET", roles.target_plan(lines, "prod"))

    def test_role_sql_never_contains_a_password(self):
        self.assertNotIn(SECRET, roles.SQL)
        self.assertIn("\\getenv pw_copilot_memory NEW_COPILOT_MEMORY_PASSWORD", roles.SQL)
        self.assertIn("\\i /db/memory.sql", roles.SQL)


class MemorySchemaTests(unittest.TestCase):
    def test_copilot_role_gets_add_and_read_on_memory_only(self):
        sql = (ROOT / "db" / "memory.sql").read_text()
        grants = [line.strip() for line in sql.splitlines() if "GRANT" in line.upper() and "copilot_memory" in line]
        self.assertTrue(grants)
        for line in grants:
            self.assertNotRegex(line.upper(), r"\b(UPDATE|DELETE|TRUNCATE|ALL)\b")
        self.assertIn("vector(768)", sql)
        self.assertIn("vector_cosine_ops", sql)
        self.assertNotIn("copilot_memory", (ROOT / "db" / "audit.sql").read_text())


if __name__ == "__main__":
    unittest.main()
