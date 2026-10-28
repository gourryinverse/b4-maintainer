# SPDX-License-Identifier: GPL-2.0-or-later
"""The key patatt signs mail with, for every project (the global git configuration): shown,
set from an existing PGP key (gpg's secret keys) or a new patatt ed25519 key, or removed.
A checkout's own patatt.signingkey still overrides it there.
"""

import pathlib
import subprocess
import sys
from typing import Callable, Optional, Sequence


def _git(*args: str, cwd: Optional[pathlib.Path] = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ['git', *args], cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )


def _value(key: str, *where: str, cwd: Optional[pathlib.Path] = None) -> str:
    run = _git('config', *where, '--get', key, cwd=cwd)
    return run.stdout.strip() if run.returncode == 0 else ''


def signing_status(checkouts: Sequence[tuple[str, pathlib.Path]]) -> list[str]:
    """Lines saying which key signs: the global one (every project), then each checkout
    whose own setting differs."""
    shared = _value('patatt.signingkey', '--global')
    pgp = _value('user.signingkey', '--global')
    if shared:
        lines = [f'Signing with {shared} for every project (patatt.signingkey)']
    elif pgp:
        lines = [f'Signing with PGP key {pgp} for every project (user.signingkey)']
    else:
        lines = ['No signing key: messages go unsigned']
    for project, checkout in checkouts:
        own = _value('patatt.signingkey', '--local', cwd=checkout)
        if own and own != shared:
            lines.append(f'{project}: its checkout signs with {own}')
    return lines


def _gpg_listing() -> str:
    run = subprocess.run(
        ['gpg', '--batch', '--list-secret-keys', '--with-colons'],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    return run.stdout if run.returncode == 0 else ''


def gpg_secret_keys(listing: Callable[[], str] = _gpg_listing) -> list[tuple[str, str]]:
    """(fingerprint, first user id) of each primary secret key gpg has."""
    keys: list[tuple[str, str]] = []
    fpr = uid = ''
    primary = False
    for line in listing().splitlines():
        fields = line.split(':')
        if fields[0] == 'sec':
            if fpr:
                keys.append((fpr, uid))
            fpr, uid, primary = '', '', True
        elif fields[0] == 'ssb':
            primary = False
        elif fields[0] == 'fpr' and primary and not fpr:
            fpr = fields[9]
        elif fields[0] == 'uid' and primary and not uid:
            uid = fields[9]
    if fpr:
        keys.append((fpr, uid))
    return keys


def use_pgp_key(fingerprint: str) -> None:
    """Sign with this gpg key for every project."""
    _check(_git('config', '--global', 'patatt.signingkey', f'openpgp:{fingerprint}'))


def new_patatt_key(name: str) -> str:
    """A new patatt ed25519 key named *name* (patatt genkey), signing for every project;
    its public key is in patatt's data directory, to publish."""
    patatt = pathlib.Path(sys.executable).parent / 'patatt'
    command = [str(patatt)] if patatt.exists() else ['patatt']
    run = subprocess.run(
        [*command, 'genkey', '-n', name], capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    if run.returncode != 0:
        lines = (run.stderr or run.stdout).strip().splitlines()
        raise RuntimeError(lines[-1] if lines else 'patatt genkey failed')
    _check(_git('config', '--global', 'patatt.signingkey', f'ed25519:{name}'))
    _check(_git('config', '--global', 'patatt.selector', name))
    return name


def stop_signing() -> None:
    """No key for every project: messages go unsigned (a checkout's own key stays)."""
    _git('config', '--global', '--unset-all', 'patatt.signingkey')
    _git('config', '--global', '--unset-all', 'patatt.selector')


def _check(run: subprocess.CompletedProcess[str]) -> None:
    if run.returncode != 0:
        raise RuntimeError(run.stderr.strip() or 'git config failed')
