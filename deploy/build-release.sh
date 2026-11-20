#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "Usage: bash deploy/build-release.sh [--build-ui] [--output FILE]"
}

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
output="$root/dist/localmodelhub.tar.gz"
build_ui=false
while (($#)); do
    case "$1" in
        --build-ui) build_ui=true; shift ;;
        --output) (($# >= 2)) || { usage >&2; exit 2; }; output=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

if "$build_ui"; then
    (
        cd "$root/web"
        if [[ -f bun.lock ]] && command -v bun >/dev/null 2>&1; then
            bun install --frozen-lockfile
        elif [[ -f package-lock.json ]]; then
            npm ci
        else
            echo "A frozen web build requires Bun for the checked-in bun.lock." >&2
            exit 1
        fi
        npm run build
    )
fi
if [[ ! -s "$root/web/build/index.html" ]] ||
    [[ -z $(find "$root/web/build" -type f -name '*.js' -print -quit) ]]; then
    echo "Missing web/build/index.html or JavaScript assets; build the UI first or pass --build-ui." >&2
    exit 1
fi
if [[ ! -f "$root/lmh/cli.py" || ! -f "$root/lmh/server.py" ]]; then
    echo "Missing Python entry point sources; build from a complete checkout." >&2
    exit 1
fi

scratch=$(mktemp -d)
trap 'rm -rf -- "$scratch"' EXIT
bundle="$scratch/localmodelhub"
mkdir -p -- "$bundle/deploy" "$bundle/wheels" "$bundle/web"
cp -- "$root/README.md" "$root/LICENSE" "$bundle/"
cp -- "$root/deploy/install.sh" "$root/deploy/localmodelhub.service" \
    "$root/deploy/localmodelhub-update.service" "$root/deploy/localmodelhub-update.timer" \
    "$root/deploy/uninstall.sh" "$root/deploy/requirements.lock" \
    "$root/deploy/example-catalog.json" "$bundle/deploy/"
cp -R -- "$root/web/build" "$bundle/web/"
mkdir -- "$bundle/catalogs"
cp -- "$root/catalogs/models.example.json" "$root/catalogs/starter.json" "$bundle/catalogs/"

if command -v uv >/dev/null 2>&1; then
    (cd "$root" && uv build --wheel --out-dir "$bundle/wheels")
else
    python3 -m venv "$scratch/build-venv"
    "$scratch/build-venv/bin/python" -m pip wheel --no-deps --wheel-dir "$bundle/wheels" "$root"
fi

(
    cd "$bundle"
    find wheels web/build deploy catalogs README.md LICENSE -type f -print0 |
        sort -z |
        xargs -0 sha256sum > SHA256SUMS
)
mkdir -p -- "$(dirname -- "$output")"
tar -czf "$output" -C "$scratch" localmodelhub
sha256sum -- "$output"
