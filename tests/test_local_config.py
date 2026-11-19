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


def test_a_setting_is_written_to_this_machines_file_keeping_the_rest(tmp_path):
    from b4_maintainer.projectsetup import set_local_setting

    local = local_config_path(tmp_path)
    local.parent.mkdir(parents=True)
    local.write_text('# This machine.\n\n[sessions]\nmax_builds = 3  # Big box.\n')
    set_local_setting(tmp_path, 'sessions', 'default_model', 'alpha/alpha-large-2')
    text = local.read_text()
    assert '# This machine.' in text and 'max_builds = 3  # Big box.' in text
    assert 'default_model = "alpha/alpha-large-2"' in text
    set_local_setting(tmp_path, 'sessions', 'default_model', 'deep')  # Replaced in place.
    assert local.read_text().count('default_model') == 1 and '"deep"' in local.read_text()
    set_local_setting(tmp_path, 'sessions', 'default_model', '')  # Removed.
    assert 'default_model' not in local.read_text() and 'max_builds = 3' in local.read_text()


def test_a_setting_makes_its_table_and_file_when_missing(tmp_path):
    from b4_maintainer.projectsetup import set_local_setting

    set_local_setting(tmp_path, 'sessions', 'default_model', 'beta/beta-pro')
    assert (
        local_config_path(tmp_path).read_text() == '[sessions]\ndefault_model = "beta/beta-pro"\n'
    )
    local = local_config_path(tmp_path)
    local.write_text('[layout]\nquote_lines = 4\n')
    set_local_setting(tmp_path, 'sessions', 'default_model', 'beta/beta-pro')
    assert local.read_text() == (
        '[layout]\nquote_lines = 4\n\n[sessions]\ndefault_model = "beta/beta-pro"\n'
    )


def test_the_default_model_reaches_the_workspace(tmp_path):
    (tmp_path / 'workspace.toml').write_text(SHARED)
    local = local_config_path(tmp_path)
    local.parent.mkdir(parents=True)
    local.write_text('[sessions]\ndefault_model = "deep"\n')
    ws = Workspace.from_config(read_config(tmp_path))
    assert ws.default_model == 'deep' and ws.session_limits.max_builds == 2
