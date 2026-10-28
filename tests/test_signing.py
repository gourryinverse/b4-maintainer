"""Contract: the signing key patatt uses, for every project: shown (the global one, and a
checkout's own), set from an existing PGP key or a new patatt ed25519 key, or removed;
all in the global git configuration."""

from gitutil import git, git_config, make_checkout

from b4_maintainer.adapters.signing import (
    gpg_secret_keys,
    new_patatt_key,
    signing_status,
    stop_signing,
    use_pgp_key,
)

COLONS = """sec:u:255:22:AAAABBBBCCCCDDDD:1700000000:::u:::scESC:::+:::ed25519:::0:
fpr:::::::::0123456789ABCDEF0123456789ABCDEFAAAABBBB:
uid:u::::1700000000::HASH::Example Maintainer <maintainer@example.org>::::::::::0:
ssb:u:255:18:EEEEFFFF00001111:1700000000::::::e:::+:::cv25519::
fpr:::::::::FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEEEEFFFF:
"""


def test_secret_keys_are_read_from_gpg(monkeypatch):
    keys = gpg_secret_keys(lambda: COLONS)
    assert keys == [
        ('0123456789ABCDEF0123456789ABCDEFAAAABBBB', 'Example Maintainer <maintainer@example.org>')
    ]


def test_a_pgp_key_is_set_for_every_project_and_removed(tmp_path, home):
    checkout = make_checkout(tmp_path / 'demo')
    assert signing_status([('demo', checkout)]) == ['No signing key: messages go unsigned']
    use_pgp_key('0123456789ABCDEF0123456789ABCDEFAAAABBBB')
    assert git('config', '--global', 'patatt.signingkey', cwd=tmp_path) == (
        'openpgp:0123456789ABCDEF0123456789ABCDEFAAAABBBB'
    )
    status = signing_status([('demo', checkout)])
    assert 'openpgp:0123456789ABCDEF' in status[0] and 'every project' in status[0]
    git('config', 'patatt.signingkey', 'ed25519:own', cwd=checkout)
    assert any(
        'demo' in line and 'ed25519:own' in line for line in signing_status([('demo', checkout)])
    )
    stop_signing()
    assert git_config(tmp_path, 'patatt.signingkey') is None


def test_a_new_patatt_key_is_made_and_configured(tmp_path, home):
    (home / '.gitconfig').write_text('[user]\n\tname = M\n\temail = m@example.org\n')
    name = new_patatt_key('testkey')
    assert name == 'testkey'
    assert git('config', '--global', 'patatt.signingkey', cwd=tmp_path) == 'ed25519:testkey'
    assert git('config', '--global', 'patatt.selector', cwd=tmp_path) == 'testkey'
    assert list(home.rglob('patatt/private/testkey.key'))  # In patatt's data directory.
