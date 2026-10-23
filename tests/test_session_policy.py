"""Contract: launch limits say why a launch must wait."""

from b4_maintainer.sessions.policy import SessionLimits, launch_check
from b4_maintainer.workspace import Workspace


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
