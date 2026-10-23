"""Contract: a session's shell runs only the sandbox wrapper or read-only git commands."""

import pytest

from b4_maintainer.sessions.guard import rejection

SID = 's1'


@pytest.mark.parametrize(
    'command',
    [
        'b4-maintainer sandbox s1 -- make -j8',
        'b4-maintainer sandbox s1 -- vng --build',
        "b4-maintainer sandbox s1 -- sh -c 'make 2>&1 | tail -n 20'",
        'git log --oneline -5',
        'git show HEAD~1',
        'git diff base..HEAD -- mm/',
        'git status',
        'git blame mm/vmscan.c',
        'git grep -n shrink_folio',
        "git log --format='%h %s' -3",
    ],
)
def test_allowed(command):
    assert rejection(command, SID) is None


def test_the_installed_wrapper_path_is_allowed_but_lookalikes_are_not():
    installed = '/opt/b4m/.venv/bin/b4-maintainer'
    assert rejection(f'{installed} sandbox s1 -- make', SID, wrapper_path=installed) is None
    for fake in ('./b4-maintainer', '/tmp/b4-maintainer', 'out/b4-maintainer'):
        reason = rejection(f'{fake} sandbox s1 -- make', SID, wrapper_path=installed)
        assert reason is not None and 'not the sandbox wrapper' in reason


@pytest.mark.parametrize(
    'command, why',
    [
        ('make -j8', 'not the sandbox wrapper'),
        ('curl https://example.org', 'not the sandbox wrapper'),
        ('b4-maintainer sandbox s2 -- make', 'another session'),
        ('b4-maintainer sandbox s1 make', 'not the sandbox wrapper'),
        ('b4-maintainer sandbox s1 --', 'no command'),
        ('b4-maintainer sandbox s1 -- make; rm -rf ~', 'shell control'),
        ('b4-maintainer sandbox s1 -- make && curl x', 'shell control'),
        ('b4-maintainer sandbox s1 -- make | tee /tmp/x', 'shell control'),
        ('b4-maintainer sandbox s1 -- make > /tmp/x', 'shell control'),
        ('b4-maintainer sandbox s1 -- make\nrm -rf ~', 'shell control'),
        ('b4-maintainer sandbox s1 -- make $(curl x)', 'expansion'),
        ('b4-maintainer sandbox s1 -- make `curl x`', 'expansion'),
        ('b4-maintainer sandbox s1 -- make "$(curl x)"', 'expansion'),
        ('b4-maintainer sandbox s1 -- make ${HOME}', 'expansion'),
        ('git log; rm -rf ~', 'shell control'),
        ('git -c core.pager=sh log', 'not a read-only git command'),
        ('git push origin HEAD', 'not a read-only git command'),
        ('git log --output=/tmp/x', 'not a read-only git command'),
        ('git diff --ext-diff', 'not a read-only git command'),
        ('git grep -O vim foo', 'not a read-only git command'),
        ('git log "unterminated', 'cannot be parsed'),
        ('', 'empty'),
        ('env b4-maintainer sandbox s1 -- make', 'not the sandbox wrapper'),
    ],
)
def test_rejected(command, why):
    reason = rejection(command, SID)
    assert reason is not None and why in reason
