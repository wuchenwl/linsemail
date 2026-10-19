"""Linsemail source-catalog HTTP API, Python 3.10+, standard library only."""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parent
FILTERS = {"category", "api", "auth", "account_required", "probe_ok", "q", "limit", "offset"}


class QueryError(ValueError):
    pass


class Catalog:
    def __init__(self, path=None):
        self.data = json.loads(Path(path or ROOT / "data/providers.json").read_text(encoding="utf-8"))
        self.by_id = {provider["id"]: provider for provider in self.data["providers"]}
        if len(self.by_id) != len(self.data["providers"]):
            raise ValueError("Duplicate provider id")

    def providers(self, query):
        unknown = set(query) - FILTERS
        if unknown:
            raise QueryError("Unknown query fields: " + ", ".join(sorted(unknown)))
        if any(len(values) != 1 for values in query.values()):
            raise QueryError("Each query field must appear once")
        params = {key: value[0] for key, value in query.items()}
        values = self.data["providers"]
        for field, get_value in (
            ("category", lambda p: p["category"]),
            ("api", lambda p: p["api"]["availability"]),
            ("auth", lambda p: p["api"]["auth"]),
        ):
            if field in params:
                allowed = {get_value(p) for p in self.data["providers"]}
                if params[field] not in allowed:
                    raise QueryError(f"Invalid {field}; choose from: " + ", ".join(sorted(allowed)))
                values = [p for p in values if get_value(p) == params[field]]
        for field in ("account_required", "probe_ok"):
            if field in params:
                if params[field] not in ("true", "false"):
                    raise QueryError(f"{field} must be true or false")
                expected = params[field] == "true"
                if field == "account_required":
                    values = [p for p in values if p["api"]["account_required"] is expected]
                else:
                    values = [p for p in values if bool((p["latest_probe"] or {}).get("metadata_probe_ok")) is expected]
        if "q" in params:
            needle = params["q"].casefold()
            values = [p for p in values if needle in json.dumps(p, ensure_ascii=False).casefold()]
        try:
            limit, offset = int(params.get("limit", "100")), int(params.get("offset", "0"))
        except ValueError as error:
            raise QueryError("limit and offset must be integers") from error
        if not 1 <= limit <= 100 or offset < 0:
            raise QueryError("limit must be 1..100 and offset must be >= 0")
        return {"total": len(values), "limit": limit, "offset": offset, "stage": "collection",
                "items": values[offset:offset + limit]}

    def stats(self):
        return {**self.data["stats"], "generated_at": self.data["generated_at"], "stage": self.data["stage"]}


def make_handler(catalog):
    class Handler(BaseHTTPRequestHandler):
        server_version = "LinsemailCatalog/0.1"

        def respond(self, status, value, head=False):
            body = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def do_HEAD(self):
            self.do_GET(head=True)

        def do_GET(self, head=False):
            target = urlsplit(self.path)
            path = unquote(target.path).rstrip("/") or "/"
            if len(target.query) > 4096:
                return self.respond(414, {"error": "query_too_long"}, head)
            try:
                query = parse_qs(target.query, keep_blank_values=True, max_num_fields=20)
                if path not in ("/api/providers",) and query:
                    raise QueryError("This endpoint does not accept query parameters")
                if path == "/":
                    result = {"service": "linsemail", "stage": "collection", "api_kind": "provider_catalog",
                              "endpoints": ["/health", "/api/providers", "/api/providers/{id}", "/api/stats", "/api/shortlist", "/openapi.json"],
                              "mailbox_adapters_implemented": False}
                elif path == "/health":
                    result = {"status": "ok", "providers": len(catalog.by_id)}
                elif path == "/api/providers":
                    result = catalog.providers(query)
                elif path == "/api/stats":
                    result = catalog.stats()
                elif path == "/api/shortlist":
                    result = {"stage": "collection", "items": sorted((p for p in catalog.by_id.values() if p["integration"]["priority"] is not None), key=lambda p: p["integration"]["priority"])}
                elif path == "/openapi.json":
                    result = json.loads((ROOT / "openapi.json").read_text(encoding="utf-8"))
                elif re.fullmatch(r"/api/providers/[a-z0-9_]+", path):
                    result = catalog.by_id.get(path.rsplit("/", 1)[1])
                    if result is None:
                        return self.respond(404, {"error": "provider_not_found"}, head)
                else:
                    return self.respond(404, {"error": "not_found"}, head)
                return self.respond(200, result, head)
            except (ValueError, QueryError) as error:
                return self.respond(400, {"error": "invalid_query", "detail": str(error)}, head)

        def do_POST(self):
            self.send_response(405)
            body = b'{"error":"read_only_catalog","stage":"collection"}\n'
            self.send_header("Allow", "GET, HEAD")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    catalog = Catalog()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(catalog))
    print(f"Linsemail catalog: http://{args.host}:{server.server_port} ({len(catalog.by_id)} providers)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
