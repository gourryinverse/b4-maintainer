"""Contract: a session's pi command line and files follow its record, profile, and model."""

from sessiondata import make_record

from b4_maintainer.sessions.pi import EXTENSION, LaunchSpec, Site, pi_command, prepare_session
from b4_maintainer.sessions.profiles import Request
from b4_maintainer.sessions.records import Worktree
from b4_maintainer.sessions.store import SessionStore

REQUEST = Request(
    prompt='-Help me review this series.',  # A leading dash must not read as an option.
    system_prompt='You assist a maintainer.',
    files=(),
    mode='lightweight',
    notes=(),
)


def site(tmp_path, **changes):
    base = dict(
        root=tmp_path,
        hook='/opt/b4m/bin/b4-maintainer',
        pi='pi',
        args=('--site-x',),
        checkouts={'linux': '/w/linux'},
    )
    return Site(**{**base, **changes})


def command(tmp_path, record=None, spec=None, first=True, **site_changes):
    store = SessionStore(tmp_path / 'var' / 'sessions')
    record = record or make_record('s1')
    spec = spec or LaunchSpec(REQUEST)
    return store, pi_command(store, record, spec, site(tmp_path, **site_changes), first=first)


def after(argv, flag):
    return argv[argv.index(flag) + 1]


def test_a_read_only_session_runs_pi_with_its_own_session_and_nothing_discovered(tmp_path):
    store, cmd = command(tmp_path)
    argv = cmd.argv
    directory = store.directory('s1')
    assert argv[0] == 'pi'
    assert after(argv, '--session-id') == 's1'
    assert after(argv, '--session-dir') == str(directory / 'pi')
    assert after(argv, '--name') == 'linux: mm: fix foo leak v3'
    assert after(argv, '-e') == str(EXTENSION) and EXTENSION.is_file()
    assert after(argv, '--append-system-prompt') == str(directory / 'system.md')
    assert {'--no-skills', '--no-context-files', '--no-mcp', '--no-approve'} <= set(argv)
    # Every session writes its artifacts under out/; the extension keeps writes there.
    assert after(argv, '--tools') == 'read,grep,find,ls,bash,edit,write'
    assert '--model' not in argv and '--thinking' not in argv  # pi's default model.
    assert '--site-x' in argv
    assert argv[-2:] == ['--', '-Help me review this series.']
    # The agent works in work/ (context/ and out/), apart from the session's internals.
    assert cmd.cwd == str(directory / 'work')


def test_the_model_and_skills_are_passed(tmp_path):
    record = make_record('s1', permissions='execute', model='alpha/alpha-large', thinking='high')
    skill = tmp_path / 'skills' / 'vng'
    spec = LaunchSpec(REQUEST, skills=(skill,), args=('--preset-y',), env={'K': 'v'})
    _store, cmd = command(tmp_path, record, spec)
    argv = cmd.argv
    assert after(argv, '--tools') == 'read,grep,find,ls,bash,edit,write'
    assert (after(argv, '--model'), after(argv, '--thinking')) == ('alpha/alpha-large', 'high')
    assert after(argv, '--skill') == str(skill)
    assert argv.index('--site-x') < argv.index('--preset-y') < argv.index('--')
    assert cmd.env['K'] == 'v'


def test_the_extension_learns_the_session_through_its_environment(tmp_path):
    wt = Worktree(str(tmp_path / 'wt'), 'b4m/session/s1', 'v6.18', 'abc')
    record = make_record('s1', mode='worktree', worktree=wt, permissions='execute')
    skill = tmp_path / 'skills' / 'vng'
    store, cmd = command(tmp_path, record, LaunchSpec(REQUEST, skills=(skill,)))
    directory = store.directory('s1')
    assert cmd.cwd == wt.path
    assert cmd.env == {
        'B4M_SESSION': 's1',
        'B4M_ROOT': str(tmp_path),
        'B4M_HOOK': '/opt/b4m/bin/b4-maintainer',
        'B4M_PERMISSIONS': 'execute',
        'B4M_EVENTS': str(directory / 'events.jsonl'),
        'B4M_OUT': str(directory / 'work' / 'out'),
        'B4M_WORKTREE': wt.path,
        # What pi's read and search tools may open, one per line.
        'B4M_READABLE': '\n'.join([str(directory / 'work'), wt.path, '/w/linux', str(skill)]),
        # git must not find a repository around the session (e.g. the workspace's own).
        'GIT_CEILING_DIRECTORIES': str(directory),
        'B4M_SANDBOX': '',  # This machine's sandbox was not used for the launch.
        'B4M_NICE': '10',  # Sandboxed commands run at low priority.
        'B4M_IONICE': 'low',
    }


def test_resuming_reopens_the_session_without_resending_the_task(tmp_path):
    _store, cmd = command(tmp_path, first=False)
    assert '--' not in cmd.argv and after(cmd.argv, '--session-id') == 's1'


def test_session_files_hold_the_system_prompt_task_and_thread(tmp_path):
    store = SessionStore(tmp_path / 'var' / 'sessions')
    mbox = tmp_path / 'thread.mbx'
    mbox.write_text('From synthetic@example.org\n')
    record = make_record('s1')
    prepare_session(store, record, REQUEST, source=str(mbox))
    directory = store.directory('s1')
    system = (directory / 'system.md').read_text()
    assert system.startswith('You assist a maintainer.')
    work = directory / 'work'
    assert str(work / 'out') in system and 'read-only' in system
    assert 'read, grep, find, and ls tools' in system  # Not shell pipelines.
    assert (work / 'context' / 'task.md').read_text() == REQUEST.prompt + '\n'
    assert (work / 'context' / 'thread.mbx').read_text() == mbox.read_text()
    assert (work / 'out').is_dir()


def test_a_session_without_mail_gets_no_thread(tmp_path):
    store = SessionStore(tmp_path / 'var' / 'sessions')
    prepare_session(store, make_record('s1'), REQUEST, source='')
    context = store.directory('s1') / 'work' / 'context'
    assert not (context / 'thread.mbx').exists()
    assert (context / 'task.md').exists()


def test_a_maildir_thread_becomes_an_mbox(tmp_path):
    import mailbox

    maildir = mailbox.Maildir(tmp_path / 'thread', create=True)
    for n in (1, 2):
        maildir.add(f'From: dev{n}@example.org\nSubject: [PATCH {n}/2] synthetic\n\nBody {n}\n')
    store = SessionStore(tmp_path / 'var' / 'sessions')
    prepare_session(store, make_record('s1'), REQUEST, source=str(tmp_path / 'thread'))
    mbox = mailbox.mbox(store.directory('s1') / 'work' / 'context' / 'thread.mbx')
    assert sorted(m['Subject'] for m in mbox) == [
        '[PATCH 1/2] synthetic',
        '[PATCH 2/2] synthetic',
    ]


def test_sandboxed_sessions_are_told_so(tmp_path):
    store = SessionStore(tmp_path / 'var' / 'sessions')
    record = make_record('s1')
    prepare_session(store, record, REQUEST, source='', sandboxed=True)
    system = (store.directory('s1') / 'system.md').read_text()
    assert 'sandbox' in system and 'without network' in system
    _store, cmd = command(tmp_path, record, LaunchSpec(REQUEST, sandboxed=True))
    assert cmd.env['B4M_SANDBOX'] == '1'


def test_worktree_sessions_are_told_they_edit_build_and_commit_in_their_tree(tmp_path):
    store = SessionStore(tmp_path / 'var' / 'sessions')
    tree = Worktree('/w/var/worktrees/s1', 'b4m/session/s1', 'b', 'a')
    record = make_record('s1', mode='worktree', permissions='execute', worktree=tree)
    prepare_session(store, record, REQUEST, source='', sandboxed=True)
    system = (store.directory('s1') / 'system.md').read_text()
    assert 'read-only except the output directory and the worktree' in system
    assert 'Edit, build, and commit in the worktree /w/var/worktrees/s1' in system
    assert 'on base b, its last patch a' in system  # The skill builds branches from these.
    assert 'agent identity' in system and 'Signed-off-by' in system
    lightweight = make_record('s2')
    prepare_session(store, lightweight, REQUEST, source='', sandboxed=True)
    system = (store.directory('s2') / 'system.md').read_text()
    assert 'read-only except the output directory.' in system and 'Edit, build' not in system


def test_rebase_sessions_are_told_to_keep_the_commits_trailers(tmp_path):
    store = SessionStore(tmp_path / 'var' / 'sessions')
    tree = Worktree('/w/var/worktrees/r1', 'b4m/session/r1', 'f' * 40, 't' * 40)
    record = make_record(
        'r1', scope='rebase', mode='worktree', permissions='execute', worktree=tree
    )
    prepare_session(store, record, REQUEST, source='', sandboxed=True, items=[])
    system = (store.directory('r1') / 'system.md').read_text()
    assert 'Rebase in the worktree /w/var/worktrees/r1' in system
    assert 'keep each commit' in system and 'never add Signed-off-by' not in system
