"""Contract: a workspace is recreated on another machine from its state repository."""

import json

from gitutil import git, git_config, make_checkout
from test_b4state import add_tracked

from b4_maintainer.adapters.b4state import tracked_series
from b4_maintainer.cli import build_app, main, project_main
from b4_maintainer.projectsetup import (
    clone_checkout,
    config_path,
    describe_checkout,
    setup_workspace,
)

CONFIG = '[[projects]]\nid = "demo"\npath = "projects/demo"\nlists = ["demo@lists.example.org"]\n'


def upstream(tmp_path, name='upstream'):
    """A source repository serving partial clones, as git.kernel.org does."""
    path = make_checkout(tmp_path / name)
    git('config', 'uploadpack.allowFilter', 'true', cwd=path)
    git('config', 'uploadpack.allowAnySHA1InWant', 'true', cwd=path)
    git('branch', 'next', cwd=path)
    return path


def configured(checkout, source):
    git('remote', 'add', 'mainline', f'file://{source}', cwd=checkout)
    git(
        'config',
        '--replace-all',
        'remote.mainline.fetch',
        '+refs/heads/master:refs/remotes/mainline/master',
        cwd=checkout,
    )
    git(
        'config',
        '--add',
        'remote.mainline.fetch',
        '+refs/heads/next:refs/remotes/mainline/next',
        cwd=checkout,
    )
    git('config', 'remote.mainline.promisor', 'true', cwd=checkout)
    git('config', 'remote.mainline.partialclonefilter', 'blob:none', cwd=checkout)
    git('fetch', '-q', 'mainline', cwd=checkout)
    git('checkout', '-q', '-B', 'master', '--track', 'mainline/master', cwd=checkout)
    git('config', 'b4.sashiko-url', 'https://sashiko.example.org', cwd=checkout)
    git('config', 'b4.review-no-patatt-sign', 'yes', cwd=checkout)
    return checkout


def test_config_lives_in_state_unless_the_root_has_one(tmp_path):
    (tmp_path / 'state').mkdir()
    assert config_path(tmp_path) == tmp_path / 'state' / 'workspace.toml'
    (tmp_path / 'workspace.toml').write_text('')
    assert config_path(tmp_path) == tmp_path / 'workspace.toml'


def test_a_state_config_opens_live(tmp_path):
    (tmp_path / 'state').mkdir()
    (tmp_path / 'state' / 'workspace.toml').write_text('')
    app = build_app([str(tmp_path)])
    assert app._actions is not None


def test_project_add_appends_to_the_state_config(ws, home):
    (ws / 'state').mkdir()
    assert project_main(['add', '-C', str(ws), 'demo']) == 0
    assert 'id = "demo"' in (ws / 'state' / 'workspace.toml').read_text()
    assert not (ws / 'workspace.toml').exists()


def test_describe_records_remotes_branch_and_b4_settings(ws, home, tmp_path):
    source = upstream(tmp_path)
    checkout = configured(ws / 'projects' / 'demo', source)
    git('config', 'b4.data-dir', '/elsewhere', cwd=checkout)
    desc = describe_checkout(checkout)
    assert desc == {
        'branch': 'master',
        'upstream': {'remote': 'mainline', 'merge': 'refs/heads/master'},
        'remotes': [
            {
                'name': 'mainline',
                'url': f'file://{source}',
                'fetch': [
                    '+refs/heads/master:refs/remotes/mainline/master',
                    '+refs/heads/next:refs/remotes/mainline/next',
                ],
                'filter': 'blob:none',
            }
        ],
        'b4': {'b4.review-no-patatt-sign': 'yes', 'b4.sashiko-url': 'https://sashiko.example.org'},
    }


def test_clone_recreates_a_blobless_checkout(ws, home, tmp_path):
    source = upstream(tmp_path)
    desc = describe_checkout(configured(ws / 'projects' / 'demo', source))
    target = tmp_path / 'new' / 'projects' / 'demo'
    clone_checkout(target, desc)
    assert describe_checkout(target) == {**desc, 'b4': {}}  # b4 settings are setup's.
    assert git('rev-parse', 'HEAD', cwd=target) == git('rev-parse', 'master', cwd=source)
    assert git('rev-parse', 'mainline/next', cwd=target)
    assert git_config(target, 'remote.mainline.promisor') == 'true'


def write_state(root, desc, records=None):
    state = root / 'state'
    (state / 'projects' / 'demo' / 'series').mkdir(parents=True)
    (state / 'workspace.toml').write_text(CONFIG)
    (state / 'projects' / 'demo' / 'checkout.json').write_text(json.dumps(desc))
    for cid, rec in (records or {}).items():
        path = state / 'projects' / 'demo' / 'series' / f'{cid}.json'
        path.write_text(json.dumps({**rec, 'updated': '2026-10-22T00:00:00+00:00'}))
    flags = 'cover@example.org\t2026-10-20T00:00:00+00:00\tSeen\t\n'
    (state / 'b4').mkdir()
    (state / 'b4' / 'flags.tsv').write_text(flags)


def test_setup_clones_enrolls_and_imports(ws, home, tmp_path):
    from b4_maintainer.adapters.b4state import all_flags, export_series

    source = upstream(tmp_path)
    here = configured(ws / 'projects' / 'demo', source)
    assert project_main(['add', '-C', str(ws), 'demo']) == 0
    add_tracked(here, 'cid-1', 'cover@example.org', ['p1@example.org'])

    root = tmp_path / 'machine-b'
    write_state(root, describe_checkout(here), export_series(here, 'demo'))
    report = setup_workspace(root)
    checkout = root / 'projects' / 'demo'
    assert any('cloned' in line for line in report)
    assert git_config(checkout, 'b4.data-dir') == '../../var/b4/data'
    assert git_config(checkout, 'b4.sashiko-url') == 'https://sashiko.example.org'
    [entry] = tracked_series(checkout, 'demo')
    assert entry.change_id == 'cid-1'
    assert [f['msgid'] for f in all_flags(checkout)] == ['cover@example.org']


def test_setup_leaves_an_existing_checkout_and_reports_remote_differences(ws, home, tmp_path):
    source = upstream(tmp_path)
    desc = describe_checkout(configured(ws / 'projects' / 'demo', source))
    root = tmp_path / 'machine-b'
    existing = make_checkout(root / 'projects' / 'demo')
    head = git('rev-parse', 'HEAD', cwd=existing)
    write_state(root, desc)
    report = setup_workspace(root)
    assert git('rev-parse', 'HEAD', cwd=existing) == head
    assert git('remote', cwd=existing) == ''
    assert any('mainline' in line and 'missing' in line for line in report)


def test_setup_and_sync_commands(ws, home, tmp_path, capsys):
    source = upstream(tmp_path)
    desc = describe_checkout(configured(ws / 'projects' / 'demo', source))
    root = tmp_path / 'machine-b'
    write_state(root, desc)
    try:
        main(['setup', '-C', str(root)])
    except SystemExit as e:
        assert e.code == 0
    assert (root / 'projects' / 'demo' / '.git').is_dir()
    git('init', '-q', '-b', 'main', cwd=root / 'state')
    try:
        main(['sync', '-C', str(root)])
    except SystemExit as e:
        assert e.code == 0
    assert 'projects/demo/checkout.json' in git('ls-files', cwd=root / 'state')
    assert 'State committed' in capsys.readouterr().out
