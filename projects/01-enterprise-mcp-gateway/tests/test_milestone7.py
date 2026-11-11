"""Milestone 7: real model providers, provider-aware evals and the A2A endpoint (stdlib-only parts).

No network and no SDK: Claude is a fake client object with the SDK's response shape, the local model a
fake HTTP response. Token verification with real RS256 keys runs in the copilot image (make copilot-test).
"""
import io
import json
import os
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("MEMORY_SQLITE", ":memory:")
os.environ.setdefault("TRACE_FILE", os.devnull)
os.environ.setdefault("PROGRESS_DIR", tempfile.mkdtemp())

from agentcore import AgentCard, Outcome, Principal, load_cases
from cs_copilot import a2a_server as A
from cs_copilot.agent import reason as R
from cs_copilot.agent.build import principal_for_role

ROOT = pathlib.Path(__file__).resolve().parents[1]
REP = principal_for_role("cs-rep")


def claude_response(text="{}", stop="end_turn", model="claude-sonnet-5-5", tin=1000, tout=200):
    return SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
                           stop_reason=stop, model=model,
                           usage=SimpleNamespace(input_tokens=tin, output_tokens=tout))


class FakeClaude:
    def __init__(self, resp):
        self.resp, self.calls = resp, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **params):
        self.calls.append(params)
        return self.resp


class ClaudeProviderTests(unittest.TestCase):
    def test_request_shape_cost_and_served_model(self):
        fake = FakeClaude(claude_response('{"type": "answer", "answer": "ok"}', tin=1000, tout=500))
        llm = R.ClaudeLLM("claude-sonnet-5-5", 2.0, 10.0, effort="medium", client=fake)
        out = llm.complete("system prompt", [{"role": "user", "content": "q"}], max_tokens=1024)
        self.assertEqual(out.text, '{"type": "answer", "answer": "ok"}')
        self.assertAlmostEqual(out.cost_usd, (1000 * 2.0 + 500 * 10.0) / 1_000_000)
        sent = fake.calls[0]
        self.assertEqual(sent["system"], "system prompt")
        self.assertEqual(sent["output_config"], {"effort": "medium"})
        self.assertEqual(sent["fallbacks"], "default")
        self.assertIn("server-side-fallback-2026-07-01", sent["betas"])
        self.assertGreaterEqual(sent["max_tokens"], 16000)          # room for adaptive thinking
        self.assertNotIn("temperature", sent)
        self.assertEqual(llm.served_models, {"claude-sonnet-5-5"})

    def test_a_refusal_becomes_an_escalation_and_a_fallback_shows_in_served_models(self):
        fake = FakeClaude(claude_response("", stop="refusal", model="claude-sonnet-5"))
        llm = R.ClaudeLLM("claude-sonnet-5-5", 2.0, 10.0, client=fake)
        out = llm.complete("s", [{"role": "user", "content": "q"}])
        self.assertEqual(json.loads(out.text)["type"], "escalate")
        self.assertEqual(llm.served_models, {"claude-sonnet-5"})
        self.assertNotIn("output_config", fake.calls[0])             # no effort set: the model's default

    def test_get_llm_needs_model_and_prices(self):
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "anthropic", "ANTHROPIC_MODEL": "",
                                          "ANTHROPIC_PRICE_IN_PER_MTOK": "2", "ANTHROPIC_PRICE_OUT_PER_MTOK": "10"}):
            with self.assertRaisesRegex(ValueError, "ANTHROPIC_MODEL"):
                R.get_llm()
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "anthropic", "ANTHROPIC_MODEL": "m",
                                          "ANTHROPIC_PRICE_IN_PER_MTOK": "", "ANTHROPIC_PRICE_OUT_PER_MTOK": "10"}):
            with self.assertRaisesRegex(ValueError, "ANTHROPIC_PRICE_IN_PER_MTOK"):
                R.get_llm()
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "ollama"}):
            with self.assertRaisesRegex(ValueError, "unknown LLM_PROVIDER"):
                R.get_llm()


class LocalProviderTests(unittest.TestCase):
    def test_json_mode_request_and_usage(self):
        sent = []

        def fake_open(req, timeout):
            sent.append(json.loads(req.data))
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": '{"type": "escalate"}'}}],
                                          "usage": {"prompt_tokens": 900, "completion_tokens": 40}}).encode())
        llm = R.LocalLLM("http://llm:8080/")
        with mock.patch("urllib.request.urlopen", fake_open):
            out = llm.complete("sys", [{"role": "user", "content": "q"}], max_tokens=4096)
        body = sent[0]
        self.assertEqual(body["messages"][0], {"role": "system", "content": "sys"})
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertLessEqual(body["max_tokens"], 768)
        self.assertEqual((out.input_tokens, out.output_tokens, out.cost_usd), (900, 40, 0.0))

    def test_server_failure_is_a_clear_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=ConnectionRefusedError()):
            with self.assertRaisesRegex(RuntimeError, "local model failed"):
                R.LocalLLM("http://llm:8080").complete("s", [])
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "local", "LOCAL_LLM_URL": "http://llm:8080"}):
            self.assertIsInstance(R.get_llm(), R.LocalLLM)


class EvalCaseTests(unittest.TestCase):
    def test_the_human_decision_lives_in_the_cases_not_the_fixtures(self):
        cases = {c.id: c for c in load_cases(ROOT / "evals" / "cases.json")}
        self.assertEqual(cases["lead-create-case-approved"].approval, "approve")
        self.assertEqual(cases["lead-create-case-rejected"].approval, "reject")
        self.assertEqual(cases["lead-create-case-approval-timeout"].approval, "timeout")
        self.assertIsNone(cases["lead-create-case-needs-approval"].approval)
        for f in (ROOT / "fixtures" / "llm").glob("*.json"):
            self.assertNotIn("approval", json.loads(f.read_text()), f.name)
        self.assertGreaterEqual(len(cases), 16)
        for name in ("baseline.json",):
            self.assertEqual(json.loads((ROOT / "evals" / name).read_text())["summary"]["cases"], len(cases))

    def test_the_eval_runner_never_reaches_a_database(self):
        source = (ROOT / "evals" / "run_evals.py").read_text()
        for unsafe in ("MEMORY_BACKEND", "MEMORY_DATABASE_URL", "DATABASE_URL", "GATEWAY_URL"):
            self.assertIn(f'"{unsafe}"', source)
        self.assertIn('os.environ["MEMORY_SQLITE"] = ":memory:"', source)
        compose = (ROOT / "docker-compose.yml").read_text()
        eval_service = compose[compose.index("\n  eval:"):compose.index("\n  a2a:")]
        for unsafe in ("DATABASE_URL", "MEMORY_", "GATEWAY_URL", "CLIENT_SECRET", "data]", "data,"):
            self.assertNotIn(unsafe, eval_service)


class MainBranchGuardTests(unittest.TestCase):
    """The wire tests write audit rows: against a gateway whose database is `main` they must refuse."""

    def test_wire_tests_skip_against_main_and_fail_when_required(self):
        import http.server
        import subprocess
        import sys
        import threading

        class Health(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = json.dumps({"status": "ok", "database": "main"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = http.server.HTTPServer(("127.0.0.1", 0), Health)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        env = {**os.environ, "GATEWAY_URL": f"http://127.0.0.1:{server.server_address[1]}",
               "PYTHONPATH": os.pathsep.join([str(ROOT.parents[1] / "agent-core"), str(ROOT)])}
        env.pop("REQUIRE_GATEWAY", None)
        run = lambda e: subprocess.run([sys.executable, "-m", "unittest", "-v", "tests.test_gateway_http"], cwd=ROOT,
                                       env=e, capture_output=True, text=True, timeout=120)
        relaxed = run(env)
        self.assertIn("OK (skipped=", relaxed.stderr.splitlines()[-1])
        self.assertIn("never run against the main branch", relaxed.stderr)
        strict = run({**env, "REQUIRE_GATEWAY": "1"})
        self.assertIn("FAILED", strict.stderr.splitlines()[-1])
        self.assertIn("main branch", strict.stderr)


class A2ACheckGuardTests(unittest.TestCase):
    def test_a2a_check_refuses_unless_the_gateway_database_is_test_or_local(self):
        from cs_copilot import a2a_check
        for database, expected in (("main", 2), ("unknown", 2), ("unreachable", 2)):
            with mock.patch.object(a2a_check, "gateway_database", return_value=database), \
                    mock.patch("sys.stderr", io.StringIO()) as err:
                self.assertEqual(a2a_check.main(), expected)
            self.assertIn("runs only on a test or local database", err.getvalue())


class A2ACardTests(unittest.TestCase):
    def test_card_is_public_safe_and_describes_the_skills(self):
        card = A.load_card("http://localhost:8701")
        raw = A.CARD_FILE.read_text()
        self.assertEqual(card.name, "cs-copilot")
        self.assertEqual({s.id for s in card.skills}, {"order-status", "billing-dispute", "open-case"})
        self.assertTrue(all(s.examples for s in card.skills))
        self.assertEqual(card.securitySchemes["keycloak"]["type"], "oauth2")
        for host in __import__("re").findall(r"https?://([^/:\"]+)", raw):
            self.assertEqual(host, "localhost")
        for word in ("secret", "password", "neon", "salesforce.com"):
            self.assertNotIn(word, raw.lower())


class A2AServerTests(unittest.TestCase):
    def test_authenticator_refuses_without_a_verified_token(self):
        class Verifier:
            def principal(self, token):
                return REP if token == "good" else None
        auth = A.make_authenticator(Verifier(), "http://gateway:8000/mcp")
        self.assertIsNone(auth(None))
        self.assertIsNone(auth("forged"))
        runner, principal = auth("good")
        self.assertIs(principal, REP)
        self.assertEqual(runner.token, "good")                       # passed through to the gateway

    def test_the_callers_token_reaches_the_gateway_source(self):
        built = []

        def builder(llm, source):
            built.append(source)
            return SimpleNamespace(run=lambda text, principal: Outcome("answered", "ok", 1, 0, 0.0, "t"))
        runner = A.CopilotForCaller("caller-token", "http://gateway:8000/mcp", llm_factory=lambda text: object(),
                                    builder=builder)
        self.assertEqual(runner.run("q", REP).status, "answered")
        self.assertEqual(built[0]._token(), "caller-token")

    def test_scripted_model_rejects_unknown_questions(self):
        runner = A.CopilotForCaller("t", "http://gateway:8000/mcp", llm_factory=lambda text: None)
        self.assertEqual(runner.run("what is the meaning of life", REP).status, "rejected")


if __name__ == "__main__":
    unittest.main()
