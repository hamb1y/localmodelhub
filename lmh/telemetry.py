"""Optional llama.cpp engine metrics; never stores response text or labels."""

from __future__ import annotations

import hashlib
import http.client
import re
import socket
import sqlite3
import threading
import time
from contextlib import closing
from decimal import Decimal
from pathlib import Path

_TIMEOUT = 1.0
_MAX_BYTES = 256 * 1024
_MAX_COUNTER = 2**63 - 1
_DEFAULT_METRICS = {
    "prompt_tokens": "llamacpp:prompt_tokens_total",
    "generation_tokens": "llamacpp:tokens_predicted_total",
    "cached_tokens": "llamacpp:prompt_tokens_cached_total",
}
_SAMPLE = re.compile(
    r'([A-Za-z_:][A-Za-z0-9_:]*)'
    r'(?:\{(?:[^"\\}]|"(?:[^"\\]|\\.)*")*\})?[ \t]+([^ \t]+)(?:[ \t]+[0-9]+)?[ \t]*'
)
_NUMBER = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?')
_UNSUPPORTED = {
    "expert_cache_hit_rate": None,
}


def _shutdown(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _fetch(port: int, path: str) -> tuple[bytes, str | None] | None:
    if (type(port) is not int or not 0 < port <= 65535 or not isinstance(path, str)
            or not path.startswith("/") or path.startswith("//")
            or len(path) > 2048 or not re.fullmatch(r"[\x21-\x7e]+", path)
            or any(char in path for char in "\\#")):
        return None
    with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=_TIMEOUT)) as conn:
        try:
            deadline = time.monotonic() + _TIMEOUT
            conn.connect()
            timer = threading.Timer(max(0, deadline - time.monotonic()), _shutdown, (conn.sock,))
            timer.daemon = True
            timer.start()
            try:
                conn.request("GET", path, headers={"Connection": "close", "Accept": "text/plain"})
                with conn.getresponse() as response:
                    if response.status != 200 or (response.length or 0) > _MAX_BYTES:
                        return None
                    body = response.read(_MAX_BYTES + 1)
                    if (len(body) > _MAX_BYTES or response.length
                            or time.monotonic() >= deadline):
                        return None
                    start = response.getheader("Process-Start-Time-Unix", "")
                    return body, start if re.fullmatch(r"[0-9]{1,24}", start) else None
            finally:
                timer.cancel()
        except (OSError, http.client.HTTPException, ValueError):
            return None


def _counters(body: bytes, names: dict | None = None) -> dict:
    """Sum each configured counter, keeping unreported values as None."""
    wanted = _DEFAULT_METRICS if names is None else names
    by_name = {}
    for counter, metric in wanted.items():
        by_name.setdefault(metric, []).append(counter)
    totals: dict[str, int | None] = dict.fromkeys(wanted, 0)
    seen = set()
    for line in body.decode("utf-8").splitlines():
        match = _SAMPLE.fullmatch(line)
        if not match:
            continue
        name, number = match.groups()
        counters = by_name.get(name)
        if not counters:
            continue
        # Bound Decimal input and SQLite integer arithmetic.
        value = Decimal(number) if len(number) < 64 and _NUMBER.fullmatch(number) else Decimal(-1)
        for counter in counters:
            seen.add(counter)
            total = totals[counter]
            if (total is None or not 0 <= value <= _MAX_COUNTER
                    or value != value.to_integral_value() or total + int(value) > _MAX_COUNTER):
                totals[counter] = None
            else:
                totals[counter] = total + int(value)
    return {counter: totals[counter] if counter in seen else None for counter in wanted}


def _delta(old: int | None, new: int | None, restarted: bool) -> tuple[int | None, bool]:
    if old is None or new is None:
        return None, False
    if restarted or new < old:
        return new, True
    return new - old, False


def _sum(first: int | None, second: int | None) -> int | None:
    return first + second if first is not None and second is not None else None


def _accumulate(total: str | None, delta: int | None) -> str | None:
    return total if delta is None else str(int(total or 0) + delta)


def _cache_rate(processed: int | None, cached: int | None) -> float | None:
    total = _sum(processed, cached)
    return cached / total if total else None


class Telemetry:
    """Scrape only when called with a verified running profile."""

    def __init__(self, db: Path):
        self.db = db
        with closing(self._connect()) as con, con:
            con.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS inference_latest (
                    model TEXT NOT NULL, profile TEXT NOT NULL, timestamp REAL NOT NULL,
                    endpoint TEXT NOT NULL, process_start TEXT,
                    prompt_counter INTEGER, generation_counter INTEGER,
                    prompt_tokens TEXT, generation_tokens TEXT,
                    PRIMARY KEY (model, profile)
                );
                CREATE TABLE IF NOT EXISTS inference_samples (
                    model TEXT NOT NULL, profile TEXT NOT NULL, timestamp REAL NOT NULL,
                    prompt_tokens INTEGER, generation_tokens INTEGER, seconds REAL,
                    PRIMARY KEY (model, profile, timestamp)
                );
                CREATE INDEX IF NOT EXISTS inference_samples_timestamp
                    ON inference_samples (timestamp);
            """)
            # NULL preserves unknown cache data in rows written by older versions.
            for table, additions in (
                ("inference_latest", ("cached_counter INTEGER", "cached_prompt_tokens TEXT",
                                      "cache_processed_tokens TEXT", "cache_cached_tokens TEXT",
                                      "request_counter INTEGER", "requests TEXT",
                                      "error_counter INTEGER", "errors TEXT")),
                ("inference_samples", ("cached_prompt_tokens INTEGER", "prompt_cache_hit_rate REAL",
                                       "requests INTEGER", "errors INTEGER")),
            ):
                columns = {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}
                for spec in additions:
                    if spec.split()[0] not in columns:
                        con.execute(f"ALTER TABLE {table} ADD COLUMN {spec}")

    def _connect(self):
        con = sqlite3.connect(self.db, timeout=1)
        con.row_factory = sqlite3.Row
        return con

    def sample(self, model: str, profile: str, port: int, path: str = "/metrics",
               names: dict | None = None) -> None:
        result = _fetch(port, path)
        if result is None:
            return
        body, process_start = result
        try:
            counters = _counters(body, names)
        except (UnicodeError, ArithmeticError):
            return
        if all(value is None for value in counters.values()):
            return
        prompt = counters.get("prompt_tokens")
        generation = counters.get("generation_tokens")
        cached = counters.get("cached_tokens")
        requests = counters.get("requests")
        errors = counters.get("errors")
        timestamp = time.time()
        endpoint = hashlib.sha256(f"{port}:{path}".encode()).hexdigest()
        with closing(self._connect()) as con, con:
            con.execute("BEGIN IMMEDIATE")
            previous = con.execute(
                "SELECT * FROM inference_latest WHERE model=? AND profile=?", (model, profile)
            ).fetchone()
            if previous and timestamp <= previous["timestamp"]:
                return
            prompt_delta = generation_delta = cached_delta = seconds = None
            request_delta = error_delta = None
            prompt_total = generation_total = cached_total = None
            request_total = error_total = None
            paired_processed = paired_cached = cache_rate = None
            if previous:
                prompt_total = previous["prompt_tokens"]
                generation_total = previous["generation_tokens"]
                cached_total = previous["cached_prompt_tokens"]
                request_total = previous["requests"]
                error_total = previous["errors"]
                paired_processed = previous["cache_processed_tokens"]
                paired_cached = previous["cache_cached_tokens"]
                if previous["endpoint"] == endpoint:
                    old_start = previous["process_start"]
                    restarted = bool(process_start and old_start and process_start != old_start)
                    prompt_delta, prompt_reset = _delta(previous["prompt_counter"], prompt, restarted)
                    generation_delta, generation_reset = _delta(
                        previous["generation_counter"], generation, restarted
                    )
                    cached_delta, cache_reset = _delta(previous["cached_counter"], cached, restarted)
                    request_delta, request_reset = _delta(previous["request_counter"], requests, restarted)
                    error_delta, error_reset = _delta(previous["error_counter"], errors, restarted)
                    if not (restarted or prompt_reset or generation_reset or cache_reset
                            or request_reset or error_reset):
                        seconds = timestamp - previous["timestamp"]
                    # Only compare deltas covering the same interval or reset epoch.
                    if (prompt_delta is not None and cached_delta is not None
                            and (restarted or not (prompt_reset or cache_reset or generation_reset))):
                        paired_processed = _accumulate(paired_processed, prompt_delta)
                        paired_cached = _accumulate(paired_cached, cached_delta)
                        cache_rate = _cache_rate(prompt_delta, cached_delta)
                    process_start = process_start or old_start
            # Decimal strings keep totals exact even after many counter resets.
            con.execute(
                """INSERT OR REPLACE INTO inference_latest
                   (model, profile, timestamp, endpoint, process_start, prompt_counter,
                    generation_counter, prompt_tokens, generation_tokens, cached_counter,
                    cached_prompt_tokens, cache_processed_tokens, cache_cached_tokens,
                    request_counter, requests, error_counter, errors)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (model, profile, timestamp, endpoint, process_start, prompt, generation,
                 _accumulate(prompt_total, prompt_delta), _accumulate(generation_total, generation_delta),
                 cached, _accumulate(cached_total, cached_delta), paired_processed, paired_cached,
                 requests, _accumulate(request_total, request_delta),
                 errors, _accumulate(error_total, error_delta)),
            )
            con.execute(
                """INSERT INTO inference_samples
                   (model, profile, timestamp, prompt_tokens, generation_tokens, seconds,
                    cached_prompt_tokens, prompt_cache_hit_rate, requests, errors)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (model, profile, timestamp, prompt_delta, generation_delta, seconds,
                 cached_delta, cache_rate, request_delta, error_delta),
            )
            con.execute("DELETE FROM inference_samples WHERE timestamp < ?", (timestamp - 7 * 86400,))

    def stats(self, model=None) -> dict:
        """Return observed totals and a six-hour series of scrape intervals.

        Cache hit rates use paired observations only. The aggregate is weighted
        by observed prompt tokens, not by profile count. Counters an engine does
        not publish stay ``None`` rather than becoming zero.
        """
        profiles = {}
        processed_sum = cached_sum = 0
        rate_requests = 0
        rate_seconds = 0.0
        with closing(self._connect()) as con, con:
            con.execute("BEGIN")
            for row in con.execute("""
                SELECT * FROM inference_latest WHERE (? IS NULL OR model=?) ORDER BY model, profile
            """, (model, model)):
                item = self._usage(row)
                processed = int(row["cache_processed_tokens"] or 0)
                cached = int(row["cache_cached_tokens"] or 0)
                processed_sum += processed
                cached_sum += cached
                item.update(model=row["model"], profile=row["profile"], timestamp=row["timestamp"],
                            samples=[], prompt_cache_hit_rate=_cache_rate(processed, cached),
                            requests_per_second=None, **_UNSUPPORTED)
                item["inference_error_rate"] = self._error_rate(item["requests"], item["errors"])
                profiles[(row["model"], row["profile"])] = item
            for row in con.execute("""
                SELECT * FROM inference_samples WHERE timestamp >= ?
                AND (? IS NULL OR model=?) ORDER BY timestamp
            """, (time.time() - 6 * 3600, model, model)):
                item = self._usage(row)
                seconds = row["seconds"]
                item.update(timestamp=row["timestamp"], interval_seconds=seconds,
                            prompt_cache_hit_rate=row["prompt_cache_hit_rate"])
                for key in ("prompt_tokens", "generation_tokens", "tokens", "cached_prompt_tokens"):
                    value = item[key]
                    item[f"{key}_per_second"] = value / seconds if value is not None and seconds else None
                item["requests_per_second"] = (item["requests"] / seconds
                                               if item["requests"] is not None and seconds else None)
                if item["requests"] is not None and seconds:
                    rate_requests += item["requests"]
                    rate_seconds += seconds
                profiles[(row["model"], row["profile"])]["samples"].append(item)
        for item in profiles.values():
            for sample in reversed(item["samples"]):
                if sample["requests_per_second"] is not None:
                    item["requests_per_second"] = sample["requests_per_second"]
                    break
        counts = [item["tokens"] for item in profiles.values()]
        cached_counts = [item["cached_prompt_tokens"] for item in profiles.values()
                         if item["cached_prompt_tokens"] is not None]
        request_totals = [item["requests"] for item in profiles.values()]
        error_totals = [item["errors"] for item in profiles.values()]
        requests = sum(request_totals) if request_totals and None not in request_totals else None
        errors = sum(error_totals) if error_totals and None not in error_totals else None
        return {
            "metrics": {"tokens": sum(counts) if counts and None not in counts else None,
                        "cached_prompt_tokens": sum(cached_counts) if cached_counts else None,
                        "prompt_cache_hit_rate": _cache_rate(processed_sum, cached_sum),
                        "requests": requests,
                        "requests_per_second": rate_requests / rate_seconds if rate_seconds else None,
                        "inference_error_rate": self._error_rate(requests, errors),
                        **_UNSUPPORTED},
            "profiles": list(profiles.values()),
        }

    @staticmethod
    def _error_rate(requests, errors):
        if requests is None or errors is None or requests <= 0:
            return None
        return errors / requests

    @staticmethod
    def _usage(row: sqlite3.Row) -> dict:
        prompt, generation = row["prompt_tokens"], row["generation_tokens"]
        prompt = int(prompt) if prompt is not None else None
        generation = int(generation) if generation is not None else None
        cached = row["cached_prompt_tokens"]
        requests = row["requests"]
        errors = row["errors"]
        return {"prompt_tokens": prompt, "generation_tokens": generation, "tokens": _sum(prompt, generation),
                "cached_prompt_tokens": int(cached) if cached is not None else None,
                "requests": int(requests) if requests is not None else None,
                "errors": int(errors) if errors is not None else None}
