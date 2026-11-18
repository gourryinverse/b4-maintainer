"""A series posted twice, as `b4 send -o` writes it from a prep branch, for forge tests."""

import pytest
from gitutil import git

from b4_maintainer.adapters.prep import cut_prep, preview_send


def posted_rounds(tmp_path_factory):
    """Two rounds of a two-patch series, as `b4 send -o` writes them from a prep branch
    (made once: b4 runs a few times)."""
    tmp_path = tmp_path_factory.mktemp('posted')
    home = tmp_path / 'home'
    home.mkdir()
    (home / '.gitconfig').write_text(
        '[user]\n\tname = Example Maintainer\n\temail = maintainer@example.org\n'
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv('HOME', str(home))
        for var in ('XDG_DATA_HOME', 'XDG_CACHE_HOME', 'XDG_CONFIG_HOME'):
            mp.setenv(var, str(home / var.lower()))
        mp.setenv('GIT_CONFIG_NOSYSTEM', '1')
        return _post_twice(tmp_path)


def _post_twice(tmp_path):
    repo = tmp_path / 'linux'
    repo.mkdir()
    git('init', '-q', '-b', 'main', cwd=repo)
    (repo / 'README').write_text('base\n')
    git('add', 'README', cwd=repo)
    git('commit', '-q', '-m', 'start', cwd=repo)
    wt = tmp_path / 'wt'
    branch = cut_prep(repo, wt, 'demo', 'main', 'demo: two changes\n\nA cover letter.\n')
    git('switch', '-q', branch, cwd=wt)
    for n in (1, 2):
        (wt / f'f{n}').write_text(f'{n}\n')
        git('add', f'f{n}', cwd=wt)
        git('commit', '-q', '-m', f'demo: add f{n}\n\nBecause {n}.', cwd=wt)
    git('switch', '-q', '--detach', cwd=wt)
    rounds = []
    for n in (1, 2):
        code, out, messages = preview_send(wt, branch, tmp_path / f'out{n}', no_sign=True)
        assert code == 0, out
        rounds.append(messages)
    return rounds
