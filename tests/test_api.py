import io
import json
import socket
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from lmh.api import make_server


CATALOG = {"schema_version": 1, "models": {}, "runtimes": {}, "artifacts": {}}
ACTION = {"action": "start", "model": "m", "profile": "p"}
ACTIONS = "/api/actions"
WEBHOOK = "/api/webhooks/action"
PREVIEW = "/api/catalog/preview"
STAGE = "/api/catalog/stage"


class Controller:
    def __init__(self):
        self.submissions = []
        self.staged = []

    def status(self):
        return {"running": True}

    def logs(self, model, profile):
        return [f"{model}/{profile}"]

    def stats(self):
        return {"runs": 3}

    def submit(self, action, model, profile, from_model=None, from_profile=None, source="panel"):
        self.submissions.append((action, model, profile, from_model, from_profile, source))
        return {"id": 1}

    def stage_catalog(self, raw):
        self.staged.append(raw)
        return {"state": "staged"}


@contextmanager
def running_server(controller, **kwargs):
    server = make_server(controller, port=0, **kwargs)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    worker.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        worker.join()
        server.server_close()


def request(port, method, path, body=None, headers=None):
    headers = dict(headers or {})
    if body is not None and not isinstance(body, bytes):
        body = json.dumps(body).encode()
    if body is not None:
        headers.setdefault("Content-Type", "application/json")
    connection = HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        data = response.read()
        if data and response.getheader("Content-Type", "").startswith("application/json"):
            data = json.loads(data)
        return response.status, response.headers, data
    finally:
        connection.close()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


class APITest(unittest.TestCase):
    def test_health_and_controller_routes(self):
        controller = Controller()
        with running_server(controller) as port:
            for path, expected in (
                ("/api/health", {"ok": True}),
                ("/api/status", {"running": True}),
                ("/api/stats", {"runs": 3}),
                ("/api/logs?model=m&profile=p", {"lines": ["m/p"]}),
            ):
                with self.subTest(path=path):
                    code, _, data = request(port, "GET", path)
                    self.assertEqual((code, data), (200, expected))
            code, _, data = request(port, "POST", ACTIONS, ACTION)
            self.assertEqual((code, data), (202, {"id": 1}))
        self.assertEqual(controller.submissions, [("start", "m", "p", None, None, "panel")])

    def test_preview_is_read_only_and_stage_accepts_only_valid_inline_catalog(self):
        controller = Controller()
        with running_server(controller) as port:
            code, _, data = request(port, "POST", PREVIEW, {"catalog": CATALOG})
            self.assertEqual((code, data["catalog"]), (200, {**CATALOG, "boot": []}))
            self.assertEqual(controller.staged, [])
            for body in ({"url": "https://catalog.example.com/models.json"}, {"catalog": {}}):
                self.assertEqual(request(port, "POST", STAGE, body)[0], 400)
            self.assertEqual(controller.staged, [])
            code, _, data = request(port, "POST", STAGE, {"catalog": CATALOG})
            self.assertEqual((code, data), (200, {"state": "staged"}))
        self.assertEqual(controller.staged, [CATALOG])
        self.assertEqual(controller.submissions, [])

    def test_local_mutations_require_local_host_and_allow_only_same_origin(self):
        controller = Controller()
        with running_server(controller) as port:
            local = f"127.0.0.1:{port}"
            untrusted = f"untrusted.example:{port}"
            for headers in (
                {"Host": untrusted, "Origin": f"http://{untrusted}"},
                {"Host": untrusted},
                {"Origin": f"http://{untrusted}"},
                {"Origin": f"http://localhost:{port}"},
                {"Origin": "null"},
                {"Sec-Fetch-Site": "cross-site"},
            ):
                with self.subTest(headers=headers):
                    code, response_headers, _ = request(port, "POST", ACTIONS, ACTION, headers)
                    self.assertEqual(code, 403)
                    self.assertNotIn("Access-Control-Allow-Origin", response_headers)
            self.assertEqual(controller.submissions, [])
            self.assertEqual(request(
                port, "POST", ACTIONS, ACTION, {"Origin": f"http://{local}", "Sec-Fetch-Site": "same-origin"},
            )[0], 202)
            self.assertEqual(request(
                port, "POST", STAGE, {"catalog": CATALOG}, {"Origin": "null"},
            )[0], 403)
            code, headers, _ = request(port, "OPTIONS", ACTIONS, headers={
                "Origin": f"http://{untrusted}", "Access-Control-Request-Method": "POST",
            })
            self.assertEqual(code, 405)
            self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_remote_binding_requires_operator_token_for_control_and_reads(self):
        controller = Controller()
        with self.assertRaises(ValueError):
            make_server(controller, host="0.0.0.0", port=0)
        with running_server(controller, host="0.0.0.0", api_token="operator-secret") as port:
            self.assertEqual(request(port, "GET", "/api/health")[0], 200)
            for method, path, body, expected in (
                ("GET", "/api/status", None, 200),
                ("GET", "/api/stats", None, 200),
                ("GET", "/api/logs?model=m&profile=p", None, 200),
                ("POST", ACTIONS, ACTION, 202),
                ("POST", PREVIEW, {"catalog": CATALOG}, 200),
                ("POST", STAGE, {"catalog": CATALOG}, 200),
            ):
                with self.subTest(path=path):
                    self.assertEqual(request(port, method, path, body)[0], 401)
                    self.assertEqual(request(port, method, path, body, bearer("wrong"))[0], 401)
                    self.assertEqual(
                        request(port, method, path, body, bearer("operator-secret"))[0], expected
                    )
        self.assertEqual(len(controller.submissions), 1)
        self.assertEqual(len(controller.staged), 1)

    def test_authenticated_https_proxy_origin_checks_exact_authority(self):
        controller = Controller()
        with running_server(controller, host="0.0.0.0", api_token="operator-secret") as port:
            headers = {"Host": "panel.example", "Sec-Fetch-Site": "same-origin"}
            for origin in ("https://evil.example", "https://panel.example:8443"):
                self.assertEqual(request(port, "POST", ACTIONS, ACTION, {
                    **headers, **bearer("operator-secret"), "Origin": origin})[0], 403)
            self.assertEqual(request(port, "POST", ACTIONS, ACTION, {
                **headers, "Origin": "https://panel.example"})[0], 401)
            self.assertEqual(request(port, "POST", ACTIONS, ACTION, {
                **headers, **bearer("operator-secret"), "Origin": "https://panel.example"})[0], 202)

    def test_webhook_tokens_are_endpoint_specific_and_switch_checks_both_models(self):
        controller = Controller()
        scopes = {"scheduler": {"token": "hook-secret", "actions": ["start", "switch"], "models": ["m", "n"]}}
        hook = bearer("hook-secret")
        switch = {**ACTION, "action": "switch", "from_model": "n", "from_profile": "q"}
        with self.assertRaises(ValueError):
            make_server(controller, port=0, api_token="hook-secret", webhooks=scopes)
        with running_server(controller, api_token="operator-secret", webhooks=scopes, role="webhook") as port:
            self.assertEqual(request(port, "POST", WEBHOOK, ACTION)[0], 401)
            self.assertEqual(request(port, "POST", WEBHOOK, ACTION, bearer("operator-secret"))[0], 401)
            self.assertEqual(request(port, "POST", ACTIONS, ACTION, hook)[0], 404)
            self.assertEqual(request(port, "POST", STAGE, {"catalog": CATALOG}, hook)[0], 404)
            self.assertEqual(request(port, "GET", "/v1/models", headers=hook)[0], 404)
            for body in (
                {**ACTION, "action": "stop"},
                {**ACTION, "model": "other"},
                {**switch, "model": "other"},
                {**switch, "from_model": "other"},
                {**ACTION, "action": "switch"},
            ):
                with self.subTest(body=body):
                    self.assertEqual(request(port, "POST", WEBHOOK, body, hook)[0], 403)
            self.assertEqual(controller.submissions, [])
            self.assertEqual(request(port, "POST", WEBHOOK, switch, hook)[0], 202)
        with running_server(controller, api_token="operator-secret") as port:
            self.assertEqual(request(port, "POST", WEBHOOK, ACTION, hook)[0], 404)
            self.assertEqual(request(port, "GET", "/v1/models")[0], 404)
            self.assertEqual(request(port, "POST", ACTIONS, ACTION, hook)[0], 401)
        self.assertEqual(controller.submissions, [("switch", "m", "p", "n", "q", "scheduler")])

    def test_json_validation_and_size_limits_apply_before_submission(self):
        controller = Controller()
        with running_server(controller) as port:
            for body in (
                [], {"action": []}, {**ACTION, "profile": ""},
                {**ACTION, "source": "scheduler"}, {**ACTION, "from_model": "m"},
                b'{"action":"start","action":"stop","model":"m","profile":"p"}',
                b'{"action":"start","model":"\xff","profile":"p"}',
                b'{"action":"start","model":"m","profile":NaN}',
            ):
                with self.subTest(body=body):
                    self.assertEqual(request(port, "POST", ACTIONS, body)[0], 400)
            self.assertEqual(request(port, "POST", ACTIONS, ACTION, {"Content-Type": "text/plain"})[0], 415)
            self.assertEqual(request(port, "POST", ACTIONS, b" " * 17000)[0], 413)
            # An advertised oversized body is rejected before waiting for the bytes.
            self.assertEqual(request(port, "POST", PREVIEW, b"", {"Content-Length": "1048577"})[0], 413)
            self.assertEqual(request(port, "POST", ACTIONS, b"{}", {"Transfer-Encoding": "chunked"})[0], 400)
            self.assertEqual(request(port, "GET", "/api/logs?model=m&model=n&profile=p")[0], 400)
        self.assertEqual(controller.submissions, [])

    def test_maps_controller_input_errors_and_conflicts(self):
        controller = Controller()
        with running_server(controller) as port:
            for error, code in ((KeyError("missing"), 400), (ValueError("invalid"), 400), (RuntimeError("busy"), 409)):
                with self.subTest(error=error), patch.object(controller, "submit", side_effect=error):
                    status, _, body = request(port, "POST", ACTIONS, ACTION)
                    self.assertEqual(status, code)
                    self.assertIn(error.args[0], body["error"])

    def test_static_ui_is_confined_to_root_and_does_not_intercept_api_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            content = Path(directory)
            ui = content / "ui"
            ui.mkdir()
            (content / "secret.txt").write_text("private")
            (ui / "index.html").write_text("<h1>Panel</h1>")
            (ui / "app.js").write_text("export {};")
            (ui / "symlink.txt").symlink_to(content / "secret.txt")
            with running_server(Controller(), ui_dir=ui) as port:
                self.assertEqual(request(port, "GET", "/")[2], b"<h1>Panel</h1>")
                self.assertEqual(request(port, "GET", "/app.js?v=1")[2], b"export {};")
                self.assertEqual(request(port, "HEAD", "/app.js")[2], b"")
                self.assertEqual(request(port, "GET", "/models", headers={"Accept": "text/html"})[2], b"<h1>Panel</h1>")
                for path in ("/missing.js", "/%2e%2e/secret.txt", "/symlink.txt", "/api/unknown"):
                    with self.subTest(path=path):
                        self.assertEqual(request(port, "GET", path, headers={"Accept": "text/html"})[0], 404)

    def test_catalog_fetch_pins_the_dns_answer_and_verifies_tls_for_the_hostname(self):
        public = ("93.184.216.34", 443)
        payload = json.dumps(CATALOG).encode()
        response = (
            b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload
        )
        with self.mock_catalog_network(response, [public]) as (dns, raw, context, tls_socket):
            with running_server(Controller()) as port:
                code, _, data = request(port, "POST", PREVIEW, {"url": "https://catalog.example.com/list?q=1"})
            self.assertEqual((code, data["catalog"]), (200, {**CATALOG, "boot": []}))
            dns.assert_called_once_with("catalog.example.com.", 443, type=socket.SOCK_STREAM)
            raw.connect.assert_called_once_with(public)
            context.wrap_socket.assert_called_once_with(raw, server_hostname="catalog.example.com")
            sent = tls_socket.sendall.call_args.args[0]
            self.assertIn(b"GET /list?q=1 HTTP/1.1\r\n", sent)
            self.assertIn(b"Host: catalog.example.com\r\n", sent)

    def test_catalog_fetch_rejects_nonpublic_dns_and_does_not_follow_redirects(self):
        with self.mock_catalog_network(b"", [("93.184.216.34", 443), ("127.0.0.1", 443)]) as (dns, raw, _, _):
            with running_server(Controller()) as port:
                for url in (
                    "http://catalog.example.com/",
                    "https://127.0.0.1/",
                    "https://[::1]/",
                    "https://169.254.169.254/",
                    "https://catalog.example.com:8443/",
                    "https://user@catalog.example.com/",
                ):
                    with self.subTest(url=url):
                        self.assertEqual(request(port, "POST", PREVIEW, {"url": url})[0], 400)
                dns.assert_not_called()
                self.assertEqual(request(port, "POST", PREVIEW, {"url": "https://catalog.example.com/"})[0], 400)
                raw.connect.assert_not_called()
        response = b"HTTP/1.1 302 Found\r\nLocation: https://other.example.com/\r\nContent-Length: 0\r\n\r\n"
        with self.mock_catalog_network(response, [("93.184.216.34", 443)]) as (dns, raw, _, _):
            with running_server(Controller()) as port:
                code, _, data = request(port, "POST", PREVIEW, {"url": "https://catalog.example.com/"})
            self.assertEqual(code, 400)
            self.assertIn("redirect", data["error"])
            dns.assert_called_once()
            raw.connect.assert_called_once()

    def test_catalog_fetch_caps_declared_and_streamed_response_sizes(self):
        for response in (
            b"HTTP/1.1 200 OK\r\nContent-Length: 1048577\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + b" " * 1048577,
        ):
            with self.mock_catalog_network(response, [("93.184.216.34", 443)]):
                with running_server(Controller()) as port:
                    code, _, _ = request(port, "POST", PREVIEW, {"url": "https://catalog.example.com/"})
            self.assertEqual(code, 413)

    @staticmethod
    @contextmanager
    def mock_catalog_network(response, addresses):
        # Keep the test server/client sockets real and fake only the outbound TLS boundary.
        dns = Mock(return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", address)
            for address in addresses
        ])
        raw = MagicMock()
        raw.__enter__.return_value = raw
        tls_socket = Mock()
        tls_socket.makefile.return_value = io.BytesIO(response)
        context = Mock()
        context.wrap_socket.return_value = tls_socket
        network = SimpleNamespace(
            getaddrinfo=dns, socket=Mock(return_value=raw),
            AF_INET=socket.AF_INET, AF_INET6=socket.AF_INET6, SOCK_STREAM=socket.SOCK_STREAM,
        )
        with patch("lmh.api.socket", network), patch("lmh.api.ssl.create_default_context", return_value=context):
            yield dns, raw, context, tls_socket
