import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from lmh.telemetry import Telemetry


def payload(prompt, generation, cached=None):
    cache = "" if cached is None else f"llamacpp:prompt_tokens_cached_total {cached}\n"
    return (f"# TYPE llamacpp:prompt_tokens_total counter\n"
            f"llamacpp:prompt_tokens_total {prompt}\n"
            f"llamacpp:tokens_predicted_total {generation}\n"
            f"llamacpp:requests_processing 42\n"
            f"llamacpp:requests_deferred 7\n"
            f"llamacpp:n_decode_total 99\n{cache}").encode()


@contextmanager
def metrics_server(routes):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            status, headers, body = routes.get(self.path, (404, {}, b""))
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            try:
                if self.path == "/slow":
                    time.sleep(0.15)
                self.wfile.write(body)
            except OSError:
                pass

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        worker.start()
        try:
            yield server.server_port, calls
        finally:
            server.shutdown()
            worker.join()


class TelemetryTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Path(self.directory.name) / "state.sqlite3"
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("CREATE TABLE samples (timestamp REAL PRIMARY KEY)")
            con.execute("INSERT INTO samples VALUES (12)")
        self.telemetry = Telemetry(self.db)
        clock = self.enterContext(patch("lmh.telemetry.time.time"))
        clock.side_effect = lambda: self.now
        self.now = 100

    def test_deltas_persist_and_restart_does_not_spike_rate(self):
        routes = {"/engine/metrics": (200, {"Process-Start-Time-Unix": "11"}, payload(100, 40))}
        with metrics_server(routes) as (port, _):
            self.telemetry.sample("qwen", "gpu", port, "/engine/metrics")
            self.assertIsNone(self.telemetry.stats()["metrics"]["tokens"])
            self.now += 5
            routes["/engine/metrics"] = (200, {"Process-Start-Time-Unix": "11"}, payload(120, 45))
            self.telemetry = Telemetry(self.db)
            self.telemetry.sample("qwen", "gpu", port, "/engine/metrics")
            self.now += 5
            routes["/engine/metrics"] = (200, {"Process-Start-Time-Unix": "22"}, payload(4, 50))
            self.telemetry.sample("qwen", "gpu", port, "/engine/metrics")
            self.now += 5
            self.telemetry.sample("qwen", "gpu", port, "/engine/metrics")

        stats = self.telemetry.stats()
        profile, = stats["profiles"]
        self.assertEqual((profile["model"], profile["profile"]), ("qwen", "gpu"))
        self.assertEqual((profile["prompt_tokens"], profile["generation_tokens"], profile["tokens"]), (24, 55, 79))
        self.assertEqual(stats["metrics"]["tokens"], 79)
        for key in ("requests", "requests_per_second", "prompt_cache_hit_rate",
                    "expert_cache_hit_rate", "inference_error_rate"):
            self.assertIsNone(stats["metrics"][key])
            self.assertIsNone(profile[key])
        baseline, normal, reset, idle = profile["samples"]
        self.assertIsNone(baseline["tokens"])
        self.assertEqual(normal["tokens"], 25)
        self.assertEqual(normal["tokens_per_second"], 5)
        self.assertEqual(reset["tokens"], 54)
        self.assertIsNone(reset["tokens_per_second"])
        self.assertEqual(idle["tokens_per_second"], 0)
        with closing(sqlite3.connect(self.db)) as con:
            self.assertEqual(con.execute("SELECT timestamp FROM samples").fetchall(), [(12.0,)])

    def test_counter_drop_and_changed_endpoint(self):
        routes = {"/metrics": (200, {}, payload(9, 10)), "/other": (200, {}, payload(200, 300))}
        with metrics_server(routes) as (port, _):
            self.telemetry.sample("qwen", "gpu", port)
            self.now += 5
            routes["/metrics"] = (200, {}, payload(2, 3))
            self.telemetry.sample("qwen", "gpu", port)
            self.now += 5
            self.telemetry.sample("qwen", "gpu", port, "/other")

        profile, = self.telemetry.stats()["profiles"]
        self.assertEqual(profile["tokens"], 5)
        self.assertEqual([sample["tokens"] for sample in profile["samples"]], [None, 5, None])
        self.assertIsNone(profile["samples"][1]["tokens_per_second"])

    def test_aggregation_partial_metrics_and_retention(self):
        first = b'llamacpp:prompt_tokens_total{slot="1"} 1e1\nllamacpp:prompt_tokens_total{slot="2"} 5\n'
        second = first.replace(b" 5", b" 8")
        routes = {"/metrics": (200, {}, first)}
        with metrics_server(routes) as (port, _):
            self.telemetry.sample("qwen", "cpu", port)
            self.now += 5
            routes["/metrics"] = (200, {}, second)
            self.telemetry.sample("qwen", "cpu", port)
            self.now += 8 * 86400
            self.telemetry.sample("qwen", "gpu", port)

        cpu, gpu = self.telemetry.stats()["profiles"]
        self.assertEqual(cpu["prompt_tokens"], 3)
        self.assertIsNone(cpu["generation_tokens"])
        self.assertIsNone(cpu["tokens"])
        self.assertEqual(cpu["samples"], [])
        self.assertIsNone(gpu["prompt_tokens"])
        with closing(sqlite3.connect(self.db)) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM inference_samples").fetchone(), (1,))
        self.assertNotIn(b'slot="1"', self.db.read_bytes())

    def test_rejects_redirect_oversize_and_invalid_url(self):
        routes = {
            "/redirect": (302, {"Location": "/metrics"}, b""),
            "/large": (200, {}, payload(20, 30) + b"x" * (256 * 1024)),
            "/metrics": (200, {}, payload(20, 30)),
        }
        with metrics_server(routes) as (port, calls):
            for path in ("/redirect", "/large", f"http://127.0.0.1:{port}/metrics", "//metrics",
                         "/metrics\r\nHost: other", "/invalid"):
                self.telemetry.sample("qwen", "gpu", port, path)
        self.assertEqual(calls, ["/redirect", "/large", "/invalid"])
        self.assertEqual(self.telemetry.stats()["profiles"], [])

    def test_timed_out_scrape_does_not_record_a_baseline(self):
        routes = {"/slow": (200, {}, payload(20, 30))}
        with metrics_server(routes) as (port, calls), patch("lmh.telemetry._TIMEOUT", 0.05):
            self.telemetry.sample("qwen", "gpu", port, "/slow")
        self.assertEqual(calls, ["/slow"])
        self.assertEqual(self.telemetry.stats()["profiles"], [])

    def test_prompt_cache_rate_is_weighted_over_paired_observations(self):
        routes = {
            "/metrics": (200, {}, payload(100, 10, 40)),
            "/second": (200, {}, payload(0, 0, 0)),
            "/legacy": (200, {}, payload(0, 0)),
        }
        with metrics_server(routes) as (port, _):
            def scrape():
                for profile, path in (("gpu", "/metrics"), ("cpu", "/second"), ("old", "/legacy")):
                    self.telemetry.sample("qwen", profile, port, path)

            scrape()
            self.assertIsNone(self.telemetry.stats()["metrics"]["prompt_cache_hit_rate"])
            self.now += 5
            routes.update({
                "/metrics": (200, {}, payload(151, 11, 64)),
                "/second": (200, {}, payload(24, 0, 1)),
                "/legacy": (200, {}, payload(999, 0)),
            })
            scrape()

        stats = self.telemetry.stats()
        cpu, gpu, legacy = stats["profiles"]
        self.assertEqual(cpu["prompt_cache_hit_rate"], 1 / 25)
        self.assertEqual(gpu["prompt_cache_hit_rate"], 24 / 75)
        self.assertEqual(gpu["prompt_tokens"], 51)
        self.assertEqual(gpu["cached_prompt_tokens"], 24)
        self.assertEqual(gpu["tokens"], 52)
        self.assertEqual(gpu["samples"][-1]["cached_prompt_tokens_per_second"], 24 / 5)
        self.assertEqual(gpu["samples"][-1]["prompt_cache_hit_rate"], 24 / 75)
        self.assertIsNone(legacy["cached_prompt_tokens"])
        self.assertIsNone(legacy["prompt_cache_hit_rate"])
        self.assertEqual(stats["metrics"]["cached_prompt_tokens"], 25)
        self.assertEqual(stats["metrics"]["prompt_cache_hit_rate"], 25 / 100)

    def test_cache_restart_and_temporarily_missing_counter(self):
        routes = {}
        with metrics_server(routes) as (port, _):
            for start, prompt, generation, cached in (
                (11, 50, 30, 30), (11, 60, 31, 30), (22, 2, 50, 40),
                (22, 12, 52, None), (22, 22, 55, 42), (22, 32, 57, 44),
                (22, 40, 58, 1),
            ):
                routes["/metrics"] = (200, {"Process-Start-Time-Unix": str(start)},
                                      payload(prompt, generation, cached))
                self.telemetry.sample("qwen", "gpu", port)
                self.now += 5

        profile, = self.telemetry.stats()["profiles"]
        baseline, no_hits, reset, missing, recovered, normal, ambiguous_reset = profile["samples"]
        self.assertIsNone(baseline["prompt_cache_hit_rate"])
        self.assertEqual(no_hits["prompt_cache_hit_rate"], 0.0)
        self.assertEqual(reset["cached_prompt_tokens"], 40)
        self.assertEqual(reset["prompt_cache_hit_rate"], 40 / 42)
        self.assertIsNone(reset["cached_prompt_tokens_per_second"])
        self.assertIsNone(missing["prompt_cache_hit_rate"])
        self.assertIsNone(recovered["prompt_cache_hit_rate"])
        self.assertEqual(normal["prompt_cache_hit_rate"], 2 / 12)
        self.assertEqual(ambiguous_reset["cached_prompt_tokens"], 1)
        self.assertIsNone(ambiguous_reset["prompt_cache_hit_rate"])
        self.assertEqual(profile["cached_prompt_tokens"], 43)
        self.assertEqual(profile["prompt_cache_hit_rate"], 42 / 64)

    def test_migrates_existing_tables_and_preserves_old_rows(self):
        original = Path(self.directory.name) / "original.sqlite3"
        with closing(sqlite3.connect(original)) as con, con:
            con.executescript("""
                CREATE TABLE inference_latest (
                    model TEXT NOT NULL, profile TEXT NOT NULL, timestamp REAL NOT NULL,
                    endpoint TEXT NOT NULL, process_start TEXT,
                    prompt_counter INTEGER, generation_counter INTEGER,
                    prompt_tokens TEXT, generation_tokens TEXT,
                    PRIMARY KEY (model, profile)
                );
                CREATE TABLE inference_samples (
                    model TEXT NOT NULL, profile TEXT NOT NULL, timestamp REAL NOT NULL,
                    prompt_tokens INTEGER, generation_tokens INTEGER, seconds REAL,
                    PRIMARY KEY (model, profile, timestamp)
                );
                INSERT INTO inference_latest VALUES ('qwen', 'gpu', 90, 'old', NULL, 60, 30, '20', '10');
                INSERT INTO inference_samples VALUES ('qwen', 'gpu', 90, 20, 10, 5);
            """)
        Telemetry(original)
        migrated = Telemetry(original)
        routes = {"/metrics": (200, {}, payload(100, 50, 40))}
        with metrics_server(routes) as (port, _):
            migrated.sample("qwen", "gpu", port)
            self.now += 5
            routes["/metrics"] = (200, {}, payload(151, 55, 64))
            migrated.sample("qwen", "gpu", port)

        profile, = migrated.stats()["profiles"]
        old, baseline, new = profile["samples"]
        self.assertEqual(old["tokens"], 30)
        self.assertIsNone(old["prompt_cache_hit_rate"])
        self.assertIsNone(baseline["cached_prompt_tokens"])
        self.assertEqual(new["prompt_cache_hit_rate"], 24 / 75)
        self.assertEqual(profile["tokens"], 86)
        self.assertEqual(profile["prompt_cache_hit_rate"], 24 / 75)
