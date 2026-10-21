#!/usr/bin/env bash
# Per-user Linux installation. Safe to run again to update from main.
set -euo pipefail

# When piped from curl, do nothing until Bash has received the complete body.
main() {
repo=https://github.com/maceeek/AnyPS3.git
ref=main
python=python3
wheelhouse=
usage() {
    cat <<'EOF'
Usage: bash install.sh [--repo URL_OR_PATH] [--ref BRANCH_OR_TAG] [--python PYTHON]
                       [--wheelhouse DIRECTORY]

Installs AnyPS3 and its GUI for the current Linux user; rerun to update.
Requires Git, Python 3.11+ with venv/ensurepip, and internet access for packages.
No sudo, system Python changes, or modifications to development checkouts.
App:     ${XDG_DATA_HOME:-~/.local/share}/anyps3-app
Library: ${XDG_DATA_HOME:-~/.local/share}/anyps3-library
Commands: ~/.local/bin/anyps3{,-gui,-manager}
--wheelhouse uses only predownloaded wheels (including setuptools), without PyPI.
EOF
}
die() { printf 'AnyPS3 installer: %s\n' "$*" >&2; exit 1; }
while (($#)); do
    case "$1" in
        --repo|--ref|--python|--wheelhouse)
            (($# >= 2)) && [[ -n "$2" ]] || die "Missing value for $1"
            case "$1" in
                --repo) repo=$2 ;; --ref) ref=$2 ;; --python) python=$2 ;;
                --wheelhouse) wheelhouse=$2 ;;
            esac
            shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "Unknown option: $1 (see --help)" ;;
    esac
done
[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || die 'Linux x86-64 is required.'
((EUID != 0)) || die 'Run this installer as your normal user, without sudo.'
for command in git flock "$python"; do
    command -v "$command" >/dev/null || die "Missing $command; install it with your distribution's package manager."
done
unset PYTHONPATH PYTHONHOME VIRTUAL_ENV LD_PRELOAD LD_LIBRARY_PATH
unset QT_PLUGIN_PATH QT_QPA_PLATFORM_PLUGIN_PATH QML2_IMPORT_PATH
# PIP_TARGET/PIP_PREFIX and user configuration can otherwise redirect a venv
# installation into a development tree. Package sources must be explicit.
for variable in ${!PIP_@}; do unset "$variable"; done
export PIP_CONFIG_FILE=/dev/null
pip_options=()
if [[ -n "$wheelhouse" ]]; then
    [[ -d "$wheelhouse" ]] || die "Wheel directory not found: $wheelhouse"
    wheelhouse=$(cd "$wheelhouse" && pwd -P)
    pip_options=(--no-index --find-links "$wheelhouse")
fi
"$python" -I -c 'import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || die 'Python 3.11+ with venv and ensurepip is required (Debian/Ubuntu: python3-venv).'

data_dir=${XDG_DATA_HOME:-$HOME/.local/share}
config_dir=${XDG_CONFIG_HOME:-$HOME/.config}/anyps3-app
app_dir=$data_dir/anyps3-app
library_dir=$data_dir/anyps3-library
bin_dir=$HOME/.local/bin
desktop_dir=$data_dir/applications
for path in "$data_dir" "$config_dir" "$bin_dir"; do
    [[ "$path" == /* && "$path" != *$'\n'* && "$path" != *$'\r'* ]] || die 'HOME and XDG paths must be absolute, single-line paths.'
done
# setuptools expands dollar variables while constructing an editable install.
[[ "$data_dir" != *'$'* ]] || die 'Python packaging cannot install under a path containing $. Set XDG_DATA_HOME to an absolute path without $.'
if [[ -e "$app_dir" && ! -f "$app_dir/.installer-owned" ]]; then
    die "Refusing to overwrite an unmanaged directory: $app_dir"
fi
mkdir -p "$app_dir/releases" "$bin_dir" "$desktop_dir"
touch "$app_dir/.installer-owned"
exec 9>"$app_dir/.install.lock"
flock -n 9 || die 'Another AnyPS3 installation is in progress.'
[[ ! -e "$app_dir/current" || -L "$app_dir/current" ]] || die 'The current installation pointer is not a symlink.'

# Never overwrite an unrelated command or desktop entry, including broken links.
check_link() {
    if [[ -e "$1" || -L "$1" ]]; then
        [[ -L "$1" && $(readlink -- "$1") == "$2" ]] || die "Unrelated file already exists: $1"
    fi
}
for name in anyps3 anyps3-manager anyps3-gui; do
    check_link "$bin_dir/$name" "$app_dir/current/launchers/$name"
done
check_link "$desktop_dir/anyps3.desktop" "$app_dir/current/anyps3.desktop"

release=$(mktemp -d "$app_dir/releases/release.XXXXXXXX")
cleanup() {
    status=$?
    if [[ $(readlink -- "$app_dir/current" 2>/dev/null || true) != "$release" ]]; then
        rm -rf -- "$release"
        rm -f -- "$app_dir/.current-$$"
        printf 'Installation did not activate; the previous version is unchanged.\n' >&2
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if [[ -d "$repo" ]]; then repo=$(cd "$repo" && pwd -P); fi
printf 'Installing AnyPS3 from %s (%s)…\n' "$repo" "$ref"
# A new clone never shares a development checkout, Git objects, or its venv.
git clone --quiet --no-hardlinks --depth 1 --branch "$ref" -- "$repo" "$release/source"
git -C "$release/source" rev-parse HEAD >"$release/revision"
"$python" -I -m venv "$release/venv"
"$release/venv/bin/python" -I -m pip --isolated --disable-pip-version-check install \
    --no-cache-dir "${pip_options[@]}" --editable "$release/source[gui,self]"

# Editable only within this private snapshot: the manager locates profiles next
# to its source. Neither the installed packages nor profiles point at a dev tree.
QT_QPA_PLATFORM=offscreen SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null \
    "$release/venv/bin/python" -I - "$release/source" <<'PY'
import json
from pathlib import Path
import sys
import cryptography
from PySide6.QtWidgets import QApplication
import anyps3_gui.app
from anyps3_manager.storage import Library
source = Path(sys.argv[1]).resolve()
assert Library().source == source, 'Installed manager must use its private source snapshot'
profiles = list((source / 'profiles').glob('*.json'))
assert profiles, 'No game profiles in the installation'
for profile in profiles:
    json.loads(profile.read_text())
app = QApplication([])
print('Python, Qt and game profiles verified.')
PY

# Launch from a fixed snapshot even if an update changes current during startup.
# Isolate imports, Qt plugins, settings, library and tool lookup from development.
"$release/venv/bin/python" -I - "$release" "$library_dir" "$config_dir" <<'PY'
from pathlib import Path
import shlex
import sys
release, library, config = map(Path, sys.argv[1:])
launchers = release / 'launchers'
launchers.mkdir()
for name, module in [('anyps3', 'anyps3'), ('anyps3-manager', 'anyps3_manager'), ('anyps3-gui', 'anyps3_gui')]:
    args = ['--library', str(library)] if module != 'anyps3' else []
    command = shlex.join([str(release / 'venv/bin/python'), '-I', '-m', module, *args])
    script = ('#!/bin/bash\nset -euo pipefail\n'
              'unset PYTHONPATH PYTHONHOME VIRTUAL_ENV LD_PRELOAD LD_LIBRARY_PATH\n'
              'unset QT_PLUGIN_PATH QT_QPA_PLATFORM_PLUGIN_PATH QML2_IMPORT_PATH\n'
              f'export PATH={shlex.quote(str(release / "venv/bin") + ":/usr/local/bin:/usr/bin:/bin")}\n'
              f'export XDG_CONFIG_HOME={shlex.quote(str(config))}\n'
              f'cd -- {shlex.quote(str(release))}\n'
              f'exec {command} "$@"\n')
    path = launchers / name
    path.write_text(script)
    path.chmod(0o755)
# Desktop Exec quoting has two escaping layers, plus literal percent escaping.
# https://specifications.freedesktop.org/desktop-entry/latest/exec-variables.html
executable = str(release.parent.parent / 'current/launchers/anyps3-gui')
quoted = '"' + ''.join('\\' + c if c in '\\"`$' else c for c in executable) + '"'
quoted = quoted.replace('\\', '\\\\').replace('%', '%%')
(release / 'anyps3.desktop').write_text(
    '[Desktop Entry]\nType=Application\nName=AnyPS3\n'
    'Comment=PS3 game library and native build manager\n'
    f'Exec=/bin/bash {quoted}\nIcon=applications-games\nTerminal=false\n'
    'Categories=Game;\nStartupNotify=false\n')
PY
for name in anyps3 anyps3-manager anyps3-gui; do
    if [[ ! -L "$bin_dir/$name" ]]; then
        ln -s -- "$app_dir/current/launchers/$name" "$bin_dir/$name"
    fi
done
if [[ ! -L "$desktop_dir/anyps3.desktop" ]]; then
    ln -s -- "$app_dir/current/anyps3.desktop" "$desktop_dir/anyps3.desktop"
fi
ln -s -- "$release" "$app_dir/.current-$$"
mv -Tf -- "$app_dir/.current-$$" "$app_dir/current"
if command -v update-desktop-database >/dev/null; then
    update-desktop-database "$desktop_dir" || true
fi
printf '\nAnyPS3 installed (%s).\nLaunch AnyPS3 from your application menu, or run:\n  %q\n' \
    "$(cat "$release/revision")" "$bin_dir/anyps3-gui"
printf 'Library: %s\nRerun the same installer command to update, then reopen the GUI.\n' "$library_dir"
printf 'Previous app versions are retained so running jobs keep their source and environment.\n'
}
main "$@"
