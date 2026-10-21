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


def test_missing_workspace_is_an_error(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        build_app([str(tmp_path / 'absent')])
    err = capsys.readouterr().err
    assert 'workspace.toml' in err and 'workspace.json' in err


def test_no_arguments_opens_live_workspace_in_current_directory(tmp_path, monkeypatch):
    from b4_maintainer.adapters.live import LiveSource

    (tmp_path / 'workspace.toml').write_text('')
    monkeypatch.chdir(tmp_path)
    app = build_app([])
    assert isinstance(app._actions, LiveSource)
    assert app._actions.root == tmp_path.resolve()
    assert app._loader().items == ()


def test_directory_with_workspace_toml_opens_live(tmp_path):
    (tmp_path / 'workspace.toml').write_text('')
    assert build_app([str(tmp_path)])._actions is not None


def test_fixture_directory_has_no_live_actions(demo_root):
    assert build_app([str(demo_root)])._actions is None


def test_neither_config_names_both(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        build_app([])
    err = capsys.readouterr().err
    assert 'workspace.toml' in err and 'workspace.json' in err


def test_project_name_opens_live_workspace_on_its_view(tmp_path, monkeypatch):
    (tmp_path / 'workspace.toml').write_text('[[projects]]\nid = "linux"\n')
    monkeypatch.chdir(tmp_path)
    app = build_app(['linux'])
    assert app._actions is not None
    assert app.current_view == 'project:linux'


def test_unknown_project_name_lists_known_projects(tmp_path, capsys, monkeypatch):
    (tmp_path / 'workspace.toml').write_text('[[projects]]\nid = "linux"\n')
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        build_app(['qemu'])
    assert 'linux' in capsys.readouterr().err
