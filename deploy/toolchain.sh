# Sourced by deploy/install.sh. Keep build tools private to LocalModelHub.
toolchain="$data/build"
mkdir -p -- "$toolchain"

fetch_pinned() {
    local url=$1 digest=$2 output=$3
    curl --disable --fail --silent --show-error --location --proto '=https' \
        --proto-redir '=https' --connect-timeout 10 --max-time 120 --max-filesize 134217728 \
        --output "$output" "$url"
    printf '%s  %s\n' "$digest" "$output" | sha256sum --strict --check --status
}

case "$(uname -m)" in
    x86_64)
        triple=x86_64-unknown-linux-gnu
        rustup_hash=20a06e644b0d9bd2fbdbfd52d42540bdde820ea7df86e92e533c073da0cdd43c
        bun_variant=bun-linux-x64-baseline
        bun_hash=1fc2edac843102909e3a1be1d8d9802cc6071cf074e67e88231f4ffe0f8b397b
        ;;
    aarch64)
        triple=aarch64-unknown-linux-gnu
        rustup_hash=e3853c5a252fca15252d07cb23a1bdd9377a8c6f3efa01531109281ae47f841c
        bun_variant=bun-linux-aarch64
        bun_hash=efa9813da5ed72423bf847f916e8d2c47c0d776add972354026a75e10da9aa21
        ;;
    *) die "Linux x86_64 or aarch64 is required." ;;
esac

export CARGO_HOME="$toolchain/cargo" RUSTUP_HOME="$toolchain/rustup"
rust_version=1.88.0
cargo=(cargo)
system_rust=$(rustc --version 2>/dev/null || true)
if ! [[ $system_rust =~ ^rustc[[:space:]]+1\.([0-9]+)\. ]] || (( BASH_REMATCH[1] < 88 )) ||
    ! command -v cargo >/dev/null; then
    rustup="$CARGO_HOME/bin/rustup"
    if [[ ! -x "$rustup" ]]; then
        echo 'Installing the LocalModelHub Rust toolchain…'
        fetch_pinned "https://static.rust-lang.org/rustup/archive/1.28.2/$triple/rustup-init" \
            "$rustup_hash" "$scratch/rustup-init" || die "Rust installer verification failed."
        chmod 0700 -- "$scratch/rustup-init"
        "$scratch/rustup-init" -y --no-modify-path --profile minimal --default-toolchain none
    fi
    if ! "$rustup" run "$rust_version" rustc --version >/dev/null 2>&1; then
        "$rustup" toolchain install "$rust_version" --profile minimal
    fi
    cargo=("$rustup" run "$rust_version" cargo)
fi

bun_version=1.4.3
bun="$toolchain/bun-$bun_version"
if [[ ! -x "$bun" ]]; then
    echo 'Installing the LocalModelHub Bun tool…'
    fetch_pinned "https://github.com/oven-sh/bun/releases/download/bun-v$bun_version/$bun_variant.zip" \
        "$bun_hash" "$scratch/bun.zip" || die "Bun verification failed."
    "$python" -I - "$scratch/bun.zip" "$bun_variant/bun" "$scratch/bun" <<'PY'
import shutil
import sys
from zipfile import ZipFile

archive, member, output = sys.argv[1:]
with ZipFile(archive) as bundle, bundle.open(member) as source, open(output, "xb") as target:
    shutil.copyfileobj(source, target)
PY
    install -m 0700 -- "$scratch/bun" "$bun"
fi
[[ $("$bun" --version) == "$bun_version" ]] || die "Unexpected Bun version."
export BUN_INSTALL_CACHE_DIR="$toolchain/bun-cache"
