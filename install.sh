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
    echo 'Usage: bash install.sh [--catalog FILE | --starter | --keep]'
    echo 'Install LocalModelHub from Git and start its public CPU starter as ai.'
    echo 'Run as ai (with linger already enabled) or as an administrator with sudo.'
    exit 0
fi
choice=
catalog_file=
case $# in
    0) ;;
    1) case "$1" in --starter) choice=starter ;; --keep) choice=keep ;; *) die 'see --help.' ;; esac ;;
    2) [[ $1 == --catalog && -n $2 ]] || die 'see --help.'
       choice=import; catalog_file=$2 ;;
    *) die 'see --help.' ;;
esac
(( EUID != 0 )) || die 'run as ai or as a non-root administrator with sudo.'

stage() {
    if [[ -t 2 && ${TERM:-dumb} != dumb ]]; then
        printf '\n\033[1m› %s\033[0m\n' "$*" >&2
    else
        printf '\n› %s\n' "$*" >&2
    fi
}
if [[ -t 1 && ${TERM:-dumb} != dumb ]] && { exec 3<>/dev/tty; } 2>/dev/null; then
    printf '\n\033[48;2;34;90;56m\033[97;1m  LOCALMODELHUB  /  SETUP  \033[0m\n' >&3
    printf '\n  Detecting inference devices…\n' >&3
    awk -F: '/^(model name|Hardware|Processor)[[:space:]]*:/ {sub(/^ +/,"",$2); print "  CPU       " $2; exit}' /proc/cpuinfo >&3
    awk '/^MemTotal:/ {printf "  RAM       %.1f GiB\n", $2 / 1048576}' /proc/meminfo >&3
    if command -v lspci >/dev/null; then
        lspci | awk 'tolower($0) ~ /vga|3d controller|display controller|processing accelerators|neural/ {print "  Device    " $0}' >&3
    else
        for device in /sys/bus/pci/devices/*; do
            [[ -r $device/class ]] || continue
            read -r class < "$device/class" || continue
            case "$class" in
                0x03*|0x12*) printf '  Device    PCI %s [%s:%s]\n' "${device##*/}" "$(< "$device/vendor")" "$(< "$device/device")" >&3 ;;
            esac
        done
    fi
    if [[ -z $choice ]]; then
        printf '\n  [1] Keep existing models (or start with the CPU starter)\n  [2] Import a models.json file\n  [3] Use the CPU starter (back up existing config)\n  [q] Quit\n\n' >&3
        while true; do
            read -r -p '  Select [1]: ' answer <&3 2>&3 || exit 130
            case "$answer" in
                ''|1) choice=keep; break ;;
                2) choice=import; read -r -e -p '  Path to models.json: ' catalog_file <&3 2>&3 || exit 130; break ;;
                3) choice=starter; break ;;
                q|Q) exit 0 ;;
                *) printf '  Choose 1, 2, 3, or q.\n' >&3 ;;
            esac
        done
    fi
    exec 3>&-
fi
choice=${choice:-keep}
if [[ $choice == import ]]; then
    [[ $catalog_file != '~/'* ]] || catalog_file="$HOME/${catalog_file:2}"
    [[ -f $catalog_file && -r $catalog_file ]] || die 'choose a readable models.json file.'
fi
stage 'Preparing the host'
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

need_python=false
need_git=false
need_cc=false
need_podman=false
python_ready || need_python=true
command -v git >/dev/null || need_git=true
command -v cc >/dev/null || need_cc=true
command -v podman >/dev/null || need_podman=true
if "$need_python" || "$need_git" || "$need_cc" || "$need_podman"; then
    command -v sudo >/dev/null || die 'Git, a C toolchain, Python 3.11+ with venv, and Podman are required; rerun as an administrator with sudo.'
    packages=()
    if command -v apt-get >/dev/null; then
        if "$need_python"; then packages+=(python3 python3-venv); fi
        if "$need_git"; then packages+=(git); fi
        if "$need_cc"; then packages+=(build-essential); fi
        if "$need_podman"; then packages+=(podman uidmap); fi
        sudo -- apt-get update &&
            sudo -- apt-get install -y "${packages[@]}" ||
            die 'apt could not install the prerequisites.'
    elif command -v dnf >/dev/null; then
        if "$need_python"; then packages+=(python3 python3-pip); fi
        if "$need_git"; then packages+=(git); fi
        if "$need_cc"; then packages+=(gcc glibc-devel); fi
        if "$need_podman"; then packages+=(podman shadow-utils); fi
        sudo -- dnf install -y "${packages[@]}" || die 'dnf could not install the prerequisites.'
    elif command -v pacman >/dev/null; then
        if "$need_python"; then packages+=(python python-pip); fi
        if "$need_git"; then packages+=(git); fi
        if "$need_cc"; then packages+=(base-devel); fi
        if "$need_podman"; then packages+=(podman uidmap); fi
        sudo -- pacman -S --needed --noconfirm "${packages[@]}" ||
            die 'pacman could not install the prerequisites.'
    else
        die 'Install Git, a C toolchain, Python 3.11+ with venv, and Podman; no supported package manager was found.'
    fi
    python_ready && command -v git >/dev/null && command -v cc >/dev/null &&
        command -v podman >/dev/null ||
        die 'The distro packages did not provide Git, cc, Python 3.11+ with venv, and Podman.'
fi
command -v newuidmap >/dev/null && command -v newgidmap >/dev/null ||
    die 'Rootless Podman requires newuidmap/newgidmap (the uidmap package on Debian and Arch).'

if ! entry=$(getent passwd ai); then
    command -v sudo >/dev/null || die 'sudo is required to create the ai account.'
    sudo -- useradd --create-home --shell /bin/bash -- ai
    entry=$(getent passwd ai) || die 'cannot look up the new ai account.'
fi
IFS=: read -r account _ ai_uid _ _ ai_home _ <<< "$entry"
[[ $account == ai && $ai_uid =~ ^[1-9][0-9]*$ && $ai_home == /* && $ai_home != / ]] ||
    die 'the ai account must be non-root and have a registered absolute home directory.'

subordinate=()
[[ -n $(getent subuid ai || true) ]] || subordinate+=(--add-subuids 100000-165535)
[[ -n $(getent subgid ai || true) ]] || subordinate+=(--add-subgids 100000-165535)
if ((${#subordinate[@]})); then
    [[ $EUID != "$ai_uid" ]] ||
        die 'ask an administrator to add subordinate UID/GID ranges for ai (usermod --add-subuids --add-subgids).'
    command -v sudo >/dev/null || die 'sudo is required to add subordinate UID/GID ranges for ai.'
    sudo -- usermod "${subordinate[@]}" ai ||
        die 'could not add subordinate UID/GID ranges for ai; rootless Podman cannot start.'
fi

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
staged_catalog=
if [[ $choice == import ]]; then
    stage 'Reading your models.json'
    staged_catalog=$(head -c 1048577 -- "$catalog_file" | "${runner[@]}" /bin/bash --noprofile --norc -c '
        set -euo pipefail
        umask 077
        file=$(mktemp /tmp/localmodelhub-catalog.XXXXXXXXXX)
        trap '\''rm -f -- "$file"'\'' EXIT
        cat > "$file"
        (( $(wc -c < "$file") <= 1048576 )) || { echo "models.json exceeds 1 MiB." >&2; exit 1; }
        printf "%s\n" "$file"
        trap - EXIT
    ')
    trap '"${runner[@]}" /usr/bin/rm -f -- "$staged_catalog"' EXIT
fi
stage 'Cloning LocalModelHub and building the app'
"${runner[@]}" /usr/bin/env -i PATH="$PATH" HOME="$ai_home" \
    XDG_RUNTIME_DIR="/run/user/$ai_uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$ai_uid/bus" \
    /bin/bash --noprofile --norc -s -- "$choice" "$staged_catalog" <<'BOOTSTRAP'
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
data="$HOME/.local/share/localmodelhub"
mkdir -p -- "$data"
exec 7>"$data/update.lock"
flock 7
temp=$(mktemp -d "$data/.bootstrap.XXXXXXXXXX")
trap 'rm -rf -- "$temp"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0 \
    git -c http.followRedirects=false clone --depth 1 --single-branch --branch main \
    https://github.com/hamb1y/localmodelhub.git "$temp/source"
case "$1" in
    import) bash "$temp/source/deploy/install.sh" --setup import --catalog "$2" ;;
    starter|keep) bash "$temp/source/deploy/install.sh" --setup "$1" ;;
esac
BOOTSTRAP
if [[ -n $staged_catalog ]]; then
    "${runner[@]}" /usr/bin/rm -f -- "$staged_catalog"
    trap - EXIT
fi

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
panel_only=0
[[ $choice != import ]] || panel_only=1
onboard=("${runner[@]}" /usr/bin/env PATH="$PATH" HOME="$ai_home" LMH_SETUP_PANEL_ONLY="$panel_only"
         XDG_RUNTIME_DIR="/run/user/$ai_uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$ai_uid/bus"
         "$ai_home/.local/share/localmodelhub/current/bin/lmh" onboard)
# The provisioning heredocs are finished. Redirect only at the final exec so
# a Bash reading this script through curl | bash never reads it from /dev/tty.
if [[ ! -t 0 && -t 1 ]] && { exec 3</dev/tty; } 2>/dev/null; then
    exec "${onboard[@]}" <&3 3<&-
fi
exec "${onboard[@]}"
