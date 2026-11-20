import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

from lmh.api import make_server
from lmh.controller import Controller
from lmh.inference import encode_upload, parse_upload
from lmh.language_stats import count_profanity


class Engine(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, data, media="application/json"):
        data = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/v1/models":
            self.reply({"data": [{"id": "engine-alias"}]})
        elif self.path == "/v1/videos/engine-job/content":
            self.reply(b"\x00mp4", "video/mp4")
        elif self.path == "/v1/videos/engine-job":
            self.reply({"id": "engine-job", "model": "engine-alias", "status": "completed"})
        else:
            self.reply({"ok": True})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.last_request = self.path, raw, self.headers
        if self.path == "/v1/chat/completions":
            if json.loads(raw).get("stream"):
                self.reply(
                    b'data: {"model":"engine-alias","choices":[{"index":0,"delta":{"content":"F.U."}}]}\n\n'
                    b'data: {"choices":[{"index":0,"delta":{"content":"C.K"}}]}\n\ndata: [DONE]\n\n',
                    "text/event-stream",
                )
            else:
                self.reply({"model": "engine-alias", "choices": [{"message": {"content": "Damn, hello!"}}]})
        elif self.path == "/v1/embeddings":
            self.reply({"model": "engine-alias", "data": [{"embedding": [1.0]}]})
        elif self.path == "/v1/videos":
            self.reply({"id": "engine-job", "model": "engine-alias", "status": "queued"})
        elif self.path == "/v1/audio/transcriptions":
            self.reply({"text": "hello"})
        else:
            self.reply({"data": [{"b64_json": "AA=="}]})


class InferenceTests(unittest.TestCase):
    def spawn(self, server):
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        return server

    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.engine = self.spawn(ThreadingHTTPServer(("127.0.0.1", 0), Engine))
        self.engine.last_request = None
        self.driver = Mock()
        self.driver.observe.side_effect = lambda model, *_: {
            "state": "stopped" if model == "stopped" else "running",
        }
        types = {
            "chat": [{"input": ["text"], "output": "text"}],
            "decision": [{"input": ["text"], "output": "decision"}],
            "embed": [{"input": ["text"], "output": "embedding"}],
            "stopped": [{"input": ["text"], "output": "text"}],
            "image": [{"input": ["text", "image"], "output": "image"}],
            "audio": [{"input": ["audio"], "output": "text"}],
            "video": [{"input": ["text"], "output": "video"}],
        }
        catalog = self.home / "models.json"
        catalog.write_text(json.dumps({
            "schema_version": 1, "boot": [],
            "runtimes": {"engine": {"adapter": "systemd", "unit": "example.service"}}, "artifacts": {},
            "models": {model: {
                "name": model,
                "io": model_io,
                "profiles": {"local": {"runtime": "engine", "device": "cpu", "artifacts": {},
                                       "port": self.engine.server_port}},
            } for model, model_io in types.items()},
        }))
        self.controller = Controller(catalog, self.home / "state", driver=self.driver, boot_reader=lambda: "test-boot")
        self.controller.reconcile()
        self.assertEqual(self.controller.boot["state"], "complete")
        self.gateway = self.spawn(make_server(self.controller, port=0, role="inference"))
        self.panel = self.spawn(make_server(self.controller, port=0, role="panel"))

    def request(self, path, data=None, *, gateway=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", (gateway or self.gateway).server_port, timeout=4)
        try:
            if isinstance(data, dict):
                data = json.dumps(data).encode()
                headers = {"Content-Type": "application/json", **(headers or {})}
            conn.request("GET" if data is None else "POST", path, data, headers or {})
            with conn.getresponse() as response:
                body = response.read()
                if response.headers.get_content_type() == "application/json":
                    body = json.loads(body)
                return response.status, body
        finally:
            conn.close()

    def chat(self, model="chat", **kwargs):
        return {"model": model, "messages": [{"role": "user", "content": "private prompt"}], **kwargs}

    def test_discovery_routes_ready_engines_without_lifecycle_actions(self):
        code, response = self.request("/v1/models", headers={"Authorization": "Bearer local"})
        self.assertEqual(code, 200)
        self.assertEqual([entry["id"] for entry in response["data"]],
                         ["audio", "chat", "decision", "embed", "image", "video"])
        code, response = self.request("/v1/chat/completions", self.chat())
        self.assertEqual((code, response["model"]), (200, "chat"))
        self.assertEqual(json.loads(self.engine.last_request[1])["model"], "engine-alias")
        self.assertIsNone(self.engine.last_request[2].get("Authorization"))
        code, response = self.request("/v1/embeddings", {"model": "embed", "input": "test"})
        self.assertEqual((code, response["model"], response["data"][0]["embedding"]), (200, "embed", [1.0]))
        for model, expected_status, expected_error in [
            ("missing", 404, "model_not_found"), ("stopped", 503, "model_not_ready"),
            ("embed", 400, "unsupported_model_io"), ("decision", 400, "unsupported_model_io"),
        ]:
            with self.subTest(model=model):
                self.engine.last_request = None
                code, response = self.request("/v1/chat/completions", self.chat(model))
                self.assertEqual((code, response["error"]["code"]), (expected_status, expected_error))
                self.assertIsNone(self.engine.last_request)
        schema = {"type": "json_schema", "json_schema": {"strict": True, "schema": {"type": "object"}}}
        self.assertEqual(self.request("/v1/chat/completions", self.chat("decision", response_format=schema))[0], 200)
        self.driver.start.assert_not_called()
        self.driver.stop.assert_not_called()

    def test_multipart_and_video_use_same_url(self):
        for model, path, fields in [
            ("image", "/v1/images/edits", [("prompt", None, None, b"change it"),
                                           ("image[]", "foo.png", "image/png", b"\x00\xffimage")]),
            ("audio", "/v1/audio/transcriptions", [("file", "foo.wav", "audio/wav", b"wave")]),
            ("video", "/v1/videos", [("prompt", None, None, b"create a short video")]),
        ]:
            with self.subTest(model=model):
                raw, media = encode_upload(fields, model)
                code, response = self.request(path, raw, headers={"Content-Type": media})
                self.assertEqual(code, 200)
                sent_path, sent_body, sent_headers = self.engine.last_request
                alias, _ = parse_upload(sent_headers["Content-Type"], sent_body, path)
                self.assertEqual((sent_path, alias), (path, "engine-alias"))
                if model == "video":
                    job = response["id"]
                    self.assertNotEqual(job, "engine-job")
                    self.assertEqual(self.request(f"/v1/videos/{job}")[1]["id"], job)
                    self.assertEqual(self.request(f"/v1/videos/{job}/content"), (200, b"\x00mp4"))

    def test_profanity_is_scoped_to_model_and_stream_fragments(self):
        self.assertEqual(self.request("/v1/chat/completions", self.chat())[0], 200)
        status, raw = self.request("/v1/chat/completions", self.chat(stream=True))
        self.assertEqual(status, 200)
        self.assertIn(b'"model": "chat"', raw)
        self.assertIn(b"data: [DONE]\n\n", raw)
        self.assertEqual(self.request("/v1/chat/completions", self.chat("decision", response_format={
            "type": "json_schema", "json_schema": {"strict": True, "schema": {}},
        }))[0], 200)
        status, stats = self.request("/api/stats?model=chat", gateway=self.panel)
        self.assertEqual(status, 200)
        self.assertEqual(stats["language_by_model"], [
            {"model": "chat", "mild": 1, "strong": 1, "slurs": 0, "responses": 2, "truncated": 0},
        ])
        self.assertEqual(len(self.request("/api/stats", gateway=self.panel)[1]["language_by_model"]), 2)
        self.assertEqual(self.request("/api/stats?model=missing", gateway=self.panel)[0], 404)
        self.assertEqual(self.request("/api/stats?model=chat")[0], 404)
        self.assertFalse((self.home / "state" / "inference-logs").exists())
        self.assertNotIn(b"private prompt", self.controller.state.db.read_bytes())

    def test_content_capture_is_opt_in_and_does_not_include_credentials(self):
        directory = self.home / "state" / "inference-logs"
        key = "private-control-credential"
        secured = self.spawn(make_server(
            self.controller, port=0, role="inference", inference_log_dir=directory,
            inference_keys={"test": hashlib.sha256(key.encode()).hexdigest()}))
        self.assertEqual(self.request("/v1/models", gateway=secured)[0], 401)
        self.assertFalse(directory.exists())
        status, body = self.request("/v1/chat/completions", self.chat(), gateway=secured,
                                    headers={"Authorization": f"Bearer {key}"})
        self.assertEqual(status, 200)
        entry, = directory.iterdir()
        self.assertEqual(entry.stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads((entry / "input.body").read_bytes()), self.chat())
        self.assertEqual(json.loads((entry / "output.body").read_bytes()), body)
        self.assertTrue(json.loads((entry / "metadata.json").read_text())["complete"])
        self.assertTrue(all(key.encode() not in file.read_bytes() for file in entry.iterdir()))
        self.assertTrue(all(file.stat().st_mode & 0o777 == 0o600 for file in entry.iterdir()))

    def test_content_capture_preserves_multipart_bytes_and_sse(self):
        directory = self.home / "state" / "inference-logs"
        logged = self.spawn(make_server(self.controller, port=0, role="inference", inference_log_dir=directory))
        raw, media = encode_upload([("file", "test.wav", "audio/wav", b"\x00\xfe\xff")], "audio")
        self.assertEqual(self.request("/v1/audio/transcriptions", raw, gateway=logged,
                                      headers={"Content-Type": media})[0], 200)
        status, stream = self.request("/v1/chat/completions", self.chat(stream=True), gateway=logged)
        self.assertEqual(status, 200)
        first, second = sorted(directory.iterdir())
        self.assertEqual((first / "input.body").read_bytes(), raw)
        self.assertEqual((second / "output.body").read_bytes(), stream)
        self.assertTrue(json.loads((second / "metadata.json").read_bytes())["complete"])

    def test_chat_validates_inline_media_before_contacting_the_engine(self):
        body = self.chat()
        body["messages"][0]["content"] = [{"type": "image_url", "image_url": {"url": "https://example.test/a"}}]
        self.assertEqual(self.request("/v1/chat/completions", body)[0], 400)
        self.assertIsNone(self.engine.last_request)
        body["messages"][0]["content"][0]["image_url"]["url"] = "data:image/png;base64,AA=="
        self.assertEqual(self.request("/v1/chat/completions", body)[1]["error"]["code"], "unsupported_model_io")

    def test_cloudflare_hostname_is_scoped_and_revocable(self):
        host = "models.example.net"
        key = "inference-key"
        digest = hashlib.sha256(key.encode()).hexdigest()
        origin = self.spawn(make_server(self.controller, port=0, role="inference",
                                        public_hostname=host, inference_keys={"laptop": digest}))
        for headers, expected in [
            ({"Host": host}, 401),
            ({"Host": host, "Authorization": "Bearer operator-key"}, 401),
            ({"Host": host, "Authorization": f"Bearer {key}"}, 200),
            ({"Host": "other.example.net", "Authorization": f"Bearer {key}"}, 403),
        ]:
            self.assertEqual(self.request("/v1/models", gateway=origin, headers=headers)[0], expected)
        self.assertEqual(self.request("/api/status", gateway=origin,
                                      headers={"Host": host, "Authorization": f"Bearer {key}"})[0], 404)
        self.assertEqual(self.request("/api/status", gateway=self.panel, headers={"Host": host})[0], 403)
        revoked = self.spawn(make_server(self.controller, port=0, role="inference",
                                         public_hostname=host, inference_keys={}))
        self.assertEqual(self.request("/v1/models", gateway=revoked,
                                      headers={"Host": host, "Authorization": f"Bearer {key}"})[0], 401)


class LanguageTests(unittest.TestCase):
    def test_whole_words_case_variants_and_disguises(self):
        self.assertEqual(count_profanity("D.A.M.N, F\u200buK; sh1t; n1gg4! F A G G O T"),
                         {"mild": 1, "strong": 2, "slurs": 2})
        self.assertEqual(count_profanity("classic assassin, a swampy marsh and a cockpit"),
                         {"mild": 0, "strong": 0, "slurs": 0})
        self.assertEqual(count_profanity("bullsht!fuck"), {"mild": 0, "strong": 2, "slurs": 0})
