#!/usr/bin/env bash
# Public LocalModelHub bootstrap. Download and run with Bash, not as root.
set -euo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

die() {
    printf 'LocalModelHub bootstrap: %s\n' "$*" >&2
    exit 1
}

if [[ $# == 1 && ( $1 == --help || $1 == -h ) ]]; then
    echo 'Usage: bash install.sh'
    echo 'Install the latest stable LocalModelHub release and start its public CPU starter as ai.'
    echo 'Run as ai (with linger already enabled) or as an administrator with sudo.'
    exit 0
fi
(( $# == 0 )) || die 'this bootstrap takes no arguments; see --help.'
(( EUID != 0 )) || die 'run as ai or as a non-root administrator with sudo.'
for executable in curl bash sha256sum getent systemctl loginctl; do
    command -v "$executable" >/dev/null || die "$executable is required."
done
read -r _ systemd_version _ <<< "$(systemctl --version)"
systemd_version=${systemd_version%%.*}
[[ $systemd_version =~ ^[0-9]+$ ]] && (( 10#$systemd_version >= 254 )) ||
    die 'systemd 254+ is required.'

python_ready() (
    command -v python3 >/dev/null &&
        python3 -I -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null || exit 1
    probe=$(mktemp -d /tmp/localmodelhub-python.XXXXXXXXXX) || exit 1
    trap 'rm -rf -- "$probe"' EXIT
    # Importing venv alone doesn't detect distro packages that lack the pip wheel.
    python3 -I -m venv "$probe/venv" >/dev/null 2>&1 &&
        "$probe/venv/bin/python" -I -m pip --version >/dev/null 2>&1
)

if ! python_ready; then
    command -v sudo >/dev/null || die 'Python 3.11+ with venv (including ensurepip) is required; rerun as an administrator with sudo.'
    if command -v apt-get >/dev/null; then
        sudo -- apt-get update &&
            sudo -- apt-get install -y python3 python3-venv ||
            die 'apt could not install Python and venv; check your package repositories and retry.'
    elif command -v dnf >/dev/null; then
        sudo -- dnf install -y python3 python3-pip ||
            die 'dnf could not install Python and venv; check your package repositories and retry.'
    elif command -v pacman >/dev/null; then
        sudo -- pacman -S --needed --noconfirm python python-pip ||
            die 'pacman could not install Python and venv; check your package repositories and retry.'
    else
        die 'Python 3.11+ with venv (including ensurepip) is required; no supported package manager was found.'
    fi
    python_ready || die 'The distro packages did not provide Python 3.11+ with venv; use a supported distro release.'
fi

if ! entry=$(getent passwd ai); then
    command -v sudo >/dev/null || die 'sudo is required to create the ai account.'
    sudo -- useradd --create-home --shell /bin/bash -- ai
    entry=$(getent passwd ai) || die 'cannot look up the new ai account.'
fi
IFS=: read -r account _ ai_uid _ _ ai_home _ <<< "$entry"
[[ $account == ai && $ai_uid =~ ^[1-9][0-9]*$ && $ai_home == /* && $ai_home != / ]] ||
    die 'the ai account must be non-root and have a registered absolute home directory.'

if [[ $(loginctl show-user ai --property=Linger --value 2>/dev/null || true) != yes ]]; then
    [[ $EUID != "$ai_uid" ]] ||
        die 'ask an administrator to run sudo loginctl enable-linger ai first.'
    command -v sudo >/dev/null || die 'sudo is required to enable ai linger.'
    sudo -- loginctl enable-linger ai
fi

runner=()
if [[ $EUID != "$ai_uid" ]]; then
    command -v sudo >/dev/null || die 'sudo is required to switch to the ai account.'
    runner=(sudo -H -u ai --)
fi
# Keep this content pin in sync with lmh/update.py (checked by tests/test_update.py).
updater_sha256=e63465bb7bf884d35cb03d469600c8bd45dc96704e743eec8e4380bf7535ec73
"${runner[@]}" /usr/bin/env -i PATH="$PATH" HOME="$ai_home" \
    XDG_RUNTIME_DIR="/run/user/$ai_uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$ai_uid/bus" \
    /bin/bash --noprofile --norc -s -- "$updater_sha256" <<'BOOTSTRAP'
set -euo pipefail
umask 077
(( EUID != 0 )) && [[ $(id -un) == ai ]] || exit 1
for ((attempt=0; attempt<10; attempt++)); do
    if systemctl --user show-environment >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
if (( attempt == 10 )); then
    echo 'LocalModelHub bootstrap: no ai user systemd manager; verify linger and try again.' >&2
    exit 1
fi
temp=$(mktemp -d /tmp/localmodelhub-bootstrap.XXXXXXXXXX)
trap 'rm -rf -- "$temp"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
(
    # Older curl cannot always enforce --max-filesize on streamed responses.
    ulimit -f 128
    curl --disable --proto '=https' --globoff --fail --silent --show-error \
        --connect-timeout 10 --max-time 30 --max-filesize 65536 \
        --output "$temp/update.py" \
        --url https://raw.githubusercontent.com/hamb1y/localmodelhub/main/lmh/update.py
)
read -r actual _ < <(sha256sum -- "$temp/update.py")
if [[ $actual != "$1" ]]; then
    echo 'LocalModelHub bootstrap: updater checksum mismatch; download a fresh install.sh and retry.' >&2
    exit 1
fi
python3 -I "$temp/update.py" --bootstrap
BOOTSTRAP

if [[ $EUID != "$ai_uid" ]]; then
    # Install a launcher from this script, never from the ai-owned release.
    # Hard-link into place atomically without replacing any pre-existing path.
    sudo -- /bin/bash --noprofile --norc -c '
        set -euo pipefail
        export PATH=/usr/sbin:/usr/bin:/sbin:/bin
        umask 077
        dir=/usr/local/bin
        target=$dir/lmh
        for path in /usr /usr/local "$dir"; do
            if [[ $path != /usr && ! -e $path && ! -L $path ]]; then
                mkdir -m 0755 -- "$path"
            fi
            owner=$(stat -c %u -- "$path")
            mode=$(stat -c %a -- "$path")
            [[ ! -L $path && $owner == 0 && $mode =~ ^[0-7]+$ ]] &&
                (( (8#$mode & 022) == 0 )) ||
                { echo "LocalModelHub bootstrap: unsafe directory for the launcher: $path" >&2; exit 1; }
        done
        temp=$(mktemp "$dir/.lmh.XXXXXXXX")
        trap '\''rm -f -- "$temp"'\'' EXIT
        cat >"$temp"
        chmod 0755 -- "$temp"
        if ! ln -T -- "$temp" "$target" 2>/dev/null; then
            if [[ -e $target || -L $target ]]; then
                if [[ ! -L $target && -f $target && $(stat -c "%u:%a" -- "$target") == 0:755 ]] &&
                    cmp -s -- "$temp" "$target"; then
                    exit 0
                fi
                echo "LocalModelHub bootstrap: keeping the existing $target; it was not replaced." >&2
            else
                echo "LocalModelHub bootstrap: could not install $target." >&2
                exit 1
            fi
        fi
    ' <<'LAUNCHER'
#!/bin/bash
# LocalModelHub administrator launcher, installed by the public bootstrap.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
entry=$(getent passwd ai) || { echo 'LocalModelHub: ai is missing; rerun the public installer.' >&2; exit 1; }
IFS=: read -r account _ ai_uid _ _ ai_home _ <<< "$entry"
[[ $account == ai && $ai_uid =~ ^[1-9][0-9]*$ && $ai_home == /* && $ai_home != / ]] ||
    { echo 'LocalModelHub: invalid ai account.' >&2; exit 1; }
runner=()
if [[ $EUID != "$ai_uid" ]]; then
    runner=(sudo -H -u ai)
    [[ -z ${LMH_API_TOKEN:-} ]] || runner+=(--preserve-env=LMH_API_TOKEN)
    [[ -z ${LMH_INFERENCE_KEY:-} ]] || runner+=(--preserve-env=LMH_INFERENCE_KEY)
    runner+=(--)
fi
command=(/usr/bin/env PATH="$PATH" HOME="$ai_home" XDG_RUNTIME_DIR="/run/user/$ai_uid"
         DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$ai_uid/bus"
         "$ai_home/.local/share/localmodelhub/current/bin/lmh" "$@")
if [[ $EUID != "$ai_uid" && ${1:-} == uninstall &&
      ( $# == 1 || ( $# == 2 && $2 == --purge ) ) ]]; then
    shim=/usr/local/bin/lmh
    identity=$(stat -Lc '%d:%i' -- "${BASH_SOURCE[0]}" 2>/dev/null || true)
    "${runner[@]}" "${command[@]}"
    # Delete only the root-owned launcher that handled this uninstall. Do not
    # delete replacements, copies of this launcher, or a caller's symlink.
    if [[ -n $identity && ! -L $shim && $(stat -c '%u:%d:%i' -- "$shim" 2>/dev/null || true) == "0:$identity" ]]; then
        sudo -- /bin/bash --noprofile --norc -c '
            shim=/usr/local/bin/lmh
            if [[ ! -L $shim && $(/usr/bin/stat -c "%u:%d:%i" -- "$shim" 2>/dev/null) == "0:$1" ]]; then
                /usr/bin/rm -- "$shim"
            fi
        ' lmh-uninstall "$identity" ||
            echo "LocalModelHub: uninstall succeeded; could not remove $shim." >&2
    fi
else
    exec "${runner[@]}" "${command[@]}"
fi
LAUNCHER
fi

if [[ $EUID != "$ai_uid" ]]; then
    runner=(sudo -H -u ai)
    [[ -z ${LMH_API_TOKEN:-} ]] || runner+=(--preserve-env=LMH_API_TOKEN)
    [[ -z ${LMH_INFERENCE_KEY:-} ]] || runner+=(--preserve-env=LMH_INFERENCE_KEY)
    runner+=(--)
fi
onboard=("${runner[@]}" /usr/bin/env PATH="$PATH" HOME="$ai_home"
         XDG_RUNTIME_DIR="/run/user/$ai_uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$ai_uid/bus"
         "$ai_home/.local/share/localmodelhub/current/bin/lmh" onboard)
# The provisioning heredocs are finished. Redirect only at the final exec so
# a Bash reading this script through curl | bash never reads it from /dev/tty.
if [[ ! -t 0 && -t 1 ]] && { exec 3</dev/tty; } 2>/dev/null; then
    exec "${onboard[@]}" <&3 3<&-
fi
exec "${onboard[@]}"
