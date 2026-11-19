"""Contract: F7 opens Settings: which key signs mail for every project, and how to set
one up there (an existing PGP key, a new patatt key) or stop signing; each after a
confirmation, in the global git configuration."""

from gitutil import git_config

import b4_maintainer.adapters.signing as signing
from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import Confirm, MaintainerApp
from b4_maintainer.branchscreen import Ask, Pick

FPR = '0123456789ABCDEF0123456789ABCDEFAAAABBBB'


async def settle(pilot):
    for _ in range(6):  # An action's worker, then the status reread's.
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


async def open_settings(pilot):
    from b4_maintainer.settingsscreen import SettingsScreen

    await settle(pilot)
    await pilot.press('f7')
    await settle(pilot)
    assert isinstance(pilot.app.screen, SettingsScreen)
    return pilot.app.screen


async def test_f7_shows_the_signing_key_and_sets_a_pgp_one(demo_root, home, monkeypatch):
    monkeypatch.setattr(
        signing, 'gpg_secret_keys', lambda: [(FPR, 'Example Maintainer <m@example.org>')]
    )
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(160, 40)) as pilot:
        screen = await open_settings(pilot)
        assert 'No signing key' in screen.status_text()
        screen.choose('pgp')
        await settle(pilot)
        pick = app.screen
        assert isinstance(pick, Pick) and pick.choices[0][0] == FPR
        await pilot.press('enter')
        await settle(pilot)
        assert isinstance(app.screen, Confirm) and 'every project' in app.screen.prompt
        await pilot.press('y')
        await settle(pilot)
        assert git_config(home, 'patatt.signingkey') == f'openpgp:{FPR}'
        assert 'openpgp:0123' in screen.status_text()

        screen.choose('stop')
        await settle(pilot)
        await pilot.press('y')
        await settle(pilot)
        assert git_config(home, 'patatt.signingkey') is None
        assert 'No signing key' in screen.status_text()


async def test_f7_makes_a_new_patatt_key(demo_root, home):
    (home / '.gitconfig').write_text('[user]\n\tname = M\n\temail = m@example.org\n')
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(160, 40)) as pilot:
        screen = await open_settings(pilot)
        screen.choose('patatt')
        await settle(pilot)
        assert isinstance(app.screen, Ask)
        app.screen.query_one('Input').value = 'mykey'
        await pilot.press('enter')
        await settle(pilot)
        assert isinstance(app.screen, Confirm) and 'publish' in app.screen.prompt
        await pilot.press('y')
        await settle(pilot)
        assert git_config(home, 'patatt.signingkey') == 'ed25519:mykey'
        assert 'ed25519:mykey' in screen.status_text()


async def test_without_gpg_keys_it_says_so(demo_root, home, monkeypatch):
    monkeypatch.setattr(signing, 'gpg_secret_keys', lambda: [])
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(160, 40)) as pilot:
        screen = await open_settings(pilot)
        screen.choose('pgp')
        await settle(pilot)
        assert app.screen is screen and 'no secret keys' in screen.status_text()


async def test_f7_chooses_the_default_model_for_this_machine(demo_root, home, tmp_path):
    from b4_maintainer.modelsetting import DefaultModel
    from b4_maintainer.sessions.models import model_choices, parse_catalog

    class Hub:
        def models(self, presets, default=''):
            listing = 'provider model thinking\nalpha alpha-large yes\nbeta beta-pro no\n'
            return model_choices(parse_catalog(listing), presets, default)

    root = tmp_path / 'ws'
    root.mkdir()
    (root / 'workspace.toml').write_text(
        '[[models]]\nid = "deep"\nmodel = "alpha/alpha-large"\nthinking = "high"\n'
    )
    app = MaintainerApp(FixtureSource(demo_root).load, default_model=DefaultModel(root, Hub()))
    async with app.run_test(size=(160, 40)) as pilot:
        screen = await open_settings(pilot)
        assert screen.model_text() == ''  # pi's own default.
        screen.choose('model')
        await settle(pilot)
        pick = app.screen
        assert isinstance(pick, Pick)
        keys = [k for k, _label in pick.choices]
        assert keys == ['-', 'deep', 'alpha/alpha-large', 'beta/beta-pro']
        await pilot.press('down', 'down', 'down', 'enter')
        await settle(pilot)
        local = root / 'var' / 'workspace.toml'
        assert 'default_model = "beta/beta-pro"' in local.read_text()
        assert screen.model_text() == 'beta/beta-pro'
        assert 'Sessions use beta/beta-pro' in screen.status_text()
        screen.choose('model')
        await settle(pilot)
        await pilot.press('enter')  # pi's own default again.
        await settle(pilot)
        assert 'default_model' not in local.read_text()
        assert screen.model_text() == ''


async def test_settings_shows_only_its_own_keys(demo_root, home):
    app = MaintainerApp(FixtureSource(demo_root).load)
    async with app.run_test(size=(160, 40)) as pilot:
        screen = await open_settings(pilot)
        shown = {b.binding.action for b in screen.active_bindings.values() if b.enabled}
        assert 'back' in shown and 'quit' in shown
        assert not shown & {'preview', 'search', 'sort_latest'}  # The list's, not here.


async def test_tab_moves_between_the_settings_boxes(demo_root, home, tmp_path):
    from b4_maintainer.modelsetting import DefaultModel

    class Hub:
        def models(self, presets, default=''):
            return []

    root = tmp_path / 'ws'
    root.mkdir()
    (root / 'workspace.toml').write_text('')
    from b4_maintainer.upstreams import UpstreamService

    app = MaintainerApp(
        FixtureSource(demo_root).load,
        default_model=DefaultModel(root, Hub()),
        upstreams=UpstreamService(root),
    )
    async with app.run_test(size=(160, 40)) as pilot:
        await open_settings(pilot)
        boxes = ['settings-actions', 'settings-models', 'settings-upstreams']
        assert app.focused.id == boxes[0]
        for expected in [*boxes[1:], boxes[0]]:  # Box to box, and round again.
            await pilot.press('tab')
            assert app.focused.id == expected
        await pilot.press('shift+tab')
        assert app.focused.id == boxes[-1]
