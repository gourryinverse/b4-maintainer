"""Contract: state moves between machines only by hand. `state export FILE` writes this
machine's state (configuration, workbenches, client state, tracked series, flags, how
each checkout was cloned); private data stays out unless asked for (--private): the
machine-local var/workspace.toml, projects only it defines, the remotes its base_refs
and track_branches name, and workbenches or checkouts on private bases or tracked
branches. `state import FILE` adds what is missing and keeps what is here."""

import datetime
import json
import tarfile

from b4.review import messages
from gitutil import git, make_checkout
from test_b4state import add_tracked

from b4_maintainer.adapters.b4state import all_flags, b4_repo, tracked_series
from b4_maintainer.adapters.live import LiveSource
from b4_maintainer.cli import project_main, sync_main
from b4_maintainer.projectsetup import setup_workspace

NOW = datetime.datetime(2026, 10, 28, 12, 0, tzinfo=datetime.timezone.utc)
PRIVATE = """\
[[projects]]
id = "demo"
base_refs = ["origin/v*-x"]
track_branches = ["origin/scratch/me/*"]
"""
PUBLIC_BENCH = 'label = "Public"\n\n[[entry]]\nproject = "demo"\nbase = "mainline/master"\n'
PRIVATE_BENCH = 'label = "Private"\n\n[[entry]]\nproject = "demo"\nbase = "origin/v1-x"\n'


def machine_a(ws):
    (ws / 'state').mkdir()
    assert project_main(['add', '-C', str(ws), 'demo']) == 0
    checkout = ws / 'projects' / 'demo'
    git('remote', 'add', 'origin', 'https://internal.example.invalid/demo.git', cwd=checkout)
    git('remote', 'add', 'mainline', 'https://public.example.org/demo.git', cwd=checkout)
    (ws / 'var').mkdir(exist_ok=True)
    (ws / 'var' / 'workspace.toml').write_text(PRIVATE)
    benches = ws / 'state' / 'workbenches'
    benches.mkdir()
    (benches / 'public.toml').write_text(PUBLIC_BENCH)
    (benches / 'private.toml').write_text(PRIVATE_BENCH)
    add_tracked(checkout, 'cid-1', 'cover@example.org', ['p1@example.org'])
    with b4_repo(checkout):
        conn = messages.get_db()
        messages.set_flag(conn, 'cover@example.org', 'Seen', '2026-10-20T00:00:00+00:00')
        conn.close()
    (ws / 'state' / 'client').mkdir()
    watched = [{'identity': 'thread:d@x', 'msgid': 'd@x', 'subject': 'Path?'}]
    (ws / 'state' / 'client' / 'watched.json').write_text(json.dumps(watched))
    return LiveSource(ws, now=lambda: NOW)


def members(path):
    with tarfile.open(path) as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}


def test_an_export_leaves_private_state_out_unless_asked(ws, tmp_path):
    source = machine_a(ws)
    public = tmp_path / 'state.tar.gz'
    report = source.export_state(public)
    assert 'private' in report  # It says what it left out.
    files = members(public)
    assert 'workspace.toml' in files and 'client/watched.json' in files
    assert 'workbenches/public.toml' in files and 'workbenches/private.toml' not in files
    assert 'private/workspace.toml' not in files
    assert any(name.startswith('projects/demo/series/') for name in files)
    assert 'b4/flags.tsv' in files
    checkout = json.loads(files['projects/demo/checkout.json'])
    assert [r['name'] for r in checkout['remotes']] == ['mainline']  # Not the internal one.
    assert b'internal.example.invalid' not in b''.join(files.values())

    everything = tmp_path / 'all.tar.gz'
    source.export_state(everything, private=True)
    files = members(everything)
    assert files['private/workspace.toml'].decode() == PRIVATE
    assert 'workbenches/private.toml' in files
    remotes = json.loads(files['projects/demo/checkout.json'])['remotes']
    assert {r['name'] for r in remotes} == {'origin', 'mainline'}


def test_an_import_brings_state_to_another_machine(ws, tmp_path):
    bundle = tmp_path / 'all.tar.gz'
    machine_a(ws).export_state(bundle, private=True)
    other = tmp_path / 'other'
    (other / 'state').mkdir(parents=True)
    report = LiveSource(other, now=lambda: NOW).import_state(bundle)
    assert 'workspace.toml' in report
    assert (other / 'state' / 'workspace.toml').exists()
    assert (other / 'var' / 'workspace.toml').read_text() == PRIVATE
    assert (other / 'state' / 'workbenches' / 'private.toml').exists()
    make_checkout(other / 'projects' / 'demo')
    setup_workspace(other)  # Enrolls the checkout and takes the series and flags.
    there = other / 'projects' / 'demo'
    assert [e.change_id for e in tracked_series(there, 'demo')] == ['cid-1']
    assert [f['msgid'] for f in all_flags(there)] == ['cover@example.org']
    assert LiveSource(other).watched.msgids() == {'d@x'}


def test_an_import_keeps_what_is_here(ws, tmp_path):
    bundle = tmp_path / 'state.tar.gz'
    source = machine_a(ws)
    source.export_state(bundle)
    (ws / 'state' / 'workbenches' / 'public.toml').write_text(
        PUBLIC_BENCH.replace('Public', 'Mine')
    )
    report = source.import_state(bundle)
    assert 'kept' in report and 'public.toml' in report
    assert 'Mine' in (ws / 'state' / 'workbenches' / 'public.toml').read_text()


def test_state_no_longer_syncs_on_its_own(ws, tmp_path, capsys):
    source = machine_a(ws)
    assert not hasattr(source, 'sync')
    calls = []
    source._refresh = lambda progress: calls.append('refresh') or 'Refreshed'
    assert source.refresh() == 'Refreshed' and calls == ['refresh']
    assert sync_main([]) == 1
    assert 'state export' in capsys.readouterr().err
