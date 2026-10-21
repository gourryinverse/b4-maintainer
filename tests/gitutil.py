"""Git helpers for tests that need scratch checkouts."""

import subprocess


def git(*args, cwd):
    return subprocess.run(
        ['git', *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def git_config(repo, key):
    result = subprocess.run(
        ['git', 'config', '--get', key], cwd=repo, capture_output=True, text=True
    )
    return result.stdout.strip() or None


def make_checkout(path):
    path.mkdir(parents=True)
    git('init', '-q', '-b', 'master', cwd=path)
    identity = ['-c', 'user.name=Example Maintainer', '-c', 'user.email=maintainer@example.org']
    git(*identity, 'commit', '-q', '--allow-empty', '-m', 'init', cwd=path)
    return path
