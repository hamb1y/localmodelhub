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
    echo 'Install the latest stable LocalModelHub release under the dedicated ai user.'
    echo 'Run as ai (with linger already enabled) or as an administrator with sudo.'
    exit 0
fi
(( $# == 0 )) || die 'this bootstrap takes no arguments; see --help.'
(( EUID != 0 )) || die 'run as ai or as a non-root administrator with sudo.'
for executable in curl bash python3 sha256sum getent systemctl loginctl; do
    command -v "$executable" >/dev/null || die "$executable is required."
done
python3 -I -c 'import sys; sys.exit(sys.version_info < (3, 11))' ||
    die 'Python 3.11+ is required.'
read -r _ systemd_version _ <<< "$(systemctl --version)"
systemd_version=${systemd_version%%.*}
[[ $systemd_version =~ ^[0-9]+$ ]] && (( 10#$systemd_version >= 254 )) ||
    die 'systemd 254+ is required.'

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
