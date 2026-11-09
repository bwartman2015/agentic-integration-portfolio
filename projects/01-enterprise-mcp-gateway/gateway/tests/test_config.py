"""Settings: seed by default, fail closed on half-configured live backends, secrets never printable."""
from __future__ import annotations

import unittest

from ridgeline_gateway.config import ConfigError, Secret, load_settings

SF_SECRET = "sf-secret-value-do-not-print-0001"
SAP_KEY = "sap-key-value-do-not-print-0002"


def env(**kw: str) -> dict[str, str]:
    return {k: v for k, v in kw.items()}


class ConfigTests(unittest.TestCase):
    def test_defaults_are_offline_seed_backends(self):
        s = load_settings({})
        self.assertEqual(s.backends(), {"crm": "seed", "erp": "seed", "carrier": "seed", "ar": "seed"})

    def test_unknown_backend_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "CRM_BACKEND must be one of"):
            load_settings(env(CRM_BACKEND="dynamics"))

    def test_salesforce_without_credentials_fails_closed_naming_variables_only(self):
        with self.assertRaises(ConfigError) as ctx:
            load_settings(env(CRM_BACKEND="salesforce", SF_CLIENT_SECRET=SF_SECRET))
        msg = str(ctx.exception)
        self.assertIn("SF_CLIENT_ID is required", msg)
        self.assertIn("SF_LOGIN_URL must be", msg)
        self.assertNotIn(SF_SECRET, msg)

    def test_salesforce_login_url_must_be_https_salesforce_domain(self):
        base = env(CRM_BACKEND="salesforce", SF_CLIENT_ID="id-123", SF_CLIENT_SECRET=SF_SECRET)
        for bad in ("http://acme.my.salesforce.com", "https://evil.example", "https://my.salesforce.com.evil.example",
                    "https://login.salesforce.com", "https://acme.my.salesforce.com:8443",
                    "https://user:pw" + "@" + "acme.my.salesforce.com",  # userinfo in the URL
                    "https://.my.salesforce.com"):
            with self.assertRaises(ConfigError, msg=bad):
                load_settings({**base, "SF_LOGIN_URL": bad})
        ok = load_settings({**base, "SF_LOGIN_URL": "https://ridgeline-dev-ed.develop.my.salesforce.com/"})
        self.assertEqual(ok.sf_login_url, "https://ridgeline-dev-ed.develop.my.salesforce.com")

    def test_salesforce_api_version_format(self):
        base = env(CRM_BACKEND="salesforce", SF_CLIENT_ID="id-123", SF_CLIENT_SECRET=SF_SECRET,
                   SF_LOGIN_URL="https://acme.my.salesforce.com")
        for bad in ("v6..2", "62.0", "v62", "v62.1"):
            with self.assertRaises(ConfigError, msg=bad):
                load_settings({**base, "SF_API_VERSION": bad})
        self.assertEqual(load_settings({**base, "SF_API_VERSION": "v64.0"}).sf_api_version, "v64.0")

    def test_sap_requires_key_and_sap_host(self):
        with self.assertRaisesRegex(ConfigError, "SAP_API_KEY is required"):
            load_settings(env(ERP_BACKEND="sap_sandbox"))
        with self.assertRaisesRegex(ConfigError, "SAP_BASE_URL must be"):
            load_settings(env(ERP_BACKEND="sap_sandbox", SAP_API_KEY=SAP_KEY, SAP_BASE_URL="https://sap.evil.example/x"))
        self.assertEqual(load_settings(env(ERP_BACKEND="sap_sandbox", SAP_API_KEY=SAP_KEY)).erp_backend, "sap_sandbox")

    def test_http_mock_backends_need_strong_shared_secrets(self):
        with self.assertRaisesRegex(ConfigError, "CARRIER_API_KEY must be set"):
            load_settings(env(CARRIER_BACKEND="http", CARRIER_API_KEY="short"))
        with self.assertRaisesRegex(ConfigError, "AR_API_TOKEN must be set"):
            load_settings(env(AR_BACKEND="http"))

    def test_bad_timeout_is_rejected(self):
        for bad in ("abc", "0", "120"):
            with self.assertRaises(ConfigError, msg=bad):
                load_settings(env(HTTP_TIMEOUT_S=bad))

    def test_secrets_never_render(self):
        s = load_settings(env(CRM_BACKEND="salesforce", SF_LOGIN_URL="https://acme.my.salesforce.com",
                              SF_CLIENT_ID="client-id-visible-nowhere", SF_CLIENT_SECRET=SF_SECRET,
                              ERP_BACKEND="sap_sandbox", SAP_API_KEY=SAP_KEY))
        for text in (repr(s), str(s), repr(s.sf_client_secret), f"{s.sap_api_key}"):
            self.assertNotIn(SF_SECRET, text)
            self.assertNotIn(SAP_KEY, text)
            self.assertNotIn("client-id-visible-nowhere", text)
        self.assertEqual(s.sf_client_secret.reveal(), SF_SECRET)
        self.assertFalse(Secret(""))


if __name__ == "__main__":
    unittest.main()
