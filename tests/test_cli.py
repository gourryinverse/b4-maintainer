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


def test_mail_flags_mirror_b4_review(tmp_path, monkeypatch):
    (tmp_path / 'workspace.toml').write_text('')
    monkeypatch.chdir(tmp_path)
    app = build_app(['--email-dry-run', '--no-sign'])
    assert app._actions.email_dryrun and app._actions.no_sign
    plain = build_app([])
    assert not plain._actions.email_dryrun and not plain._actions.no_sign


@pytest.mark.parametrize(
    'config,tmux,expected',
    [
        ('', '/tmp/tmux-1/default,1,0', 'tmux'),
        ('', None, None),
        ('[drafts]\neditor = "app"\n', '/tmp/tmux-1/default,1,0', None),
        ('[drafts]\neditor = "tmux"\ntmux_width = 120\n', '/tmp/tmux-1/default,1,0', 'tmux'),
        ('[drafts]\neditor = "tmux"\n', None, None),  # Not inside tmux: the in-app pane.
    ],
)
def test_draft_editor_follows_config_and_tmux(tmp_path, monkeypatch, config, tmux, expected):
    from b4_maintainer.adapters.tmux import TmuxEditor

    (tmp_path / 'workspace.toml').write_text(config)
    if tmux:
        monkeypatch.setenv('TMUX', tmux)
        monkeypatch.setenv('TMUX_PANE', '%3')
    else:
        monkeypatch.delenv('TMUX', raising=False)
    app = build_app([str(tmp_path)])
    editor = app._editor
    if expected is None:
        assert editor is None
    else:
        assert isinstance(editor, TmuxEditor)
        assert editor.width == (120 if 'tmux_width' in config else 100)


def test_sigusr1_dumps_every_threads_stack(tmp_path, monkeypatch):
    import os
    import signal

    from b4_maintainer.cli import enable_stack_dumps

    path = enable_stack_dumps(tmp_path)
    try:
        assert path == tmp_path / f'b4-maintainer-{os.getpid()}.stacks'
        os.kill(os.getpid(), signal.SIGUSR1)
        text = path.read_text()
        assert 'test_sigusr1_dumps_every_threads_stack' in text
    finally:
        import faulthandler

        faulthandler.unregister(signal.SIGUSR1)


def test_live_sessions_run_pi_in_tmux_with_the_workspace_settings(tmp_path):
    from b4_maintainer.sessions.tmuxrunner import TmuxRunner

    (tmp_path / 'workspace.toml').write_text(
        '[sessions]\npi = "/opt/pi/bin/pi"\npi_args = ["--site-option"]\n'
        '[[projects]]\nid = "linux"\npath = "projects/linux"\n'
    )
    hub = build_app([str(tmp_path)])._sessions
    runner = hub.runner
    assert isinstance(runner, TmuxRunner)
    root = tmp_path.resolve()
    assert (runner.site.root, runner.site.pi, runner.site.args) == (
        root,
        '/opt/pi/bin/pi',
        ('--site-option',),
    )
    assert runner.site.checkouts == {'linux': str(root / 'projects' / 'linux')}
    assert runner.site.hook.endswith('b4-maintainer')
    assert (hub.profiles_dir, hub.skills_dir) == (
        root / 'state' / 'profiles',
        root / 'state' / 'skills',
    )
    assert hub.store.root == root / 'var' / 'sessions'


def test_demo_sessions_also_run_in_tmux(tmp_path):
    from b4_maintainer.sessions.tmuxrunner import TmuxRunner

    hub = build_app(['--demo', str(tmp_path / 'demo')])._sessions
    assert isinstance(hub.runner, TmuxRunner) and hub.runner.site.root == tmp_path / 'demo'


def test_the_app_knows_its_own_tmux_pane(tmp_path, monkeypatch):
    (tmp_path / 'workspace.toml').write_text('')
    monkeypatch.setenv('TMUX_PANE', '%7')
    assert build_app([str(tmp_path)])._app_pane == '%7'
    assert build_app(['--demo', str(tmp_path / 'demo')])._app_pane == '%7'
    monkeypatch.delenv('TMUX_PANE')
    assert build_app([str(tmp_path)])._app_pane == ''
