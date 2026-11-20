"""Validate and, only during explicit setup, repair or replace a local catalog."""

import argparse
import copy
import json
import shutil
import sys
import time
from pathlib import Path

from .catalog import CatalogError, _invalid_constant, _unique_object, load_catalog, validate_catalog


def prepare(path, starter):
    # Fill only known stock recipes; a custom model's I/O must be declared by its owner.
    with path.open(encoding="utf-8") as handle:
        raw = json.load(handle, parse_constant=_invalid_constant, object_pairs_hook=_unique_object)
    models = raw.get("models") if isinstance(raw, dict) else None
    if isinstance(models, dict):
        for name, model in models.items():
            known = starter["models"].get(name)
            if not known or not isinstance(model, dict) or "io" in model:
                continue
            recipe = {key: value for key, value in known.items() if key != "io"}
            if model == recipe and _stock_dependencies(recipe, raw, starter):
                model["io"] = copy.deepcopy(known["io"])
    return validate_catalog(raw)


def _stock_dependencies(recipe, raw, starter):
    runtimes, artifacts = raw.get("runtimes"), raw.get("artifacts")
    if not isinstance(runtimes, dict) or not isinstance(artifacts, dict):
        return False
    for profile in recipe["profiles"].values():
        runtime = profile["runtime"]
        if runtimes.get(runtime) != starter["runtimes"][runtime]:
            return False
        for refs in profile["artifacts"].values():
            for artifact in refs if isinstance(refs, list) else [refs]:
                if artifacts.get(artifact) != starter["artifacts"][artifact]:
                    return False
    return True


def apply(draft, starter, config, state, mode):
    from .controller import _check_config, _write_json, boot_id
    from .state import State

    chosen = _check_config(load_catalog(draft))
    bundled = load_catalog(starter)
    current_boot = (state / "state.sqlite3").is_file() and State(state).get("boot_id") == boot_id()
    saved, pending = state / "current-models.json", state / "pending-models.json"
    replace = mode in {"starter", "import"}
    repairs = {}
    for path in (saved, pending):
        if not path.is_file() or path == pending and replace:
            continue
        try:
            load_catalog(path)
        except CatalogError:
            if current_boot and path == saved and replace:
                repairs[path] = chosen
            elif path == pending or current_boot:
                repairs[path] = _check_config(prepare(path, bundled))

    def backup(path):
        if path.is_file():
            target = path.with_name(f"{path.name}.backup-{time.time_ns()}")
            shutil.copyfile(path, target)
            target.chmod(0o600)
            print(f"Backed up {path.name} to {target}")

    try:
        unchanged = config.is_file() and _check_config(load_catalog(config)) == chosen
    except (CatalogError, ValueError):
        unchanged = False
    if not unchanged:
        backup(config)
        _write_json(config, chosen)
    if replace and pending.is_file():
        backup(pending)
        pending.unlink()
    if current_boot and not saved.is_file():
        repairs[saved] = chosen
    for path, catalog in repairs.items():
        backup(path)
        _write_json(path, catalog)
    if current_boot and saved not in repairs and not unchanged:
        print("Catalog saved for the next Linux boot; the running catalog was left untouched.")
    else:
        print("Catalog is valid and ready.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "apply"))
    parser.add_argument("starter", type=Path)
    parser.add_argument("path", type=Path)
    parser.add_argument("config", type=Path)
    parser.add_argument("state", type=Path)
    parser.add_argument("mode", choices=("keep", "starter", "import"))
    parser.add_argument("--input", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.operation == "prepare":
            chosen = (
                args.starter if args.mode == "starter" or args.mode == "keep" and not args.config.is_file()
                else args.input if args.mode == "import" else args.config
            )
            catalog = prepare(chosen, load_catalog(args.starter))
            args.path.write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")
            print(f"Catalog checked: {len(catalog['models'])} model(s).")
        else:
            apply(args.path, args.starter, args.config, args.state, args.mode)
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        print(f"LocalModelHub catalog: {exc}. No model was stopped. "
              "To choose another catalog, rerun with --catalog FILE or --starter.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
