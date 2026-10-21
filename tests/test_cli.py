import pytest

from b4_maintainer.cli import build_app


def test_demo_builds_synthetic_workspace(tmp_path):
    app = build_app(['--demo', str(tmp_path / 'demo')])
    assert (tmp_path / 'demo' / 'workspace.json').exists()
    snapshot = app._loader()
    assert len(snapshot.items) > 0
    assert all(i.series.author_email.endswith('@example.org') for i in snapshot.items)


def test_workspace_directory_is_loaded(demo_root):
    app = build_app([str(demo_root)])
    assert app._loader().items


def test_missing_workspace_is_an_error(tmp_path, capsys):
    with pytest.raises(SystemExit):
        build_app([str(tmp_path / 'absent')])
    assert 'workspace.json' in capsys.readouterr().err
