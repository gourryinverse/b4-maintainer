"""Contract: Settings' upstream service tracks a branch (its remote fetched, then the ref
written), changes how membership in it counts (authority, view, ends review, label),
and untracks it (the ref gone, then its fetch line, then the remote once unused). A
branch a maintainer base builds on is not untracked; a failed write undoes the fetch."""

import pytest
from gitutil import git, make_checkout
from test_remotes import upstream  # noqa: F401 (fixture)

from b4_maintainer.adapters import remotes
from b4_maintainer.model import Authority
from b4_maintainer.projectsetup import read_config
from b4_maintainer.upstreams import UpstreamService
from b4_maintainer.workspace import Workspace, make_ref

CONFIG = """\
[[projects]]
id = "linux"
path = "projects/linux"
lists = ["linux-mm@kvack.org"]
refs = [
  { label = "m", name = "mainline/master", authority = "mainline" },
]
bases = [{ branch = "me/base", upstream = "mainline/master" }]
"""


@pytest.fixture
def root(tmp_path):
    (tmp_path / 'workspace.toml').write_text(CONFIG)
    make_checkout(tmp_path / 'projects' / 'linux')
    return tmp_path


def refs(root):
    return Workspace.from_config(read_config(root)).project('linux').refs


def test_track_change_and_untrack_a_branch(root, upstream):  # noqa: F811
    service = UpstreamService(root)
    checkout = root / 'projects' / 'linux'
    core = make_ref('c', 'core/for-next', Authority.ACCEPTANCE, 'Core')
    service.add('linux', 'core', upstream.as_uri(), 'for-next', core)
    assert refs(root)[-1] == core
    assert remotes.tracked(checkout, 'core') == ['for-next']
    assert [(u.ref.name, u.remote, u.url) for u in service.listing()] == [
        ('mainline/master', '', ''),  # No such remote here: a local branch.
        ('core/for-next', 'core', upstream.as_uri()),
    ]
    service.update('linux', 'core/for-next', make_ref('k', 'core/for-next', Authority.PRELIMINARY))
    assert refs(root)[-1] == make_ref('k', 'core/for-next', Authority.PRELIMINARY)
    note = service.remove('linux', 'core/for-next')
    assert 'remote core removed' in note
    assert [r.name for r in refs(root)] == ['mainline/master']
    assert remotes.remotes(checkout) == {}


def test_a_branch_a_base_builds_on_stays(root):
    with pytest.raises(ValueError, match='me/base'):
        UpstreamService(root).remove('linux', 'mainline/master')
    assert [r.name for r in refs(root)] == ['mainline/master']


def test_a_failed_write_undoes_the_fetch(root, upstream):  # noqa: F811
    service = UpstreamService(root)
    clash = make_ref('m', 'core/for-next', Authority.ACCEPTANCE)  # Label m is taken.
    with pytest.raises(ValueError):
        service.add('linux', 'core', upstream.as_uri(), 'for-next', clash)
    assert remotes.remotes(root / 'projects' / 'linux') == {}
    assert git('branch', '-r', cwd=root / 'projects' / 'linux') == ''
