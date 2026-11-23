#!/usr/bin/env bash
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
venv="$root/communication/eln_http_demo/.venv"
exports="${UNITREE_DEPENDENCY_EXPORTS:-$root/../../unitree-migration/migration-dependencies}"

usage() {
    printf 'Usage: bash %s [--prepare-only | --wheelhouse DIR]\n' "$0"
}

source_args=(--index-url https://pypi.org/simple --retries 0 --timeout 10)
prepare_only=0
case "${1:-}" in
    '') [[ $# == 0 ]] || { usage >&2; exit 2; } ;;
    --help|-h) usage; exit 0 ;;
    --prepare-only)
        [[ $# == 1 ]] || { usage >&2; exit 2; }
        prepare_only=1
        ;;
    --wheelhouse)
        [[ $# == 2 && -d "$2" ]] || { usage >&2; exit 2; }
        source_args=(--no-index --find-links "$(realpath -- "$2")")
        ;;
    *) usage >&2; exit 2 ;;
esac

( cd -- "$exports" && sha256sum --quiet -c SHA256SUMS )
if [[ -L "$venv" || ( -e "$venv" && ! -f "$venv/pyvenv.cfg" ) ]]; then
    printf 'Not an existing local venv: %s\n' "$venv" >&2
    exit 1
fi
if [[ ! -e "$venv" ]]; then
    /usr/bin/python3 -IB -c 'import sys; assert sys.version_info[:2] == (3, 8), "Python 3.8 required by the export"'
    /usr/bin/python3 -IB -m venv --without-pip "$venv"
fi

python="$venv/bin/python"
"$python" -IB -c 'import os, sys; assert sys.version_info[:2] == (3, 8) and os.path.realpath(sys.prefix) == sys.argv[1], "Expected the workspace Python 3.8 venv"' "$venv"
if ! grep -Eqi '^include-system-site-packages[[:space:]]*=[[:space:]]*false[[:space:]]*$' "$venv/pyvenv.cfg"; then
    printf 'Refusing a venv that can import system packages: %s\n' "$venv" >&2
    exit 1
fi
export TMPDIR="$venv" PIP_CONFIG_FILE=/dev/null
pip_args=(--isolated --disable-pip-version-check --no-cache-dir --no-input --require-virtualenv)

if ! "$python" -IB -m pip --version >/dev/null 2>&1; then
    if "$python" -IB -c 'import ensurepip' >/dev/null 2>&1; then
        "$python" -IB -m ensurepip --default-pip
    else
        # Ubuntu ships this pip wheel separately from the ensurepip module.
        shopt -s nullglob
        wheels=(/usr/share/python-wheels/pip-*.whl)
        if (( ${#wheels[@]} != 1 )); then
            printf 'Neither ensurepip nor an unambiguous Ubuntu pip seed wheel is available.\n' >&2
            exit 1
        fi
        seed="${wheels[0]}"
        "$python" -IB -c 'import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); runpy.run_module("pip", run_name="__main__", alter_sys=True)' \
            "$seed" "${pip_args[@]}" install --no-index --no-deps "$seed"
    fi
fi

if (( prepare_only )); then
    "$python" -IB -m pip --version
    printf 'ELN venv prepared: %s\nApplication dependencies were not installed by --prepare-only.\n' "$venv"
    exit 0
fi

"$python" -IB -m pip "${pip_args[@]}" install --only-binary=:all: "${source_args[@]}" 'pip==24.3.1'
"$python" -IB -m pip "${pip_args[@]}" install --only-binary=:all: "${source_args[@]}" \
    -c "$exports/Unitree/requirements-pip-only.txt" \
    -r "$root/requirements-eln.txt" -r "$root/communication/eln_http_demo/requirements.txt"
"$python" -IB -m pip "${pip_args[@]}" check
"$python" -IB -c 'import fastapi, httpx, numpy, pydantic, pytest, uvicorn; print("ELN imports OK")'
