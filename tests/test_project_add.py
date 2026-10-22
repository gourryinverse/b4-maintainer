"""Contract: `b4-maintainer project add` registers a checkout with workspace-local b4 state."""

import tomllib

import pytest
from gitutil import git, git_config, make_checkout

from b4_maintainer.cli import project_main
from b4_maintainer.model import Authority
from b4_maintainer.workspace import Workspace


def add(ws, *args):
    return project_main(['add', '-C', str(ws), *args])


def load(ws):
    return Workspace.from_config(tomllib.loads((ws / 'workspace.toml').read_text()))


def test_add_registers_project_and_enrolls_with_workspace_state(ws, home):
    refs = ['--ref', 'm:origin/master:mainline', '--ref', 'n:next/master:acceptance']
    rc = add(ws, 'demo', '--list', 'Demo@Lists.Example.org', *refs)
    assert rc == 0

    project = load(ws).project('demo')
    assert project.path == 'projects/demo'
    assert project.lists == ('demo.lists.example.org',)
    assert [(r.label, r.name, r.authority) for r in project.refs] == [
        ('m', 'origin/master', Authority.MAINLINE),
        ('n', 'next/master', Authority.ACCEPTANCE),
    ]

    checkout = ws / 'projects' / 'demo'
    assert git_config(checkout, 'b4.data-dir') == '../../var/b4/data'
    assert git_config(checkout, 'b4.cache-dir') == '../../var/b4/cache'
    assert (ws / 'var' / 'b4' / 'data' / 'review' / 'demo.sqlite3').is_file()
    assert (checkout / '.git' / 'b4-review' / 'metadata.json').is_file()
    assert not (home / 'xdg-data').exists()
    assert not (home / '.local').exists()


def test_add_preserves_existing_config(ws):
    original = '# hand-written\nme = ["maintainer@example.org"]\n\n[layout]\nthree_pane = 170\n'
    (ws / 'workspace.toml').write_text(original)
    assert add(ws, 'demo') == 0

    text = (ws / 'workspace.toml').read_text()
    assert text.startswith(original)
    config = tomllib.loads(text)
    assert config['me'] == ['maintainer@example.org']
    assert config['layout'] == {'three_pane': 170}
    assert load(ws).project('demo') is not None


def test_add_second_project(ws):
    make_checkout(ws / 'projects' / 'other')
    assert add(ws, 'demo') == 0
    assert add(ws, 'other', '--label', 'Other', '--short', 'oth') == 0

    workspace = load(ws)
    assert [p.id for p in workspace.projects] == ['demo', 'other']
    assert workspace.project('other').label == 'Other'
    assert (ws / 'var' / 'b4' / 'data' / 'review' / 'other.sqlite3').is_file()


def test_add_custom_path(ws):
    make_checkout(ws / 'trees' / 'demo2')
    assert add(ws, 'demo2', '--path', 'trees/demo2') == 0
    assert load(ws).project('demo2').path == 'trees/demo2'
    assert git_config(ws / 'trees' / 'demo2', 'b4.data-dir') == '../../var/b4/data'


def test_duplicate_id_is_refused(ws, capsys):
    assert add(ws, 'demo') == 0
    before = (ws / 'workspace.toml').read_text()
    assert add(ws, 'demo') != 0
    assert 'already registered' in capsys.readouterr().err
    assert (ws / 'workspace.toml').read_text() == before


def test_missing_checkout_is_refused(ws, capsys):
    assert add(ws, 'absent') != 0
    assert 'not a git checkout' in capsys.readouterr().err
    assert not (ws / 'workspace.toml').exists()
    assert not (ws / 'var').exists()


def test_conflicting_b4_data_dir_is_refused(ws, capsys):
    checkout = ws / 'projects' / 'demo'
    git('config', 'b4.data-dir', '/elsewhere', cwd=checkout)
    assert add(ws, 'demo') != 0
    assert 'b4.data-dir' in capsys.readouterr().err
    assert git_config(checkout, 'b4.data-dir') == '/elsewhere'
    assert git_config(checkout, 'b4.cache-dir') is None
    assert not (checkout / '.git' / 'b4-review').exists()
    assert not (ws / 'workspace.toml').exists()


def test_enrolled_elsewhere_is_refused(ws, capsys):
    checkout = ws / 'projects' / 'demo'
    (checkout / '.git' / 'b4-review').mkdir()
    (checkout / '.git' / 'b4-review' / 'metadata.json').write_text('{"identifier": "old"}')
    assert add(ws, 'demo') != 0
    assert 'already enrolled' in capsys.readouterr().err
    assert git_config(checkout, 'b4.data-dir') is None
    assert not (ws / 'workspace.toml').exists()


@pytest.mark.parametrize('ref', ['m', 'm:origin/master', 'm:origin/master:bogus', 'mm:x:mainline'])
def test_bad_ref_is_refused(ws, ref, capsys):
    with pytest.raises(SystemExit) as exc:
        add(ws, 'demo', '--ref', ref)
    assert exc.value.code != 0
    assert '--ref' in capsys.readouterr().err
    assert not (ws / 'workspace.toml').exists()


def test_invalid_existing_config_is_refused(ws, capsys):
    (ws / 'workspace.toml').write_text('projects = [ { id = "x" } ]\n')
    assert add(ws, 'demo') != 0
    assert 'workspace.toml' in capsys.readouterr().err
    assert (ws / 'workspace.toml').read_text() == 'projects = [ { id = "x" } ]\n'
    assert git_config(ws / 'projects' / 'demo', 'b4.data-dir') is None


def test_project_toml_round_trips_views_and_review_ends():
    from b4_maintainer.projectsetup import project_toml
    from b4_maintainer.workspace import Project, make_ref

    project = Project(
        id='demo',
        label='demo',
        short='demo',
        lists=('demo.lists.example.org',),
        refs=(
            make_ref('m', 'master', Authority.MAINLINE),
            make_ref('n', 'next', Authority.ACCEPTANCE, ends_review=True),
            make_ref('u', 'unstable', Authority.PRELIMINARY, view='Unstable'),
            make_ref('x', 'odd', Authority.MAINLINE, ends_review=False),
        ),
        path='projects/demo',
    )
    loaded = Workspace.from_config(tomllib.loads(project_toml(project))).project('demo')
    assert loaded == project
