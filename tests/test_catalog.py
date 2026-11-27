import copy
import tempfile
import unittest
from pathlib import Path

from lmh.catalog import CatalogError, load_catalog, validate_catalog


def sample():
    return {
        "schema_version": 1,
        "runtimes": {"native": {"adapter": "llama.cpp", "options": {"threads": 4}}},
        "artifacts": {
            "first": {
                "source": {"kind": "hf", "repo": "org/repo", "revision": "A" * 40,
                           "file": "folder/first.gguf"},
                "hashes": {"sha256": "B" * 64, "blake3": "C" * 64},
                "size": 100,
            },
            "second": {"source": {"kind": "local", "path": "/placeholder/second.gguf"}},
        },
        "models": {
            "m": {
                "name": "My Model",
                "io": [{"input": ["text"], "output": "text"}],
                "profiles": {
                    "p": {"runtime": "native", "device": "gpu",
                          "artifacts": {"model": ["first", "second"], "aux": "second"}},
                },
                "default_profile": "p",
            },
        },
        "boot": [{"model": "m"}],
    }


class CatalogTest(unittest.TestCase):
    def test_example(self):
        directory = Path(__file__).resolve().parents[1] / "catalogs"
        for example in directory.glob("*.json"):
            with self.subTest(example=example):
                self.assertTrue(load_catalog(example)["models"])

    def test_model_types_reject_missing_unknown_repeated_and_flattened_pairs(self):
        for invalid in (None, [], [{"input": "text", "output": "text"}],
                        [{"input": ["Text"], "output": "text"}],
                        [{"input": ["text", "text"], "output": "text"}],
                        [{"input": ["text"], "output": "text image"}],
                        [{"input": ["text", "image"], "output": "text"},
                         {"input": ["image", "text"], "output": "text"}]):
            raw = sample()
            raw["models"]["m"]["io"] = invalid
            with self.subTest(io=invalid), self.assertRaisesRegex(CatalogError, r"models\.m\.io"):
                validate_catalog(raw)

    def test_custom_media_types_are_declarative(self):
        raw = sample()
        raw["models"]["m"]["io"] = [{"input": ["text", "document"], "output": "json"},
                                    {"input": ["audio"], "output": "text"}]
        normalized = validate_catalog(raw)
        self.assertEqual(normalized["models"]["m"]["io"], [
            {"input": ["audio"], "output": "text"},
            {"input": ["document", "text"], "output": "json"},
        ])

    def test_normalizes_without_mutation_and_preserves_single_and_sharded_roles(self):
        raw = sample()
        previous = copy.deepcopy(raw)
        normalized = validate_catalog(raw)
        first = normalized["artifacts"]["first"]
        self.assertEqual(first["source"]["revision"], "a" * 40)
        self.assertEqual(first["hashes"], {"sha256": "b" * 64, "blake3": "c" * 64})
        roles = normalized["models"]["m"]["profiles"]["p"]["artifacts"]
        self.assertEqual(roles, {"model": ["first", "second"], "aux": "second"})
        self.assertEqual(normalized["boot"], [{"model": "m"}])
        normalized["runtimes"]["native"]["options"]["threads"] = 9
        roles["model"].reverse()
        self.assertEqual(raw, previous)
        self.assertEqual(validate_catalog(normalized), normalized)

    def test_rejects_malformed_remote_sources_and_integrity_fields(self):
        for key, value, field in [
            ("revision", "main", "revision"),
            ("revision", "abc0123", "revision"),
            ("repo", "https://example.com/org/repo", "repo"),
            ("file", "/model.gguf", "file"),
            ("file", "folder/../model.gguf", "file"),
            ("file", "folder/%2e%2e/model.gguf", "file"),
            ("file", "secret.gguf?token=private", "file"),
        ]:
            with self.subTest(key=key, value=value):
                raw = sample()
                raw["artifacts"]["first"]["source"][key] = value
                with self.assertRaisesRegex(CatalogError, rf"source\.{field}") as caught:
                    validate_catalog(raw)
                self.assertNotIn(value, str(caught.exception))
        for key, value in [("size", True), ("size", -1), ("hashes", {"sha256": "not hex"})]:
            with self.subTest(key=key, value=value):
                raw = sample()
                raw["artifacts"]["first"][key] = value
                with self.assertRaisesRegex(CatalogError, key):
                    validate_catalog(raw)
        raw = sample()
        raw["artifacts"]["second"]["source"]["path"] = "relative/model.gguf"
        with self.assertRaisesRegex(CatalogError, "path must be absolute"):
            validate_catalog(raw)

    def test_rejects_bad_references_and_empty_or_duplicate_shard_lists(self):
        for section, key, value in [
            ("profile", "runtime", "unknown"),
            ("profile", "device", "tpu"),
            ("profile", "artifacts", {"model": "unknown"}),
            ("profile", "artifacts", {"model": ["first", "unknown"]}),
            ("profile", "artifacts", {"model": ["first", "first"]}),
            ("profile", "artifacts", {"model": []}),
            ("model", "default_profile", "unknown"),
            ("boot", "model", "unknown"),
            ("boot", "profile", "unknown"),
        ]:
            with self.subTest(section=section, key=key, value=value):
                raw = sample()
                model = raw["models"]["m"]
                target = {"profile": model["profiles"]["p"], "model": model, "boot": raw["boot"][0]}
                target[section][key] = value
                with self.assertRaisesRegex(CatalogError, key):
                    validate_catalog(raw)

    def test_boot_requires_a_profile_if_no_unambiguous_default(self):
        raw = sample()
        model = raw["models"]["m"]
        del model["default_profile"]
        self.assertEqual(validate_catalog(raw)["boot"], [{"model": "m"}])
        model["profiles"]["q"] = model["profiles"]["p"]
        with self.assertRaisesRegex(CatalogError, "no unambiguous default"):
            validate_catalog(raw)
        raw["boot"][0]["profile"] = "q"
        self.assertEqual(validate_catalog(raw)["boot"], [{"model": "m", "profile": "q"}])

    def test_exact_version_and_strict_json(self):
        for version in [None, True, 1.0, "1", 2]:
            raw = sample()
            raw["schema_version"] = version
            with self.subTest(version=version), self.assertRaisesRegex(CatalogError, "schema_version"):
                validate_catalog(raw)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "models.json"
            for content, message in [
                ('{"secret-value":1,"secret-value":2}', "duplicate"),
                ('{"secret-value":NaN}', "nonstandard"),
                ('{"secret-value":}', "line 1"),
            ]:
                with self.subTest(message=message):
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaisesRegex(CatalogError, message) as caught:
                        load_catalog(path)
                    self.assertNotIn("secret-value", str(caught.exception))
