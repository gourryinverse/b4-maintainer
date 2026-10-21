import socket

import pytest
from gitutil import make_checkout


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fixture-backed tests must never reach a network service."""

    def refuse(*args, **kwargs):
        raise AssertionError('network access attempted during a fixture test')

    monkeypatch.setattr(socket.socket, 'connect', refuse)
    monkeypatch.setattr(socket, 'create_connection', refuse)


@pytest.fixture
def demo_root(tmp_path):
    from b4_maintainer.fixtures import build_demo_workspace

    return build_demo_workspace(tmp_path / 'demo')


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolate global git and b4 state; anything written here is a leak."""
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_DATA_HOME', str(home / 'xdg-data'))
    monkeypatch.setenv('XDG_CACHE_HOME', str(home / 'xdg-cache'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home / 'xdg-config'))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    return home


@pytest.fixture
def ws(tmp_path, home):
    root = tmp_path / 'ws'
    make_checkout(root / 'projects' / 'demo')
    return root
