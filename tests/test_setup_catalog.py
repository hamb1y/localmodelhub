import json
import tempfile
import unittest
from pathlib import Path

from lmh.catalog import CatalogError, load_catalog
from lmh.controller import boot_id
from lmh.setup_catalog import apply, prepare
from lmh.state import State


STARTER = Path(__file__).resolve().parents[1] / "catalogs/starter.json"


class SetupCatalogTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.directory / "models.json"
        self.draft = self.directory / "draft.json"
        self.state = self.directory / "state"
        self.bundled = load_catalog(STARTER)

    def write_legacy(self, path):
        old = load_catalog(STARTER)
        for model in old["models"].values():
            del model["io"]
        path.write_text(json.dumps(old))
        return old

    def test_recovers_old_stock_catalog_without_replaying_boot(self):
        state = State(self.state)
        state.put("boot_id", boot_id())
        saved = self.state / "current-models.json"
        for path in (self.config, saved):
            self.write_legacy(path)
        self.draft.write_text(json.dumps(prepare(self.config, self.bundled)))
        apply(self.draft, STARTER, self.config, self.state, "keep")
        self.assertEqual(state.get("boot_id"), boot_id())
        self.assertEqual(load_catalog(self.config)["models"]["qwen2.5-chat"]["io"],
                         [{"input": ["text"], "output": "text"}])
        self.assertEqual(load_catalog(saved), load_catalog(self.config))
        self.assertEqual(len(list(self.directory.glob("models.json.backup-*"))), 1)
        self.assertEqual(len(list(self.state.glob("current-models.json.backup-*"))), 1)

    def test_custom_io_is_not_guessed(self):
        old = self.write_legacy(self.config)
        old["models"]["qwen2.5-chat"]["name"] = "A custom model"
        self.config.write_text(json.dumps(old))
        with self.assertRaisesRegex(CatalogError, r"qwen2\.5-chat\.io"):
            prepare(self.config, self.bundled)

    def test_import_keeps_a_valid_active_snapshot_until_the_next_boot(self):
        State(self.state).put("boot_id", boot_id())
        saved = self.state / "current-models.json"
        saved.write_bytes(STARTER.read_bytes())
        self.config.write_bytes(STARTER.read_bytes())
        self.draft.write_text('{"schema_version":1,"models":{},"artifacts":{},"runtimes":{}}')
        apply(self.draft, STARTER, self.config, self.state, "import")
        self.assertEqual(load_catalog(self.config)["models"], {})
        self.assertEqual(load_catalog(saved), self.bundled)
