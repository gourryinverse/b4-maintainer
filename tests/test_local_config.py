"""Contract: var/workspace.toml (per machine, never synced) overrides the synced
configuration: tables merge key by key, [[projects]]/[[profiles]]/[[models]] entries by id;
anything else it sets replaces the shared value."""

import pytest

from b4_maintainer.projectsetup import local_config_path, read_config
from b4_maintainer.workspace import Workspace, merge_config

SHARED = """
me = ["m@example.org"]

[[projects]]
id = "linux"
path = "projects/linux"
lists = ["linux-mm@kvack.org"]
refs = [{ label = "m", name = "mainline/master", authority = "mainline" }]

[[models]]
id = "deep"
model = "provider/deep"
thinking = "high"

[sessions]
max_live = 4
max_builds = 2
sandbox_nice = 10
"""


def workspace_root(tmp_path, local=None):
    (tmp_path / 'state').mkdir()
    (tmp_path / 'state' / 'workspace.toml').write_text(SHARED)
    if local is not None:
        (tmp_path / 'var').mkdir()
        (tmp_path / 'var' / 'workspace.toml').write_text(local)
    return tmp_path


def test_without_local_overrides_the_synced_file_is_the_configuration(tmp_path):
    root = workspace_root(tmp_path)
    assert local_config_path(root) == root / 'var' / 'workspace.toml'
    ws = Workspace.from_config(read_config(root))
    assert ws.session_limits.max_builds == 2 and [p.id for p in ws.projects] == ['linux']


def test_local_tables_override_key_by_key(tmp_path):
    root = workspace_root(tmp_path, '[sessions]\nmax_builds = 6\n')
    ws = Workspace.from_config(read_config(root))
    assert ws.session_limits.max_builds == 6  # This machine's.
    assert ws.session_limits.max_live == 4 and ws.sandbox_nice == 10  # The rest kept.
    assert ws.warnings == ()


def test_entries_merge_by_id_and_new_ones_are_added(tmp_path):
    local = """
[[projects]]
id = "linux"
path = "/srv/linux"

[[models]]
id = "deep"
enabled = false

[[models]]
id = "local"
model = "ollama/small"
"""
    ws = Workspace.from_config(read_config(workspace_root(tmp_path, local)))
    linux = ws.project('linux')
    assert linux.path == '/srv/linux'  # Overridden here.
    assert linux.lists == ('linux-mm.kvack.org',) and linux.refs  # Kept from the shared file.
    assert [(m['id'], m.get('enabled', True)) for m in ws.models] == [
        ('deep', False),
        ('local', True),
    ]
    assert ws.models[0]['model'] == 'provider/deep'  # Kept.


def test_other_values_replace_the_shared_ones():
    merged = merge_config(
        {'me': ['a'], 'projects': [{'id': 'x', 'refs': [{'label': 'm'}]}]},
        {'me': ['b'], 'projects': [{'id': 'x', 'refs': [{'label': 'n'}]}]},
    )
    assert merged == {'me': ['b'], 'projects': [{'id': 'x', 'refs': [{'label': 'n'}]}]}


def test_a_broken_local_file_names_itself(tmp_path):
    root = workspace_root(tmp_path, '[sessions\n')
    with pytest.raises(ValueError, match='var/workspace.toml'):
        read_config(root)


def test_the_live_workspace_reads_the_local_overrides(tmp_path):
    from b4_maintainer.adapters.live import LiveSource

    root = workspace_root(tmp_path, '[sessions]\nmax_builds = 5\n')
    assert LiveSource(root).workspace().session_limits.max_builds == 5
