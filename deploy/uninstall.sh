#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "Usage: lmh uninstall [--purge]"
    echo "Without --purge, keep catalogs, policy, history, and downloaded models."
}

purge=false
case "${1:-}" in
    "") ;;
    --purge) purge=true ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
esac
(($# <= 1)) || { usage >&2; exit 2; }

if (( EUID == 0 )) || [[ $(id -un) != ai ]]; then
    echo "Run uninstall as the ai account, not as root." >&2
    exit 1
fi
[[ ${HOME:-} == /* && $HOME != / ]] || { echo "Invalid HOME." >&2; exit 1; }
config="$HOME/.config/localmodelhub"
data="$HOME/.local/share/localmodelhub"
unit_dir="$HOME/.config/systemd/user"
systemctl --user show-environment >/dev/null

# Disable scheduling before stopping the worker. Never stop adopted system
# units: they have no LocalModelHub transient unit name.
if [[ -e "$unit_dir/localmodelhub-update.timer" ]]; then
    systemctl --user disable --now localmodelhub-update.timer
fi
if [[ -e "$unit_dir/localmodelhub-update.service" ]]; then
    systemctl --user stop localmodelhub-update.service
fi
if [[ -e "$unit_dir/localmodelhub.service" ]]; then
    systemctl --user disable --now localmodelhub.service
fi
if [[ -d "$data" ]]; then
    exec 9>"$data/install.lock"
    flock 9
fi
managed=$(systemctl --user list-units 'lmh-*.service' --type=service \
    --state=active,activating,reloading,deactivating --no-legend --plain)
while read -r unit _; do
    if [[ $unit =~ ^lmh-[A-Za-z0-9_.-]{1,30}-[0-9a-f]{12}\.service$ ]]; then
        systemctl --user stop "$unit"
    fi
done <<< "$managed"

for unit in localmodelhub.service localmodelhub-update.service localmodelhub-update.timer; do
    rm -f -- "$unit_dir/$unit"
    rm -rf -- "$unit_dir/$unit.d"
done
systemctl --user daemon-reload
if [[ $(readlink -- "$HOME/.local/bin/lmh" || true) == ../share/localmodelhub/current/bin/lmh ]]; then
    rm -f -- "$HOME/.local/bin/lmh"
fi
if "$purge"; then
    rm -rf -- "$config" "$data"
    echo "LocalModelHub and its settings, history, and private cache were removed."
else
    rm -f -- "$data/current" "$data/.systemd-release"
    rm -rf -- "$data/releases"
    echo "LocalModelHub was removed. Its local configuration and inference cache were kept."
fi
