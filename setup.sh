#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Prepare this checkout: submodules, .venv, state/ (cloned once from STATE_REPO when set),
# and the workspace recorded there. Settings: setup.conf. Safe to run again.
set -euo pipefail
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

if [[ ! -f setup.conf ]]; then
    cp setup.conf.example setup.conf
    echo 'Created setup.conf with defaults (edit it to set STATE_REPO or PYTHON, then run again).'
fi
STATE_REPO='' PYTHON='' DEV_TOOLS=no SITE_CONF=''
PIP_ARGS=()
# shellcheck source=setup.conf.example
. ./setup.conf

# An optional site file (not in this repository) adapts installation to its environment.
site=${SITE_CONF:-setup.site.conf}
if [[ -n $SITE_CONF && ! -f $SITE_CONF ]]; then
    echo "setup.sh: SITE_CONF $SITE_CONF does not exist" >&2
    exit 1
fi
if [[ -f $site ]]; then
    echo "== Site settings: $site"
    configured_python=$PYTHON
    # shellcheck disable=SC1090
    . "$site"
    PYTHON=${configured_python:-$PYTHON}  # setup.conf wins over the site default.
fi

echo '== Submodules'
git submodule update --init --recursive

find_python() {
    local candidate path
    for candidate in ${PYTHON:-python3.14 python3.13 python3.12 python3.11 python3}; do
        path=$(command -v "$candidate") || continue
        if "$path" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
            # The interpreter itself, not a version-manager shim.
            "$path" -c 'import sys; print(sys.executable)'
            return 0
        fi
    done
    return 1
}

echo '== Python environment (.venv)'
if ! .venv/bin/python -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    if ! python=$(find_python); then
        echo "setup.sh: no Python 3.11+ found${PYTHON:+ as $PYTHON}; set PYTHON in setup.conf" >&2
        exit 1
    fi
    "$python" -m venv --clear .venv
fi
packages=(-e deps/liblore -e 'deps/b4[tui]' -e .)
if [[ $DEV_TOOLS == yes ]]; then
    packages+=(pytest pytest-asyncio pytest-xdist ruff)
fi
if declare -F site_install >/dev/null; then
    VENV=$PWD/.venv site_install "${packages[@]}"
else
    .venv/bin/pip install -q "${PIP_ARGS[@]}" "${packages[@]}"
fi

echo '== State repository (state/)'
if [[ -e state/.git ]]; then
    echo 'state/ is already a repository; left as is (the app never pushes or pulls it).'
elif [[ -n $STATE_REPO ]]; then
    git clone "$STATE_REPO" state
else
    echo 'No STATE_REPO in setup.conf. Register projects with ./b4-maintainer project add,'
    echo 'or bring a state export: ./b4-maintainer state import FILE, then ./setup.sh again.'
fi

if [[ -f state/workspace.toml || -f workspace.toml ]]; then
    echo '== Workspace'
    ./b4-maintainer setup
fi
echo 'Ready: ./b4-maintainer'
