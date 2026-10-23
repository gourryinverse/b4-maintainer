"""Contract: private context reaches only backends allowed it; limits say why to wait."""

from b4_maintainer.sessions.backends import backend_entries
from b4_maintainer.sessions.policy import ContextItem, SessionLimits, disclosure, launch_check
from b4_maintainer.workspace import Workspace

ITEMS = (
    ContextItem('mbox', 'Series mail', private=False),
    ContextItem('sashiko', 'Sashiko findings', private=False),
    ContextItem('drafts', 'Your unsent drafts', private=True),
    ContextItem('notes', 'Synced client state', private=True),
)


def entry(eid, config=()):
    return next(e for e in backend_entries(list(config), which=lambda c: c) if e.id == eid)


def test_public_context_always_goes_and_private_only_when_ticked_and_allowed():
    included, refused = disclosure(ITEMS, entry('claude'), ticked=set())
    assert [i.id for i in included] == ['mbox', 'sashiko'] and refused == []

    included, refused = disclosure(ITEMS, entry('claude'), ticked={'drafts'})
    assert [i.id for i in included] == ['mbox', 'sashiko']
    assert refused and 'Your unsent drafts' in refused[0] and 'claude' in refused[0]

    local = entry('local', [{'id': 'local', 'adapter': 'claude', 'private_ok': True}])
    included, refused = disclosure(ITEMS, local, ticked={'drafts'})
    assert [i.id for i in included] == ['mbox', 'sashiko', 'drafts'] and refused == []


def test_limits_block_live_sessions_and_warn_on_worktrees():
    limits = SessionLimits(max_live=2, max_builds=1, worktree_warn=3, worktree_warn_gb=10)
    assert launch_check(limits, live=1, worktrees=0, worktree_bytes=0, mode='worktree') == ([], [])
    blockers, _ = launch_check(limits, live=2, worktrees=0, worktree_bytes=0, mode='lightweight')
    assert blockers == ['2 sessions are live (max_live = 2); stop one first']
    _, warnings = launch_check(
        limits, live=0, worktrees=3, worktree_bytes=11 * 2**30, mode='worktree'
    )
    assert warnings == [
        '3 worktrees exist (worktree_warn = 3)',
        'worktrees use 11.0 GB (worktree_warn_gb = 10)',
    ]
    _, warnings = launch_check(limits, live=0, worktrees=9, worktree_bytes=0, mode='lightweight')
    assert warnings == []  # A lightweight session adds no worktree.


def test_workspace_config_carries_limits_and_backends():
    config = {
        'sessions': {'max_live': 6, 'worktree_warn_gb': 50},
        'backends': [{'id': 'claude-cyber', 'adapter': 'claude', 'command': ['claude-cyber']}],
    }
    ws = Workspace.from_config(config)
    assert ws.session_limits == SessionLimits(max_live=6, worktree_warn_gb=50)
    assert ws.backends[0]['id'] == 'claude-cyber'
    default = Workspace.from_config({})
    assert default.session_limits == SessionLimits() and default.backends == ()
