"""Contract: --showcase DIR writes a synthetic live workspace once (example.org identities
and lists, nothing read or written outside DIR) and opens it like any live workspace:
every view has something to show, as do the Workbench, Branches, and Settings tabs, and
replies are dry runs. An existing showcase reopens as it is; a directory that is neither
new, empty, nor a showcase is refused."""

import pytest

from b4_maintainer.adapters.live import LiveSource
from b4_maintainer.showcase import build_showcase
from b4_maintainer.views import ViewIndex


@pytest.fixture(scope='module')
def showcase(tmp_path_factory):
    import os

    home = tmp_path_factory.mktemp('home')
    old = os.environ.get('HOME')
    os.environ['HOME'] = str(home)  # b4 and git must keep to DIR.
    try:
        yield build_showcase(tmp_path_factory.mktemp('ws') / 'showcase')
    finally:
        if old is not None:
            os.environ['HOME'] = old
    # No git configuration or b4 state of its own outside DIR (tools' own logs aside).
    leaked = ['.gitconfig', '.config/b4', '.local/share/b4', '.cache/b4', '.b4-config']
    assert [p for p in leaked if (home / p).exists()] == []


def test_every_view_has_something_to_show(showcase):
    snapshot = LiveSource(showcase).load()
    index = ViewIndex(snapshot.workspace, snapshot.items)
    empty = [v.label for v in index.views if not index.count(v.id)]
    assert empty == []
    for wanted in ('discussions', 'robots', 'responses', 'review', 'drafts', 'unmerged'):
        assert index.count(wanted), wanted
    assert [v.label for v in index.views if v.kind == 'ref'] == [
        'MM-stable',
        'MM-unstable',
        'Core',
        'Mine',
    ]
    assert snapshot.workspace.warnings == ()
    people = {i.series.author_email for i in snapshot.items}
    assert all(p.endswith('@example.org') for p in people)


def test_the_tabs_have_something_to_show(showcase):
    from b4_maintainer.cli import _sessions
    from b4_maintainer.upstreams import UpstreamService
    from b4_maintainer.workbench import WorkbenchStore

    benches, problems = WorkbenchStore(showcase / 'workbenches').list()
    assert [b.label for b in benches] == ['MM fixes'] and problems == []
    hub = _sessions(showcase, LiveSource(showcase).workspace())
    filed = {r.workbench for r in hub.records()}
    assert filed == {'mm-fixes'}
    assert len(UpstreamService(showcase).listing()) == 7
    import subprocess

    prep = subprocess.run(
        ['git', 'branch', '--list', 'b4/*'],
        cwd=showcase / 'projects' / 'linux',
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert 'b4/hotplug-weights' in prep


def test_showcase_reopens_and_refuses_other_directories(showcase, tmp_path):
    from b4_maintainer.cli import build_app

    app = build_app(['--showcase', str(showcase)])
    assert app._actions.email_dryrun is True
    (tmp_path / 'notes.txt').write_text('mine')
    with pytest.raises(SystemExit):
        build_app(['--showcase', str(tmp_path)])
    assert (tmp_path / 'notes.txt').read_text() == 'mine'
