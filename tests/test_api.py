from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import Catalog, make_handler


class CatalogApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog()
        cls.expected = json.loads((ROOT / "tests/catalog_expectations.json").read_text(encoding="utf-8"))
        cls.tasklet_attempts = {a["provider"]: a for a in json.loads((ROOT / "tests/fixtures/catalog-attempts.json").read_text(encoding="utf-8"))["attempts"]}
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.catalog))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, path, method="GET"):
        request = Request(self.base + path, data=b"{}" if method == "POST" else None, method=method)
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            body = response.read()
            return response.status, json.loads(body) if body else None, response.headers

    def test_collection_counts_and_evidence_bound_signup_claims(self):
        status, data, headers = self.request("/api/providers")
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], self.expected["provider_count"])
        self.assertEqual(len(data["items"]), min(100, self.expected["provider_count"]))
        for offset in range(len(data["items"]), data["total"], 100):
            page_status, page, _ = self.request("/api/providers?limit=100&offset=" + str(offset))
            self.assertEqual(page_status, 200)
            self.assertEqual(page["total"], data["total"])
            data["items"].extend(page["items"])
        self.assertEqual(len(data["items"]), self.expected["provider_count"])
        self.assertEqual(len({p["id"] for p in data["items"]}), self.expected["provider_count"])
        self.assertIn("utf-8", headers["Content-Type"])
        ids = {p["id"] for p in data["items"]}
        self.assertTrue(set(self.expected["round2_provider_ids"]) <= ids)
        self.assertTrue(set(self.expected["excluded_provider_ids"]).isdisjoint(ids))
        for provider in data["items"]:
            self.assertEqual(provider["integration"]["adapter_implemented"], provider["id"] in set(self.expected["implemented_adapter_ids"]))
            attempt = self.tasklet_attempts.get(provider["id"])
            if attempt:
                self.assertTrue(provider["mailbox"]["delivery_tested"])
                for field in ("status", "email_accepted", "ui_send_acknowledged", "verification_received", "registration_succeeded"):
                    self.assertEqual(provider["tasklet_ai"][field], attempt[field], (provider["id"], field))
                self.assertNotIn("token", json.dumps(provider["tasklet_ai"]))
                self.assertNotIn("password", json.dumps(provider["tasklet_ai"]))
            else:
                self.assertFalse(provider["mailbox"]["delivery_tested"])
                self.assertEqual(provider["tasklet_ai"]["status"], "not_tested")

    def test_documented_no_account_filter_excludes_unknown(self):
        status, data, _ = self.request("/api/providers?api=documented&account_required=false&category=disposable")
        self.assertEqual(status, 200)
        ids = {p["id"] for p in data["items"]}
        self.assertEqual(ids, set(self.expected["documented_no_account_disposable_ids"]))
        self.assertNotIn("1secmail", ids)
        self.assertNotIn("mailcx", ids)
        self.assertNotIn("mailisk", ids)

    def test_verified_filter_is_metadata_only(self):
        status, data, _ = self.request("/api/providers?probe_ok=true")
        self.assertEqual(status, 200)
        self.assertEqual({p["id"] for p in data["items"]}, set(self.expected["metadata_probe_ok_ids"]))
        # Domain/ping success must never be promoted to successful delivery.
        self.assertTrue(all(p["mailbox"]["delivery_observed"] is not True for p in data["items"]
                            if not p["tasklet_ai"]["verification_received"]))

    def test_pagination_and_exact_provider(self):
        _, first, _ = self.request("/api/providers?limit=2&offset=0")
        _, second, _ = self.request("/api/providers?limit=2&offset=2")
        self.assertEqual(first["total"], self.expected["provider_count"])
        self.assertTrue({p["id"] for p in first["items"]}.isdisjoint(p["id"] for p in second["items"]))
        status, provider, _ = self.request("/api/providers/inboxes")
        self.assertEqual(status, 200)
        self.assertIn("getnada", provider["aliases"])

    def test_invalid_queries_do_not_silently_broaden_selection(self):
        paths = ["/api/providers?account_required=no", "/api/providers?category=invalid", "/api/providers?api=ready",
                 "/api/providers?limit=-1", "/api/providers?offset=-2", "/api/providers?limit=x",
                 "/api/providers?auth=none&auth=api_key", "/api/providers?unknown=1", "/api/stats?q=x",
                 "/api/providers?" + "&".join(f"x{i}=1" for i in range(21))]
        for path in paths:
            with self.subTest(path=path):
                status, error, _ = self.request(path)
                self.assertEqual(status, 400)
                self.assertEqual(error["error"], "invalid_query")

    def test_missing_paths_and_write_requests(self):
        for path in ("/api/providers/missing", "/api/providers/../../app.py", "/api/mailboxes"):
            self.assertEqual(self.request(path)[0], 404)
        status, result, headers = self.request("/api/mailboxes", "POST")
        self.assertEqual(status, 405)
        self.assertEqual(result["error"], "read_only_catalog")
        self.assertEqual(headers["Allow"], "GET, HEAD")

    def test_api_description_matches_served_routes(self):
        status, schema, _ = self.request("/openapi.json")
        self.assertEqual(status, 200)
        self.assertEqual(schema["openapi"], "3.1.0")
        for path in schema["paths"]:
            if path.startswith(("/api/mailbox", "/api/tasklet")):
                continue
            concrete = path.replace("{provider_id}", "grabmail")
            self.assertEqual(self.request(concrete)[0], 200, path)
        self.assertEqual(self.request("/health", "HEAD")[0], 200)

    def test_catalog_source_provenance_has_no_fabricated_verification(self):
        for provider in self.catalog.by_id.values():
            self.assertTrue(provider["sources"])
            if provider["api"]["availability"] == "documented":
                self.assertTrue(provider["api"]["documentation_verified"])
            if provider["api"]["availability"] == "not_found":
                self.assertEqual(provider["api"]["operations"], [])
            if provider["category"] == "forwarding_alias":
                self.assertEqual(provider["integration"]["route"], "forwarding_only")
            if provider["category"] == "self_hosted":
                self.assertEqual(provider["integration"]["route"], "self_host_required")
        _, stats, _ = self.request("/api/stats")
        self.assertEqual(sum(stats["categories"].values()), stats["providers"])
        self.assertEqual(stats["metadata_probes_ok"], len(self.expected["metadata_probe_ok_ids"]))
        self.assertEqual(stats["tasklet_registrations_tested"], len(self.tasklet_attempts))
        self.assertEqual(stats["tasklet_registrations_completed"], sum(a["registration_succeeded"] is True for a in self.tasklet_attempts.values()))


if __name__ == "__main__":
    unittest.main()
