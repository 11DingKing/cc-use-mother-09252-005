"""端到端冒烟：真实 HTTP  socket 往返。"""
import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from service_09252_005.api import make_http_handler

import support


class ApiSmokeTests(unittest.TestCase):
    def setUp(self):
        self.app = support.make_app(self)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_http_handler(self.app.router))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    def call(self, method, path, body=None, headers=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health_and_roundtrip(self):
        status, body = self.call("GET", "/health")
        self.assertEqual((status, body["status"]), (200, "ok"))
        admin = {"X-Actor-Role": "admin", "X-Actor-Id": "root"}
        status, body = self.call("POST", "/api/v1/enterprises",
                                 {"id": "ent1", "name": "智造股份", "region": "苏州"}, admin)
        self.assertEqual(status, 201)
        status, body = self.call("POST", "/api/v1/commitments/import", {
            "enterprise_id": "ent1", "quarter": "2026Q1",
            "positions": [{"position_key": "p1", "title": "焊工", "region": "苏州",
                           "skills": ["weld"], "capacity": 2}]}, admin)
        self.assertEqual(status, 200)
        self.assertEqual(body["positions"][0]["capacity"], 2)
        status, body = self.call("GET", "/api/v1/ledger/version", headers=admin)
        self.assertEqual(status, 200)
        self.assertGreater(body["version"], 0)

    def test_invalid_json_and_unknown_route(self):
        admin = {"X-Actor-Role": "admin", "X-Actor-Id": "root"}
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/v1/enterprises",
            data=b"{not json", method="POST")
        req.add_header("X-Actor-Role", "admin")
        req.add_header("X-Actor-Id", "root")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        self.assertEqual(status, 422)
        status, _ = self.call("GET", "/api/v1/nope", headers=admin)
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
