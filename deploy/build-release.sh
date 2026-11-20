#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "Usage: bash deploy/build-release.sh [--build-ui] [--terminal-dir DIR] [--output FILE]"
}

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
output="$root/dist/localmodelhub.tar.gz"
build_ui=false
terminal_dir=
while (($#)); do
    case "$1" in
        --build-ui) build_ui=true; shift ;;
        --terminal-dir) (($# >= 2)) || { usage >&2; exit 2; }; terminal_dir=$2; shift 2 ;;
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
if [[ ! -f "$root/terminal/Cargo.toml" || ! -f "$root/lmh/server.py" ]]; then
    echo "Missing Python entry point sources; build from a complete checkout." >&2
    exit 1
fi

scratch=$(mktemp -d)
trap 'rm -rf -- "$scratch"' EXIT
bundle="$scratch/localmodelhub"
mkdir -p -- "$bundle/deploy" "$bundle/wheels" "$bundle/web"
mkdir -- "$bundle/bin"
if [[ -n "$terminal_dir" ]]; then
    for arch in x86_64 aarch64; do
        if [[ ! -s "$terminal_dir/lmh-$arch" ]]; then
            echo "Missing prebuilt terminal binary: $terminal_dir/lmh-$arch" >&2
            exit 1
        fi
        install -m 0755 -- "$terminal_dir/lmh-$arch" "$bundle/bin/lmh-$arch"
    done
else
    arch=$(uname -m)
    case "$arch" in x86_64|aarch64) ;; *) echo "Unsupported Linux architecture: $arch" >&2; exit 1 ;; esac
    cargo build --manifest-path "$root/terminal/Cargo.toml" --locked --release --target-dir "$root/terminal/target"
    install -m 0755 -- "$root/terminal/target/release/lmh" "$bundle/bin/lmh-$arch"
fi
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
    find bin wheels web/build deploy catalogs README.md LICENSE -type f -print0 |
        sort -z |
        xargs -0 sha256sum > SHA256SUMS
)
mkdir -p -- "$(dirname -- "$output")"
tar -czf "$output" -C "$scratch" localmodelhub
sha256sum -- "$output"
