"""Read versioned model catalogs as data; execution policy lives in the controller."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path


class CatalogError(ValueError):
    """The catalog cannot be read or does not conform to the schema."""


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_COMMIT = re.compile(r"[0-9a-fA-F]{40}")
_DIGEST = re.compile(r"[0-9a-fA-F]{64}")
_REPO_PART = re.compile(r"[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_])?")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _object(value: object, where: str) -> dict:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise CatalogError(f"{where} must be an object with string keys")
    return value


def _only(obj: dict, keys: set[str], where: str) -> None:
    if obj.keys() - keys:
        raise CatalogError(f"{where} has unsupported fields (allowed: {', '.join(sorted(keys))})")


def _identifier(value: object, where: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise CatalogError(f"{where} must be an identifier (letters, digits, '_', '-', '.')")
    return value


def _named(value: object, where: str):
    for key, entry in _object(value, where).items():
        name = _identifier(key, f"{where} key")
        yield name, _object(entry, f"{where}.{name}")


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip() or _CONTROL.search(value):
        raise CatalogError(f"{where} must be a nonempty string without control characters")
    return value


def _validate_artifact(raw: object, where: str = "artifact") -> dict:
    """Normalize one artifact, including calls made independently of a catalog."""
    obj = _object(raw, where)
    _only(obj, {"source", "hashes", "size"}, where)
    source = _object(obj.get("source"), f"{where}.source")
    kind = source.get("kind")
    if kind == "local":
        _only(source, {"kind", "path"}, f"{where}.source")
        path = _text(source.get("path"), f"{where}.source.path")
        if not Path(path).is_absolute():
            raise CatalogError(f"{where}.source.path must be absolute on this host")
        normalized_source = {"kind": "local", "path": path}
    elif kind == "hf":
        _only(source, {"kind", "repo", "revision", "file"}, f"{where}.source")
        repo = _text(source.get("repo"), f"{where}.source.repo")
        parts = repo.split("/")
        if (len(parts) > 2 or len(parts[-1]) > 96 or ".." in repo or "--" in repo
                or repo.endswith(".git") or not all(_REPO_PART.fullmatch(part) for part in parts)):
            raise CatalogError(f"{where}.source.repo must be a Hugging Face model repository ID")
        revision = source.get("revision")
        if not isinstance(revision, str) or not _COMMIT.fullmatch(revision):
            raise CatalogError(f"{where}.source.revision must be a full 40-digit Git commit hash")
        file = _text(source.get("file"), f"{where}.source.file")
        if (any(part in {"", ".", ".."} for part in file.split("/"))
                or any(char in file for char in "\\:%?#")):
            raise CatalogError(f"{where}.source.file must be an exact repository-relative file path")
        normalized_source = {"kind": "hf", "repo": repo, "revision": revision.lower(), "file": file}
    else:
        raise CatalogError(f"{where}.source.kind must be 'local' or 'hf'")

    normalized = {"source": normalized_source}
    if "size" in obj:
        size = obj["size"]
        if type(size) is not int or size < 0:
            raise CatalogError(f"{where}.size must be a nonnegative integer in bytes")
        normalized["size"] = size
    if "hashes" in obj:
        hashes = _object(obj["hashes"], f"{where}.hashes")
        _only(hashes, {"sha256", "blake3"}, f"{where}.hashes")
        normalized["hashes"] = {}
        for algorithm, digest in hashes.items():
            if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                raise CatalogError(f"{where}.hashes.{algorithm} must be a 64-digit hex digest")
            normalized["hashes"][algorithm] = digest.lower()
    return normalized


def _reference(value: object, entries: dict, where: str) -> str:
    name = _identifier(value, where)
    if name not in entries:
        raise CatalogError(f"{where} references an unknown ID")
    return name


def validate_catalog(raw: object) -> dict:
    """Return a normalized schema-v1 catalog without mutating the input.

    Runtime and profile extension fields are opaque adapter configuration.
    Catalog validation does not authorize adapter names, commands or local paths.
    """
    obj = _object(raw, "catalog")
    _only(obj, {"schema_version", "runtimes", "artifacts", "models", "boot"}, "catalog")
    if type(obj.get("schema_version")) is not int or obj["schema_version"] != 1:
        raise CatalogError("schema_version must be 1")

    runtimes = {}
    for name, runtime in _named(obj.get("runtimes"), "runtimes"):
        _identifier(runtime.get("adapter"), f"runtimes.{name}.adapter")
        runtimes[name] = deepcopy(runtime)

    artifacts = {
        name: _validate_artifact(artifact, f"artifacts.{name}")
        for name, artifact in _named(obj.get("artifacts"), "artifacts")
    }
    models = {}
    for name, model in _named(obj.get("models"), "models"):
        where = f"models.{name}"
        _only(model, {"name", "profiles", "default_profile"}, where)
        label = _text(model.get("name"), f"{where}.name")
        profiles = {}
        for profile_id, profile in _named(model.get("profiles"), f"{where}.profiles"):
            location = f"{where}.profiles.{profile_id}"
            _reference(profile.get("runtime"), runtimes, f"{location}.runtime")
            if profile.get("device") not in ("cpu", "gpu"):
                raise CatalogError(f"{location}.device must be 'cpu' or 'gpu'")
            roles = _object(profile.get("artifacts"), f"{location}.artifacts")
            for role, refs in roles.items():
                _identifier(role, f"{location}.artifacts role")
                field = f"{location}.artifacts.{role}"
                if isinstance(refs, list):
                    if not refs:
                        raise CatalogError(f"{field} shard list must not be empty")
                    shards = [_reference(ref, artifacts, field) for ref in refs]
                    if len(shards) != len(set(shards)):
                        raise CatalogError(f"{field} shard list must not repeat artifact IDs")
                else:
                    _reference(refs, artifacts, field)
            profiles[profile_id] = deepcopy(profile)
        if not profiles:
            raise CatalogError(f"{where}.profiles must not be empty")
        models[name] = {"name": label, "profiles": profiles}
        if "default_profile" in model:
            models[name]["default_profile"] = _reference(
                model["default_profile"], profiles, f"{where}.default_profile"
            )

    boot = obj.get("boot", [])
    if not isinstance(boot, list):
        raise CatalogError("boot must be an array")
    normalized_boot = []
    for index, entry in enumerate(boot):
        where = f"boot[{index}]"
        item = _object(entry, where)
        _only(item, {"model", "profile"}, where)
        model_id = _reference(item.get("model"), models, f"{where}.model")
        model = models[model_id]
        if "profile" in item:
            _reference(item["profile"], model["profiles"], f"{where}.profile")
        elif "default_profile" not in model and len(model["profiles"]) != 1:
            raise CatalogError(f"{where}.profile is required when the model has no unambiguous default")
        normalized_boot.append(dict(item))

    return {
        "schema_version": 1, "runtimes": runtimes, "artifacts": artifacts,
        "models": models, "boot": normalized_boot,
    }


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise CatalogError("catalog JSON contains a duplicate object key")
        obj[key] = value
    return obj


def _invalid_constant(_value: str) -> None:
    raise CatalogError("catalog JSON contains a nonstandard numeric constant")


def load_catalog(path: str | Path) -> dict:
    """Load a UTF-8 JSON model catalog from a local file."""
    try:
        with Path(path).open(encoding="utf-8") as handle:
            raw = json.load(
                handle, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
            )
    except json.JSONDecodeError as exc:
        raise CatalogError(f"invalid catalog JSON at line {exc.lineno}, column {exc.colno}") from None
    except (OSError, UnicodeError, RecursionError, ValueError) as exc:
        if isinstance(exc, CatalogError):
            raise
        raise CatalogError("unable to read catalog as UTF-8 JSON") from None
    return validate_catalog(raw)
