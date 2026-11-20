#!/usr/bin/env bash
set -euo pipefail
umask 077

usage() {
    cat <<'EOF'
Usage: bash deploy/install.sh [--catalog FILE] [--no-systemd]

Run from an unpacked LocalModelHub release as the service account (ai), not
root. First install seeds the bundled starter catalog unless --catalog FILE
is given. --no-systemd installs files without contacting or enabling the
user service manager.
EOF
}

die() {
    echo "LocalModelHub install: $*" >&2
    exit 1
}

seed=
systemd=true
while (($#)); do
    case "$1" in
        --catalog) (($# >= 2)) || { usage >&2; exit 2; }; seed=$2; shift 2 ;;
        --no-systemd) systemd=false; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

((EUID != 0)) || die "run as ai, not root; see README.md for one-time admin setup."
[[ $HOME == /* && $HOME != / ]] || die "HOME must be an absolute home directory."
if "$systemd" && [[ $(id -un) != ai ]]; then
    die "the service runs under the dedicated ai account; use --no-systemd for a staging check."
fi
bundle=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
[[ -s "$bundle/SHA256SUMS" && -s "$bundle/web/build/index.html" ]] ||
    die "run from an unpacked release built with deploy/build-release.sh."
arch=$(uname -m)
case "$arch" in x86_64|aarch64) ;; *) die "unsupported Linux architecture: $arch." ;; esac
terminal="$bundle/bin/lmh-$arch"
[[ -s "$terminal" ]] || die "this release does not contain the terminal binary for $arch."
shopt -s nullglob
wheels=("$bundle"/wheels/localmodelhub-*.whl)
((${#wheels[@]} == 1)) || die "expected exactly one LocalModelHub wheel."
(cd "$bundle" && sha256sum --check --quiet SHA256SUMS)
read -r bundle_id _ < <(sha256sum -- "$bundle/SHA256SUMS")
python=$(command -v python3) || die "Python 3.11+ is required."
python_tag=$("$python" -I -c '
import sys
if sys.version_info < (3, 11):
    sys.exit(1)
print("py%d.%d" % sys.version_info[:2])
') ||
    die "Python 3.11+ is required."
id="$bundle_id-$python_tag"

config="$HOME/.config/localmodelhub"
data="$HOME/.local/share/localmodelhub"
unit_dir="$HOME/.config/systemd/user"
unit="$unit_dir/localmodelhub.service"
release="$data/releases/$id"
catalog="$config/models.json"

if [[ ! -f "$catalog" ]]; then
    initial_catalog=${seed:-"$bundle/catalogs/starter.json"}
    [[ -r "$initial_catalog" && -f "$initial_catalog" ]] ||
        die "cannot read initial catalog: $initial_catalog"
fi
if "$systemd"; then
    read -r _ version _ <<< "$(systemctl --version)"
    version=${version%%.*}
    [[ $version =~ ^[0-9]+$ ]] && ((10#$version >= 254)) ||
        die "systemd 254+ is required (systemd-run --expand-environment=no)."
    systemctl --user show-environment >/dev/null ||
        die "no user systemd manager; log in as ai after enabling linger, or use --no-systemd to stage files."
fi

# mkdir -p traverses existing parents without needing to own ~/.local.
mkdir -p -- "$config" "$data/releases" "$data/state" "$unit_dir"
exec 9>"$data/install.lock"
flock 9

scratch=$(mktemp -d "$data/.install.XXXXXXXX")
cleanup_release=
cleanup() {
    rm -rf -- "$scratch"
    if [[ -n "$cleanup_release" ]]; then
        rm -rf -- "$cleanup_release"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ ! -e "$release/.ready" ]]; then
    # Create the virtualenv at its permanent location: entry point shebangs
    # contain this path. A failed build must not change the current symlink.
    mkdir -- "$release" ||
        die "incomplete release at $release; verify it is not current before removing it and retrying."
    cleanup_release=$release
    if command -v uv >/dev/null 2>&1; then
        uv venv --python "$python" "$release/venv"
        uv pip install --python "$release/venv/bin/python" --require-hashes --only-binary :all: \
            -r "$bundle/deploy/requirements.lock"
        uv pip install --python "$release/venv/bin/python" --no-deps "${wheels[0]}"
    else
        "$python" -m venv "$release/venv"
        "$release/venv/bin/python" -m pip install --require-hashes --only-binary :all: \
            -r "$bundle/deploy/requirements.lock"
        "$release/venv/bin/python" -m pip install --no-deps "${wheels[0]}"
    fi
    mkdir -- "$release/web"
    cp -R -- "$bundle/web/build" "$release/web/"
    install -D -m 0600 -- "$bundle/catalogs/starter.json" "$release/catalogs/starter.json"
    install -D -m 0700 -- "$terminal" "$release/bin/lmh"
    version=$("$release/venv/bin/python" -I -c 'from importlib.metadata import version; print(version("localmodelhub"))')
    [[ $("$release/bin/lmh" --version) == "lmh $version" ]] ||
        die "the packaged terminal and service have different versions"
    ln -s -- ../../bin/lmh "$release/venv/bin/lmh"
    install -m 0700 -- "$bundle/deploy/uninstall.sh" "$release/uninstall.sh"
    touch -- "$release/.ready"
    cleanup_release=
fi

if [[ ! -f "$catalog" ]]; then
    initial_catalog=${seed:-"$bundle/catalogs/starter.json"}
    "$release/venv/bin/python" -I - "$initial_catalog" <<'PY'
import sys

from lmh.catalog import CatalogError, load_catalog

try:
    load_catalog(sys.argv[1])
except CatalogError as exc:
    sys.exit(f"LocalModelHub install: invalid initial catalog: {exc}")
PY
    install -m 0600 -- "$initial_catalog" "$catalog"
elif [[ -n "$seed" ]]; then
    echo "Keeping existing catalog: $catalog"
fi

if "$systemd"; then
    exec 8>"$data/state/maintenance.lock"
    flock -n 8 || die "a model lifecycle job is active; deferring activation"
fi

previous_release=$(readlink -- "$data/current" || true)
units=(localmodelhub.service localmodelhub-update.service localmodelhub-update.timer)
mkdir -- "$scratch/previous-units"
for name in "${units[@]}"; do
    if [[ -f "$unit_dir/$name" ]]; then
        cp -p -- "$unit_dir/$name" "$scratch/previous-units/$name"
    fi
done

if ! cmp -s -- "$bundle/deploy/localmodelhub.service" "$unit"; then
    install -m 0644 -- "$bundle/deploy/localmodelhub.service" "$unit"
    rm -f -- "$data/.systemd-release"
fi
for name in localmodelhub-update.service localmodelhub-update.timer; do
    if ! cmp -s -- "$bundle/deploy/$name" "$unit_dir/$name"; then
        install -m 0644 -- "$bundle/deploy/$name" "$unit_dir/$name"
    fi
done
if [[ "$previous_release" != "releases/$id" ]]; then
    ln -s -- "releases/$id" "$scratch/current"
    mv -Tf -- "$scratch/current" "$data/current"
    rm -f -- "$data/.systemd-release"
fi
if mkdir -p -- "$HOME/.local/bin" 2>/dev/null && [[ -w "$HOME/.local/bin" ]] &&
    [[ ! -e "$HOME/.local/bin/lmh" && ! -L "$HOME/.local/bin/lmh" ]]; then
    ln -s -- ../share/localmodelhub/current/bin/lmh "$HOME/.local/bin/lmh"
fi

if "$systemd"; then
    panel_ready() {
        local try port=${1:-5100}
        for ((try=0; try<12; try++)); do
            if systemctl --user is-active --quiet localmodelhub.service &&
                curl --disable --noproxy '*' -fsS --max-time 1 "http://127.0.0.1:$port/api/health" |
                grep -Eq '"ok"[[:space:]]*:[[:space:]]*true'; then
                return 0
            fi
            sleep 1
        done
        return 1
    }

    restore_release() {
        local restored_port=5100
        if [[ "$previous_release" =~ ^releases/[a-f0-9]{64}-py[0-9]+\.[0-9]+$ ]] &&
            [[ -f "$data/$previous_release/.ready" ]]; then
            for name in "${units[@]}"; do
                if [[ -f "$scratch/previous-units/$name" ]]; then
                    install -m 0644 -- "$scratch/previous-units/$name" "$unit_dir/$name"
                else
                    rm -f -- "$unit_dir/$name"
                fi
            done
            ln -s -- "$previous_release" "$scratch/previous-current"
            mv -Tf -- "$scratch/previous-current" "$data/current"
            systemctl --user daemon-reload
            if [[ -f "$unit" ]] && grep -Eq -- '--port[[:space:]]+5189([[:space:]]|$)' "$unit"; then
                restored_port=5189
            fi
            if systemctl --user restart localmodelhub.service && panel_ready "$restored_port"; then
                printf '%s\n' "${previous_release##*/}" > "$data/.systemd-release"
                die "new release failed to become healthy; previous release restored"
            fi
            die "new release failed to become healthy; previous release selected but could not start"
        fi
        die "LocalModelHub could not become healthy; see journalctl --user -u localmodelhub.service"
    }

    systemctl --user daemon-reload
    systemctl --user enable localmodelhub.service
    if [[ -f "$data/.systemd-release" && $(<"$data/.systemd-release") == "$id" ]]; then
        if ! systemctl --user start localmodelhub.service || ! panel_ready; then restore_release; fi
    else
        # Also activate a release previously staged with --no-systemd.
        if ! systemctl --user restart localmodelhub.service || ! panel_ready; then restore_release; fi
        printf '%s\n' "$id" > "$scratch/systemd-release"
        mv -Tf -- "$scratch/systemd-release" "$data/.systemd-release"
    fi
    systemctl --user enable --now localmodelhub-update.timer
fi
echo "LocalModelHub installed to $data/current"
if ! "$systemd"; then
    echo "Service not enabled or started (--no-systemd)."
fi
