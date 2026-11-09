"""Mock carrier and AR APIs over real HTTP (uvicorn in a thread, stdlib client). Run in the image's
test stage: `make gateway-test P=01` builds and runs it alongside the gateway tests."""
from __future__ import annotations

import json
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from typing import Any

import uvicorn

from mock_systems import get_systems
from ridgeline_mock_apis import ar, carrier

CARRIER_KEY = "test-carrier-key-0123456789"
AR_TOKEN = "test-ar-token-0123456789abcdef"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    def __init__(self, app: Any):
        self.port = _free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "_Server":
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.02)
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)

    def get(self, path: str, headers: dict[str, str] | None = None) -> tuple[int, Any]:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            body = err.read()
            err.close()
            return err.code, json.loads(body) if body else None


class CarrierApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.srv = _Server(carrier.create_app(CARRIER_KEY)).__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.__exit__(None, None, None)

    def test_tracking_matches_seeded_carrier(self):
        status, body = self.srv.get("/v1/orders/SO-104233/tracking", {"X-API-Key": CARRIER_KEY})
        self.assertEqual(status, 200)
        self.assertEqual(body, get_systems().carrier.tracking("SO-104233"))

    def test_missing_or_wrong_key_is_401(self):
        for headers in ({}, {"X-API-Key": "wrong-key-0123456789"}, {"X-API-Key": CARRIER_KEY[:-1]}):
            status, _ = self.srv.get("/v1/orders/SO-104233/tracking", headers)
            self.assertEqual(status, 401, headers)

    def test_unknown_order_is_404_and_bad_id_is_422(self):
        self.assertEqual(self.srv.get("/v1/orders/SO-999999/tracking", {"X-API-Key": CARRIER_KEY})[0], 404)
        self.assertEqual(self.srv.get("/v1/orders/104233/tracking", {"X-API-Key": CARRIER_KEY})[0], 422)

    def test_no_docs_or_schema_endpoints(self):
        for path in ("/docs", "/redoc", "/openapi.json"):
            self.assertEqual(self.srv.get(path)[0], 404, path)

    def test_refuses_to_start_without_a_strong_key(self):
        for weak in ("", "short"):
            with self.assertRaises(RuntimeError):
                carrier.create_app(weak)


class ArApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.srv = _Server(ar.create_app(AR_TOKEN)).__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.__exit__(None, None, None)

    def test_open_items_match_seeded_ar(self):
        status, body = self.srv.get("/v1/accounts/ACCT-1002/open-items", {"Authorization": f"Bearer {AR_TOKEN}"})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"account_id": "ACCT-1002", "invoices": get_systems().ar.open_items("ACCT-1002")})

    def test_missing_wrong_or_non_bearer_token_is_401(self):
        for headers in ({}, {"Authorization": "Bearer nope-nope-nope-nope"}, {"Authorization": f"Basic {AR_TOKEN}"},
                        {"Authorization": AR_TOKEN}):
            status, _ = self.srv.get("/v1/accounts/ACCT-1002/open-items", headers)
            self.assertEqual(status, 401, headers)

    def test_unknown_account_is_404(self):
        status, _ = self.srv.get("/v1/accounts/ACCT-9999/open-items", {"Authorization": f"Bearer {AR_TOKEN}"})
        self.assertEqual(status, 404)

    def test_refuses_to_start_without_a_strong_token(self):
        with self.assertRaises(RuntimeError):
            ar.create_app("short")


if __name__ == "__main__":
    unittest.main()
