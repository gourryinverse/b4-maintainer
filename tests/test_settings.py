"""Contract: F6 opens Settings: which key signs mail for every project, and how to set
one up there (an existing PGP key, a new patatt key) or stop signing; each after a
confirmation, in the global git configuration."""

from gitutil import git_config

import b4_maintainer.adapters.signing as signing
from b4_maintainer.adapters.fixture import FixtureSource
from b4_maintainer.app import Confirm, MaintainerApp
from b4_maintainer.branchscreen import Ask, Pick

FPR = '0123456789ABCDEF0123456789ABCDEFAAAABBBB'


async def settle(pilot):
    for _ in range(3):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()


async def open_settings(pilot):
    from b4_maintainer.settingsscreen import SettingsScreen

    await settle(pilot)
    await pilot.press('f6')
    await settle(pilot)
    assert isinstance(pilot.app.screen, SettingsScreen)
    return pilot.app.screen


async def test_f6_shows_the_signing_key_and_sets_a_pgp_one(demo_root, home, monkeypatch):
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


async def test_f6_makes_a_new_patatt_key(demo_root, home):
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
