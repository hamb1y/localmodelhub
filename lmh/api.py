"""Local control panel HTTP API and read-only catalog previews."""

import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
import math
import mimetypes
import queue
import re
import shutil
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from lmh.catalog import validate_catalog
from lmh import inference
from lmh.inference_log import InferenceLog


_ACTIONS = {"start", "stop", "switch"}
PANEL_PORT, INFERENCE_PORT, WEBHOOK_PORT = 5100, 5101, 5102
WEBHOOK_PATH = "/api/webhooks/action"
_ACTION_LIMIT = 16 * 1024
_CATALOG_LIMIT = 1024 * 1024
_TIMEOUT = 8
_FETCH_TIMEOUT = 5
_DNS_SLOTS = threading.BoundedSemaphore(4)
_FETCH_SLOTS = threading.BoundedSemaphore(4)
_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*")
_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_IPV6_TRANSITION = tuple(map(ipaddress.ip_network, (
    "64:ff9b::/96", "64:ff9b:1::/48", "2001::/32", "2002::/16",
)))


class _HTTPError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _dns_name(name):
    return len(name) <= 253 and all(_LABEL.fullmatch(label) for label in name.split("."))


def _authority(value, default_port=80):
    if not value or any(ord(c) <= 32 or ord(c) >= 127 for c in value):
        raise ValueError("invalid authority")
    match = re.fullmatch(r"(\[[0-9a-fA-F:.]+\]|[a-zA-Z0-9.-]+)(?::([0-9]{1,5}))?", value)
    if not match:
        raise ValueError("invalid authority")
    name, port = match.groups()
    if name.startswith("["):
        name = str(ipaddress.IPv6Address(name[1:-1]))
    else:
        name = name.lower()
        if not _dns_name(name.rstrip(".")) or name.endswith(".."):
            raise ValueError("invalid host name")
    port = int(port) if port is not None else default_port
    if not 1 <= port <= 65535:
        raise ValueError("invalid port")
    return name, port


def _loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _finite(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


def _decode_json(data):
    return json.loads(
        data.decode("utf-8"), object_pairs_hook=_json_object,
        parse_float=_finite, parse_constant=_finite,
    )


def _identifier(value):
    return (
        isinstance(value, str) and 0 < len(value) <= 256 and value.strip() == value
        and value.isprintable()
    )


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return remaining


def _public_address(address):
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if address.version == 6:
        return (
            address.ipv4_mapped is None and not address.is_site_local
            and not any(address in network for network in _IPV6_TRANSITION)
        )
    return True


def _resolve_public(host, deadline):
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _public_address(literal):
            raise _HTTPError(400, "catalog URL must resolve only to public addresses")
        if literal.version == 6:
            return [(socket.AF_INET6, (str(literal), 443, 0, 0))]
        return [(socket.AF_INET, (str(literal), 443))]

    if "." not in host or host.endswith((".localhost", ".local", ".internal", ".home.arpa")):
        raise _HTTPError(400, "catalog URL must name a public host")
    if not _DNS_SLOTS.acquire(blocking=False):
        raise _HTTPError(503, "catalog DNS resolution is busy")
    result = queue.Queue(maxsize=1)

    def resolve():
        try:
            # Absolute DNS name: do not consult resolver search suffixes.
            result.put(socket.getaddrinfo(host + ".", 443, type=socket.SOCK_STREAM))
        except OSError as error:
            result.put(error)
        finally:
            _DNS_SLOTS.release()

    worker = threading.Thread(target=resolve, daemon=True)
    try:
        worker.start()
    except RuntimeError:
        _DNS_SLOTS.release()
        raise
    addresses = result.get(timeout=_remaining(deadline))
    if isinstance(addresses, OSError):
        raise addresses
    pins = []
    for family, _, _, _, sockaddr in addresses:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        address = ipaddress.ip_address(sockaddr[0])
        if not _public_address(address) or (family == socket.AF_INET6 and sockaddr[3]):
            raise _HTTPError(400, "catalog URL must resolve only to public addresses")
        if family == socket.AF_INET6:
            pins.append((family, (str(address), 443, 0, 0)))
        else:
            pins.append((family, (str(address), 443)))
    if not pins:
        raise OSError("no usable DNS addresses")
    return list(dict.fromkeys(pins))


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, pin, deadline, context):
        super().__init__(host, port=443, timeout=_remaining(deadline), context=context)
        self._pin = pin
        self._deadline = deadline
        self._tls_context = context
        self._watchdog = None
        self._guard = None

    def connect(self):
        family, sockaddr = self._pin
        with socket.socket(family, socket.SOCK_STREAM) as raw:
            raw.settimeout(_remaining(self._deadline))
            raw.connect(sockaddr)
            # The duplicate can interrupt slow-drip headers or bodies even after
            # ssl.wrap_socket takes ownership and HTTPResponse owns the reader.
            self._guard = raw.dup()
            self._watchdog = threading.Timer(_remaining(self._deadline), self._abort)
            self._watchdog.daemon = True
            self._watchdog.start()
            raw.settimeout(_remaining(self._deadline))
            self.sock = self._tls_context.wrap_socket(raw, server_hostname=self.host)

    def _abort(self):
        try:
            self._guard.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish(self):
        # http.client can close the connection before the response body is read.
        # Keep the deadline active until the caller has finished reading.
        try:
            self.close()
        finally:
            if self._watchdog is not None:
                self._watchdog.cancel()
            if self._guard is not None:
                self._guard.close()


def _fetch_catalog(url):
    try:
        if not isinstance(url, str) or not 0 < len(url) <= 2048:
            raise ValueError
        if any(ord(c) <= 32 or ord(c) >= 127 or c == "\\" for c in url) or "#" in url:
            raise ValueError
        parts = urlsplit(url)
        host, port = _authority(parts.netloc, 443)
        if parts.scheme != "https" or port != 443:
            raise ValueError
    except ValueError as error:
        raise _HTTPError(400, "catalog URL must be an HTTPS URL on port 443") from error
    if not _FETCH_SLOTS.acquire(blocking=False):
        raise _HTTPError(503, "catalog fetch is busy")
    try:
        deadline = time.monotonic() + _FETCH_TIMEOUT
        host = host.rstrip(".")
        pins = _resolve_public(host, deadline)
        context = ssl.create_default_context()
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        for index, pin in enumerate(pins):
            connection = _PinnedHTTPS(host, pin, deadline, context)
            try:
                try:
                    connection.connect()
                except (OSError, http.client.HTTPException):
                    if index + 1 == len(pins):
                        raise
                    continue
                connection.request("GET", target, headers={
                    "Accept": "application/json", "Accept-Encoding": "identity",
                    "Connection": "close",
                })
                with connection.getresponse() as response:
                    if 300 <= response.status < 400:
                        raise _HTTPError(400, "catalog redirects are not allowed")
                    if not 200 <= response.status < 300:
                        raise _HTTPError(502, "catalog host did not return a successful response")
                    if response.getheader("Content-Encoding", "identity").lower() != "identity":
                        raise _HTTPError(502, "encoded catalog responses are not supported")
                    if response.length is not None and response.length > _CATALOG_LIMIT:
                        raise _HTTPError(413, "catalog response is too large")
                    data = response.read(_CATALOG_LIMIT + 1)
                    _remaining(deadline)
                    if len(data) > _CATALOG_LIMIT:
                        raise _HTTPError(413, "catalog response is too large")
                    if response.length:
                        raise _HTTPError(502, "catalog response is incomplete")
                try:
                    return _decode_json(data)
                except (ValueError, RecursionError) as error:
                    raise _HTTPError(400, "catalog response is not valid JSON") from error
            finally:
                connection.finish()
    except (OSError, http.client.HTTPException, queue.Empty) as error:
        raise _HTTPError(502, "could not fetch catalog") from error
    finally:
        _FETCH_SLOTS.release()


def _credential(token):
    if not isinstance(token, str) or len(token) > 512 or not _TOKEN.fullmatch(token):
        raise ValueError("credentials must use bearer token syntax")
    return token


def _webhook_config(webhooks, api_token):
    scopes = {}
    used = {api_token}
    if webhooks is not None and not isinstance(webhooks, dict):
        raise ValueError("webhooks must be keyed by name")
    for name, scope in (webhooks or {}).items():
        if not _identifier(name) or not isinstance(scope, dict):
            raise ValueError("invalid webhook name or scope")
        token = _credential(scope.get("token"))
        actions, models = scope.get("actions"), scope.get("models")
        if (
            token in used or not isinstance(actions, (list, tuple))
            or any(not isinstance(action, str) or action not in _ACTIONS for action in actions)
            or not isinstance(models, (list, tuple)) or not all(_identifier(model) for model in models)
        ):
            raise ValueError("invalid webhook scope or duplicate credential")
        used.add(token)
        scopes[name] = (token, frozenset(actions), frozenset(models))
    return scopes


def inference_hostname(value):
    if not isinstance(value, str) or "." not in value or not _dns_name(value):
        raise ValueError("Use a public hostname without a protocol, port, or path.")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value.lower()
    raise ValueError("Use a public hostname, not an IP address.")


def inference_policy(keys, hostname):
    if keys is not None and (not isinstance(keys, dict) or len(keys) > 128 or any(
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name)
        or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
        for name, digest in keys.items()
    )):
        raise ValueError("inference_keys must map key names to SHA-256 digests (use lmh api-key).")
    return (tuple(keys.values()) if keys is not None else None,
            inference_hostname(hostname) if hostname is not None else None)


class _Server(ThreadingHTTPServer):
    def __init__(self, address, controller, local, ui_dir, api_token, webhooks, inference_log_dir,
                 role, inference_config):
        self.controller = controller
        self.local = local
        self.ui_dir = ui_dir
        self.api_token = api_token
        self.webhooks = webhooks
        self.role = role
        self.inference_keys, self.public_hostname = inference_config
        self.listener_ports = None
        self.inference_url = None
        self.inference_log_dir = inference_log_dir
        self.inference_log_warned = threading.Event()
        self._slots = threading.BoundedSemaphore(32)
        self.inference_slots = threading.BoundedSemaphore(8)
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, _Handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class _Handler(BaseHTTPRequestHandler):
    timeout = _TIMEOUT
    server_version = "lmh"
    sys_version = ""
    inference_log = None

    def log_message(self, format, *args):
        # Request targets and headers need not be copied into operational logs.
        pass

    def do_GET(self):
        self._dispatch()

    do_HEAD = do_GET

    def do_POST(self):
        self._dispatch()

    def do_OPTIONS(self):
        self._json(405, {"error": "method not allowed"})

    def _dispatch(self):
        self.inference_log = None
        path = self.path.partition("?")[0]
        is_inference = path == "/v1" or path.startswith("/v1/")
        try:
            authority = self._check_host()
            if self.command == "POST":
                self._check_origin(authority)
            if not self.path.startswith("/") or "#" in self.path:
                raise _HTTPError(400, "invalid request target")
            path, _, query = self.path.partition("?")
            role = self.server.role
            if role == "webhook" and path == WEBHOOK_PATH and self.command == "POST" and not query:
                self._submit(self._webhook_auth())
            elif role == "inference" and is_inference:
                self._inference(path, query)
            elif role != "panel" or is_inference or path == WEBHOOK_PATH:
                raise _HTTPError(404, "not found")
            elif path == "/api" or path.startswith("/api/"):
                self._api(path, query)
            elif self.command in {"GET", "HEAD"}:
                self._static(path)
            else:
                raise _HTTPError(404, "not found")
        except (_HTTPError, inference.InferenceError) as error:
            if is_inference:
                code = getattr(error, "code", "invalid_request")
                kind = "server_error" if error.status >= 500 else "invalid_request_error"
                self._json(error.status, {"error": {"message": str(error), "type": kind, "code": code,
                                                    "param": None}})
            else:
                self._json(error.status, {"error": str(error)})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        finally:
            self._finish_capture()

    def _check_host(self):
        values = self.headers.get_all("Host", [])
        if len(values) != 1:
            raise _HTTPError(400, "one Host header is required")
        try:
            authority = _authority(values[0])
        except ValueError as error:
            raise _HTTPError(400, "invalid Host header") from error
        if self.server.local:
            local_authority = _loopback(authority[0]) and authority[1] == self.server.server_port
            public_authority = (self.server.role == "inference" and self.server.public_hostname
                                and authority[0] == self.server.public_hostname
                                and authority[1] in {80, 443})
            if not _loopback(self.client_address[0]) or not (local_authority or public_authority):
                raise _HTTPError(403, "Host must match the local listener or configured inference hostname")
        return authority

    def _check_origin(self, authority):
        origins = self.headers.get_all("Origin", [])
        fetch_site = self.headers.get_all("Sec-Fetch-Site", [])
        if fetch_site and fetch_site not in (["same-origin"], ["none"]):
            raise _HTTPError(403, "cross-origin requests are not allowed")
        if not origins:
            return  # CLI clients do not send Origin.
        try:
            if len(origins) != 1:
                raise ValueError
            if origins[0].startswith("http://"):
                allowed = _authority(origins[0][7:]) == authority
            elif origins[0].startswith("https://") and (
                self.server.api_token is not None or authority[0] == self.server.public_hostname
            ):
                # Authenticated deployments may terminate TLS in a proxy that
                # preserves Host. Do not trust client-supplied forwarding headers.
                allowed = _authority(origins[0][8:], 443) == _authority(self.headers["Host"], 443)
            else:
                allowed = False
            if not allowed:
                raise ValueError
        except ValueError as error:
            raise _HTTPError(403, "cross-origin requests are not allowed") from error

    def _bearer(self):
        values = self.headers.get_all("Authorization", [])
        if not values:
            return None
        scheme, _, token = values[0].partition(" ")
        if len(values) != 1 or scheme.lower() != "bearer" or (
            len(token) > 512 or not _TOKEN.fullmatch(token)
        ):
            raise _HTTPError(401, "valid bearer credential required")
        return token

    def _normal_auth(self):
        expected = self.server.api_token
        actual = self._bearer()
        if expected is None and actual is None:
            return
        if expected is None or actual is None or not hmac.compare_digest(expected, actual):
            raise _HTTPError(401, "valid API bearer credential required")

    def _webhook_auth(self):
        actual = self._bearer()
        matched = None
        if actual is not None:
            for name, (token, actions, models) in self.server.webhooks.items():
                if hmac.compare_digest(token, actual):
                    matched = (name, actions, models)
        if matched is None:
            raise _HTTPError(401, "valid webhook bearer credential required")
        return matched

    def _inference(self, path, query):
        token = self._bearer()
        keys = self.server.inference_keys
        if self.server.public_hostname is not None or keys is not None:
            digest = hashlib.sha256(token.encode("ascii")).hexdigest() if token is not None else ""
            valid = False
            for expected in keys or ():
                valid |= hmac.compare_digest(digest, expected)
            if not valid:
                raise _HTTPError(401, "Valid inference API key required. Create one with lmh api-key create.")
        if query:
            raise _HTTPError(400, "Inference routes do not accept query parameters")
        if not self.server.inference_slots.acquire(blocking=False):
            raise inference.InferenceError(429, "Too many active inference requests.", "rate_limit")
        try:
            self._inference_request(path)
        finally:
            self.server.inference_slots.release()

    def _inference_request(self, path):
        controller = self.server.controller
        if self.command == "GET" and path.startswith("/v1/videos/"):
            self._video(path)
            return
        if self.command in {"GET", "HEAD"} and (path == "/v1/models" or path.startswith("/v1/models/")):
            models = controller.inference_types()
            if path == "/v1/models":
                self._json(200, {"object": "list", "data": [
                    self._model_descriptor(model, pairs, controller.model_aliases(model))
                    for model, pairs in sorted(models.items())
                    if self._engine_target(model)
                ]})
            else:
                model = inference.model_id(path.removeprefix("/v1/models/"))
                model = controller.resolve_model(model)
                if model not in models or not self._engine_target(model):
                    raise inference.InferenceError(404, "Model is not available.", "model_not_found")
                self._json(200, self._model_descriptor(model, models[model],
                                                       controller.model_aliases(model)))
            return
        if self.command != "POST" or path not in inference.JSON_ROUTES.keys() | inference.UPLOAD_ROUTES.keys():
            raise inference.InferenceError(404, "Inference endpoint not supported.", "endpoint_not_found")
        if path in inference.JSON_ROUTES:
            request_type, raw = self._read_body(inference.REQUEST_LIMIT, "application/json")
            body = self._parse_body(raw)
            model = inference.model_id(body.get("model"))
            model = controller.resolve_model(model)
            inputs, output = inference.json_io(path, body)
        else:
            request_type, raw = self._read_body(inference.REQUEST_LIMIT, "multipart/form-data")
            model, fields = inference.parse_upload(request_type, raw, path)
            model = controller.resolve_model(model)
            inputs, output = inference.UPLOAD_ROUTES[path]
            if path == "/v1/videos" and any(name == "input_reference" for name, *_ in fields):
                inputs = inputs | {"image"}
            body = None
        pairs = controller.inference_types().get(model)
        if pairs is None:
            raise inference.InferenceError(404, "Unknown model ID. See /v1/models.", "model_not_found")
        if not inference.supports(pairs, inputs, output, body):
            raise inference.InferenceError(400, "This input/output pair is not declared by the model.",
                                           "unsupported_model_io")
        target = self._engine_target(model)
        if not target:
            raise inference.InferenceError(503, "Model is not ready. Start it explicitly in LocalModelHub.",
                                           "model_not_ready")
        self._begin_capture(model, path, request_type, raw)
        alias = inference.upstream_alias(target, model, controller.upstream_map(model, target), path)
        if body is None:
            payload, media = inference.encode_upload(fields, alias)
        else:
            media = "application/json"
            forwarded = {key: value for key, value in body.items() if key != "model"}
            if alias is not None:
                forwarded["model"] = alias
            payload = json.dumps(forwarded, allow_nan=False).encode()
        inference.relay(self, target, path, payload, media, model)

    def _video(self, path):
        job, separator, asset = path.removeprefix("/v1/videos/").partition("/")
        if not re.fullmatch(r"video_[0-9a-f]{32}", job) or (separator and asset != "content"):
            raise inference.InferenceError(404, "Unknown video.", "video_not_found")
        stored = self.server.controller.state.video(job)
        if stored is None:
            raise inference.InferenceError(404, "Unknown video.", "video_not_found")
        model = stored["model"]
        target = self._engine_target(model)
        if target != (stored["host"], stored["port"]):
            raise inference.InferenceError(503, "The original video engine is not ready.", "model_not_ready")
        self._begin_capture(model, path, None, b"")
        upstream_path = f"/v1/videos/{stored['upstream_id']}" + ("/content" if asset else "")
        inference.relay(self, target, upstream_path, None, None, model, video_id=job)

    def _begin_capture(self, model, path, request_type, raw):
        if self.server.inference_log_dir is not None:
            try:
                self.inference_log = InferenceLog(self.server.inference_log_dir, model, path, request_type, raw)
            except OSError:
                # Local capture is best effort; never log payloads to the journal.
                self._capture_warning()

    def _capture_warning(self):
        if not self.server.inference_log_warned.is_set():
            self.server.inference_log_warned.set()
            logging.warning("Could not save an inference log. Check the directory permissions and available space.")

    def _finish_capture(self):
        if self.inference_log is not None:
            capture, self.inference_log = self.inference_log, None
            try:
                capture.finish()
            except OSError:
                self._capture_warning()

    def _write_body(self, data, *, complete=False):
        if self.inference_log is not None:
            try:
                self.inference_log.write(data)
            except OSError:
                self._capture_warning()
                self._finish_capture()
        if complete:
            self._complete_body()
        self.wfile.write(data)

    def _complete_body(self):
        if self.inference_log is not None:
            self.inference_log.metadata["complete"] = True
            self._finish_capture()

    def _engine_target(self, model):
        ports = self.server.listener_ports or {self.server.server_port}
        return self.server.controller.inference_target(model, exclude_ports=ports)

    @staticmethod
    def _model_descriptor(model, pairs, aliases=()):
        descriptor = {"id": model, "object": "model", "created": 0, "owned_by": "localmodelhub", "io": pairs}
        if aliases:
            descriptor["aliases"] = list(aliases)
        return descriptor

    def _api(self, path, query):
        if path == "/api/health" and self.command in {"GET", "HEAD"}:
            self._json(200, {"ok": True})
            return
        self._normal_auth()
        controller = self.server.controller
        if self.command in {"GET", "HEAD"}:
            try:
                if path == "/api/status":
                    data = controller.status()
                    if self.server.inference_url:
                        data = {**data, "inference_url": self.server.inference_url}
                elif path == "/api/stats":
                    if query:
                        params = parse_qs(query, keep_blank_values=True, max_num_fields=1, errors="strict")
                        if set(params) != {"model"} or len(params["model"]) != 1:
                            raise ValueError("one model is required")
                        model = params["model"][0]
                        if model not in controller.inference_types():
                            raise _HTTPError(404, "unknown model")
                        data = controller.stats(model)
                    else:
                        data = controller.stats()
                elif path == "/api/logs":
                    params = parse_qs(query, keep_blank_values=True, max_num_fields=2, errors="strict")
                    if set(params) != {"model", "profile"} or any(
                        len(values) != 1 or not _identifier(values[0]) for values in params.values()
                    ):
                        raise ValueError("one model and one profile are required")
                    data = {"lines": controller.logs(params["model"][0], params["profile"][0])}
                else:
                    raise _HTTPError(404, "not found")
            except (ValueError, KeyError) as error:
                raise _HTTPError(400, str(error)) from error
            except RuntimeError as error:
                raise _HTTPError(409, str(error)) from error
            self._json(200, data)
        elif path == "/api/actions":
            self._submit()
        elif path == "/api/catalog/preview":
            self._preview()
        elif path == "/api/catalog/stage":
            self._stage()
        else:
            raise _HTTPError(404, "not found")

    def _read_body(self, limit, content_type):
        if "Transfer-Encoding" in self.headers or "Content-Encoding" in self.headers:
            raise _HTTPError(400, "encoded request bodies are not supported")
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            raise _HTTPError(411, "Content-Length is required")
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]):
            raise _HTTPError(400, "invalid Content-Length")
        digits = lengths[0].lstrip("0") or "0"
        if len(digits) > len(str(limit)) or int(digits) > limit:
            raise _HTTPError(413, "request body is too large")
        media = self.headers.get_all("Content-Type", [])
        if len(media) != 1 or self.headers.get_content_type() != content_type:
            raise _HTTPError(415, f"Content-Type must be {content_type}")
        remaining = int(digits)
        deadline = time.monotonic() + _TIMEOUT
        data = bytearray()
        try:
            while remaining:
                self.connection.settimeout(_remaining(deadline))
                chunk = self.rfile.read1(min(remaining, 65536))
                if not chunk:
                    raise _HTTPError(400, "request body is incomplete")
                data.extend(chunk)
                remaining -= len(chunk)
        except TimeoutError as error:
            raise _HTTPError(408, "request body timed out") from error
        finally:
            self.connection.settimeout(_TIMEOUT)
        return media[0], data

    def _body(self, limit):
        _, data = self._read_body(limit, "application/json")
        return self._parse_body(data)

    @staticmethod
    def _parse_body(data):
        try:
            body = _decode_json(data)
        except (ValueError, RecursionError) as error:
            raise _HTTPError(400, "invalid JSON body") from error
        if not isinstance(body, dict):
            raise _HTTPError(400, "request body must be a JSON object")
        return body

    def _submit(self, scope=None):
        body = self._body(_ACTION_LIMIT)
        if body.keys() - {"action", "model", "profile", "from_model", "from_profile"}:
            raise _HTTPError(400, "unexpected action fields")
        action, model, profile = (body.get(key) for key in ("action", "model", "profile"))
        from_model, from_profile = (body.get(key) for key in ("from_model", "from_profile"))
        if not isinstance(action, str) or action not in _ACTIONS or not (
            _identifier(model) and _identifier(profile)
        ):
            raise _HTTPError(400, "valid action, model and profile are required")
        if "from_model" in body or "from_profile" in body:
            if action != "switch" or not (_identifier(from_model) and _identifier(from_profile)):
                raise _HTTPError(400, "only switch accepts a source model and profile pair")
        source = "panel"
        if scope is not None:
            source, actions, models = scope
            if action not in actions or model not in models or (
                action == "switch" and from_model not in models
            ):
                raise _HTTPError(403, "action or model is outside webhook scope")
        try:
            result = self.server.controller.submit(
                action, model, profile, from_model=from_model, from_profile=from_profile, source=source,
            )
        except (ValueError, KeyError) as error:
            raise _HTTPError(400, str(error)) from error
        except RuntimeError as error:
            raise _HTTPError(409, str(error)) from error
        self._json(202, result)

    def _preview(self):
        body = self._body(_CATALOG_LIMIT)
        if body.keys() == {"catalog"}:
            catalog = body["catalog"]
        elif body.keys() == {"url"}:
            catalog = _fetch_catalog(body["url"])
        else:
            raise _HTTPError(400, "provide exactly one of catalog or url")
        checked = self._validate_catalog(catalog)
        summary = {key: len(checked[key]) for key in ("models", "runtimes", "artifacts", "boot")}
        summary["profiles"] = sum(len(model["profiles"]) for model in checked["models"].values())
        self._json(200, {"catalog": checked, "summary": summary})

    def _stage(self):
        body = self._body(_CATALOG_LIMIT)
        if body.keys() != {"catalog"}:
            raise _HTTPError(400, "provide an inline catalog to stage")
        raw = body["catalog"]
        self._validate_catalog(raw)
        try:
            result = self.server.controller.stage_catalog(raw)
        except (ValueError, KeyError) as error:
            raise _HTTPError(400, str(error)) from error
        except RuntimeError as error:
            raise _HTTPError(409, str(error)) from error
        except OSError as error:
            raise _HTTPError(500, "could not persist the staged catalog") from error
        self._json(200, result)

    @staticmethod
    def _validate_catalog(catalog):
        try:
            return validate_catalog(catalog)
        except (ValueError, KeyError, RecursionError) as error:
            raise _HTTPError(400, str(error)) from error

    def _static(self, path):
        root = self.server.ui_dir
        if root is None:
            raise _HTTPError(404, "not found")
        try:
            decoded = unquote(path, errors="strict")
        except UnicodeError as error:
            raise _HTTPError(400, "invalid static path") from error
        segments = decoded.split("/")
        if "\\" in decoded or "\0" in decoded or any(part in {".", ".."} for part in segments):
            raise _HTTPError(404, "not found")
        file = self._within_root(root, root.joinpath(*(part for part in segments if part)))
        if file.is_dir():
            file = self._within_root(root, file / "index.html")
        if not file.is_file() and not Path(decoded).suffix and "text/html" in self.headers.get("Accept", ""):
            file = self._within_root(root, root / "index.html")
        if not file.is_file():
            raise _HTTPError(404, "not found")
        try:
            stream = file.open("rb")
        except OSError as error:
            raise _HTTPError(404, "not found") from error
        with stream:
            length = file.stat().st_size
            self._headers(200, mimetypes.guess_type(file.name)[0] or "application/octet-stream", length)
            if self.command != "HEAD":
                shutil.copyfileobj(stream, self.wfile)

    @staticmethod
    def _within_root(root, file):
        try:
            resolved = file.resolve()
        except (OSError, RuntimeError) as error:
            raise _HTTPError(404, "not found") from error
        if not resolved.is_relative_to(root):
            raise _HTTPError(404, "not found")
        return resolved

    def _headers(self, status, media, length):
        if self.inference_log is not None:
            self.inference_log.headers(status, media)
        self.send_response(status)
        self.send_header("Content-Type", media)
        if length is not None:
            self.send_header("Content-Length", str(length))
        else:
            self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        if status == 401:
            self.send_header("WWW-Authenticate", 'Bearer realm="lmh"')
        self.end_headers()

    def _json(self, status, data):
        encoded = json.dumps(data, allow_nan=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(encoded))
        if self.command != "HEAD":
            self._write_body(encoded, complete=True)
        else:
            self._complete_body()


def make_server(
    controller, host: str = "127.0.0.1", port: int | None = None,
    ui_dir: Path | None = None, api_token: str | None = None, webhooks: dict | None = None,
    inference_log_dir: Path | None = None, *, role="panel", inference_keys=None, public_hostname=None,
) -> ThreadingHTTPServer:
    """Build an HTTP server without starting it. Non-loopback requires a token.

    The transport does not encrypt credentials. Prefer loopback, or provide a
    separate trusted tunnel if the panel must be used from elsewhere.
    """
    if host == "localhost":
        host = "127.0.0.1"
    local = _loopback(host)
    defaults = {"panel": PANEL_PORT, "inference": INFERENCE_PORT, "webhook": WEBHOOK_PORT}
    if role not in defaults:
        raise ValueError("Unknown listener role")
    if port is None:
        port = defaults[role]
    if api_token is not None:
        _credential(api_token)
    if not local and (role != "panel" or api_token is None):
        raise ValueError("Non-loopback binding is only supported for a panel with an operator token.")
    scopes = _webhook_config(webhooks, api_token)
    root = Path(ui_dir).resolve() if ui_dir is not None else None
    return _Server((host, port), controller, local, root, api_token, scopes, inference_log_dir,
                   role, inference_policy(inference_keys, public_hostname))
