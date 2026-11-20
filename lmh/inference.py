"""OpenAI-compatible routes over ready, explicitly configured local engines."""

import http.client
import json
import math
import re
import sqlite3
import uuid
from email import policy
from email.parser import BytesParser

from .language_stats import OutputSample


class InferenceError(Exception):
    def __init__(self, status, message, code="invalid_request"):
        super().__init__(message)
        self.status = status
        self.code = code


JSON_ROUTES = {
    "/v1/chat/completions": "text",
    "/v1/embeddings": "embedding",
    "/v1/images/generations": "image",
    "/v1/audio/speech": "audio",
}
UPLOAD_ROUTES = {
    "/v1/images/edits": ({"text", "image"}, "image"),
    "/v1/audio/transcriptions": ({"audio"}, "text"),
    "/v1/audio/translations": ({"audio"}, "text"),
    "/v1/videos": ({"text"}, "video"),
}
REQUEST_LIMIT = 32 * 1024 * 1024
RESPONSE_LIMIT = 64 * 1024 * 1024
TIMEOUT = 120
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_INLINE_MEDIA = re.compile(r"data:(image|audio|video)/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=\r\n]+")
_SAFE_MEDIA = re.compile(r"(?:application|audio|image|text|video)/[A-Za-z0-9.+-]+")
_CHAT_PARTS = {"text": "text", "image_url": "image", "input_audio": "audio", "video_url": "video"}
_BASE64 = re.compile(r"[A-Za-z0-9+/=\r\n]+")
_IMAGE_JSON_FIELDS = {
    "model", "prompt", "n", "size", "quality", "style", "response_format",
    "output_format", "output_compression", "background", "moderation", "user",
}
_UPLOAD_FIELDS = {
    "/v1/images/edits": _IMAGE_JSON_FIELDS | {"image", "image[]", "mask", "input_fidelity"},
    "/v1/audio/transcriptions": {"model", "file", "language", "prompt", "response_format",
                                 "temperature", "timestamp_granularities[]", "stream", "chunking_strategy",
                                 "include[]"},
    "/v1/audio/translations": {"model", "file", "prompt", "response_format", "temperature"},
    "/v1/videos": {"model", "prompt", "input_reference", "seconds", "size"},
}


def model_id(value):
    if not _valid_id(value):
        raise InferenceError(400, "Use a model ID from /v1/models.", "invalid_model")
    return value


def _valid_id(value):
    return isinstance(value, str) and len(value) <= 256 and _ID.fullmatch(value) is not None


def json_io(path, body):
    output = JSON_ROUTES[path]
    inputs = {"text"}
    if path != "/v1/chat/completions":
        if path == "/v1/images/generations" and body.keys() - _IMAGE_JSON_FIELDS:
            raise InferenceError(400, "Image generation accepts only OpenAI-compatible fields.")
        return inputs, output
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise InferenceError(400, "messages must be a nonempty array.")
    inputs = set()
    for message in messages:
        if not isinstance(message, dict):
            raise InferenceError(400, "Each message must be an object.")
        content = message.get("content")
        if isinstance(content, str) and content:
            inputs.add("text")
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict) or not isinstance(part.get("type"), str):
                    raise InferenceError(400, "Invalid message content part.")
                key = part["type"]
                if key not in _CHAT_PARTS:
                    raise InferenceError(400, "Unsupported message content part.")
                kind = _CHAT_PARTS[key]
                if kind == "text" and (not isinstance(part.get("text"), str) or not part["text"]):
                    raise InferenceError(400, "Text parts require nonempty text.")
                if kind == "audio":
                    audio = part.get("input_audio")
                    data = audio.get("data") if isinstance(audio, dict) else None
                    if not isinstance(data, str) or not _BASE64.fullmatch(data):
                        raise InferenceError(400, "Audio parts require inline base64 data.")
                if key in {"image_url", "video_url"}:
                    media = part.get(key)
                    url = media.get("url") if isinstance(media, dict) else None
                    match = _INLINE_MEDIA.fullmatch(url) if isinstance(url, str) else None
                    if match is None or match[1] != kind:
                        raise InferenceError(400, "Use an inline base64 media data URL, not a remote URL.")
                inputs.add(kind)
        elif content is not None and not isinstance(content, str):
            raise InferenceError(400, "Invalid message content.")
    if not inputs:
        raise InferenceError(400, "messages must contain model input.")
    if body.get("modalities") == ["audio"]:
        output = "audio"
    elif "modalities" in body and body["modalities"] != ["text"]:
        raise InferenceError(400, "Select a single text or audio output modality.")
    return inputs, output


def supports(pairs, inputs, output, body=None):
    outputs = {pair["output"] for pair in pairs if set(pair["input"]) == inputs}
    if output in outputs:
        return True
    response_format = body.get("response_format") if isinstance(body, dict) else None
    schema = response_format.get("json_schema") if isinstance(response_format, dict) else None
    return (output == "text" and "decision" in outputs and isinstance(schema, dict)
            and response_format.get("type") == "json_schema" and schema.get("strict") is True
            and isinstance(schema.get("schema"), dict))


def parse_upload(media, raw, path):
    if not isinstance(media, str) or not media.isascii() or any(c in media for c in "\r\n"):
        raise InferenceError(415, "Expected multipart/form-data.")
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + media.encode("ascii") + b"\r\n\r\n" + raw)
    if (message.get_content_type() != "multipart/form-data" or not message.is_multipart()
            or message.defects):
        raise InferenceError(400, "Invalid multipart form.")
    fields = []
    models = []
    for part in message.iter_parts():
        name = part.get_param("name", header="Content-Disposition")
        filename = part.get_filename()
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+(?:\[\])?", name)
                or part.get_content_disposition() != "form-data" or part.is_multipart()
                or part.defects or part.get("Content-Transfer-Encoding")):
            raise InferenceError(400, "Invalid multipart field.")
        if name not in _UPLOAD_FIELDS[path]:
            raise InferenceError(400, "Unsupported multipart field.")
        data = part.get_payload(decode=True)
        if name == "model":
            if filename is not None or len(data) > 512:
                raise InferenceError(400, "Invalid multipart model field.")
            models.append(data.decode("ascii", errors="replace"))
        elif filename is None:
            fields.append((name, None, None, data))
        else:
            extension = re.search(r"\.[A-Za-z0-9]{1,10}$", filename)
            content_type = part.get_content_type()
            fields.append((name, "upload" + (extension[0] if extension else ""),
                           content_type if _SAFE_MEDIA.fullmatch(content_type) else "application/octet-stream", data))
        if len(fields) > 64:
            raise InferenceError(400, "Too many multipart fields.")
    if len(models) != 1:
        raise InferenceError(400, "Exactly one model field is required.")
    if path != "/v1/videos":
        assets = {"image", "image[]"} if path == "/v1/images/edits" else {"file"}
        if not any(name in assets and filename and data for name, filename, _, data in fields):
            raise InferenceError(400, "Upload the input media as a file.")
    if path in {"/v1/images/edits", "/v1/videos"} and not any(
        name == "prompt" and filename is None and data.strip() for name, filename, _, data in fields
    ):
        raise InferenceError(400, "A text prompt is required.")
    if path == "/v1/videos" and any(
        name == "input_reference" and (not filename or not data) for name, filename, _, data in fields
    ):
        raise InferenceError(400, "Upload input_reference as a file.")
    return model_id(models[0]), fields


def encode_upload(fields, alias):
    boundary = "lmh-" + uuid.uuid4().hex
    parts = []
    for name, filename, media, data in [("model", None, None, alias.encode()), *fields]:
        header = f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
        if filename:
            header += f'; filename="{filename}"\r\nContent-Type: {media}'
        parts.extend((header.encode("ascii"), b"\r\n\r\n", data, b"\r\n"))
    parts.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def upstream_alias(target, model):
    """Single-model engines may advertise a filename or a different alias."""
    conn = http.client.HTTPConnection(*target, timeout=5)
    try:
        conn.request("GET", "/v1/models")
        with conn.getresponse() as response:
            if response.status in {404, 405, 501}:
                return model
            if response.status != 200:
                raise InferenceError(502, "Could not resolve the engine's model.", "upstream_error")
            raw = response.read(1024 * 1024 + 1)
            ids = {entry["id"] for entry in json.loads(raw)["data"]
                   if isinstance(entry["id"], str) and entry["id"]}
        if model in ids or not ids:
            return model
        if len(ids) == 1:
            return ids.pop()
        raise InferenceError(502, "The engine does not advertise this model ID.", "upstream_error")
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError, RecursionError):
        raise InferenceError(502, "Could not resolve the engine's model.", "upstream_error") from None
    finally:
        conn.close()


def _record(handler, model, sample):
    try:
        sample.record(handler.server.controller.state, model)
    except (AttributeError, OSError, sqlite3.Error):
        # Telemetry is best effort and must not interrupt inference.
        pass


def _sse(handler, response, model):
    sample = OutputSample()
    total = 0
    while line := response.readline(RESPONSE_LIMIT + 1 - total):
        total += len(line)
        if total > RESPONSE_LIMIT or not line.endswith(b"\n"):
            return
        done = False
        if line.startswith(b"data:"):
            value = line[5:].strip()
            if value == b"[DONE]":
                done = True
            else:
                try:
                    event = json.loads(value)
                    if isinstance(event, dict):
                        sample.feed(event, streaming=True)
                        if "model" in event:
                            event["model"] = model
                            line = b"data: " + json.dumps(event).encode() + b"\n"
                except (ValueError, RecursionError):
                    pass
        if done:
            # The event is complete only after the blank line SSE delimiter.
            _record(handler, model, sample)
            handler._write_body(line + b"\n", complete=True)
            handler.wfile.flush()
            return
        handler._write_body(line)
        handler.wfile.flush()


def relay(handler, target, path, payload, media, model, *, video_id=None):
    conn = http.client.HTTPConnection(*target, timeout=TIMEOUT)
    started = False
    try:
        headers = {"Accept-Encoding": "identity"}
        if media:
            headers["Content-Type"] = media
        conn.request(handler.command, path, body=payload, headers=headers)
        with conn.getresponse() as response:
            if response.status < 200 or 300 <= response.status < 400 or response.getheader("Content-Encoding"):
                raise InferenceError(502, "Unsupported engine response.", "upstream_error")
            content_type = response.headers.get_content_type()
            if content_type == "text/event-stream" and response.status == 200:
                handler.close_connection = True
                handler._headers(200, content_type, None)
                started = True
                _sse(handler, response, model)
                return
            data = response.read(RESPONSE_LIMIT + 1)
            if len(data) > RESPONSE_LIMIT:
                raise InferenceError(502, "Engine response exceeded the size limit.", "upstream_error")
            if content_type == "application/json":
                result = json.loads(data, parse_constant=_invalid_constant, parse_float=_finite)
                if not isinstance(result, dict):
                    raise InferenceError(502, "Invalid engine response.", "upstream_error")
                if response.status >= 400:
                    error = result.get("error")
                    if not isinstance(error, dict) or not isinstance(error.get("message"), str):
                        message = result.get("message", error)
                        raise InferenceError(response.status,
                                             (message[:500] if isinstance(message, str)
                                              else "The engine rejected the request."), "upstream_error")
                else:
                    if path == "/v1/videos":
                        if not _valid_id(result.get("id")):
                            raise InferenceError(502, "Engine did not return a valid video ID.", "upstream_error")
                        result["id"] = handler.server.controller.state.add_video(model, target, result["id"])
                    elif video_id is not None:
                        result["id"] = video_id
                    if "model" in result:
                        result["model"] = model
                    sample = OutputSample()
                    sample.feed(result)
                    _record(handler, model, sample)
                started = True
                handler._json(response.status, result)
            elif response.status < 300 and (
                content_type in {"text/plain", "application/octet-stream"}
                or _SAFE_MEDIA.fullmatch(content_type) and content_type.startswith(("audio/", "video/"))
            ):
                handler._headers(response.status, content_type, len(data))
                started = True
                if content_type == "text/plain" and path.startswith("/v1/audio/"):
                    sample = OutputSample()
                    sample.add(0, data.decode("utf-8", errors="replace"))
                    _record(handler, model, sample)
                handler._write_body(data, complete=True)
            else:
                raise InferenceError(response.status if response.status >= 400 else 502,
                                     "The engine rejected the request or returned unsupported data.", "upstream_error")
    except (BrokenPipeError, ConnectionResetError):
        if not started:
            raise
    except (OSError, http.client.HTTPException, ValueError, RecursionError, sqlite3.Error):
        if not started:
            raise InferenceError(502, "Could not complete the request to the local engine.", "upstream_error") from None
    finally:
        conn.close()


def _invalid_constant(_):
    raise ValueError("Non-finite JSON number")


def _finite(value):
    number = float(value)
    if not math.isfinite(number):
        return _invalid_constant(value)
    return number
