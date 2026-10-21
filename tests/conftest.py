import socket

import pytest


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
