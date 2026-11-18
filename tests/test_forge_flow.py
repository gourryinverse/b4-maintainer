"""Contract: a forge's sessions leave their results in out/forge/ and the series on
forge/<id>/next in the space's repository; collecting turns them into mail on the local
list: plan rounds, series rounds (through the forge's b4 prep branch, kept as snapshots),
replies under the messages they answer, findings, and test reports. Rounds compare by
range-diff, fold into a prep branch in the project, and upstream postings are harvested
as the next cycle's input."""

import json
import pathlib

import pytest
from forgedata import posted_rounds
from gitutil import git

from b4_maintainer.forge import AGENT, ForgeError, ForgeService, ForgeStore, Round
from b4_maintainer.sessions.records import SessionRecord
from b4_maintainer.sessions.trees import clone_project
from b4_maintainer.workbench import WorkbenchStore

ME = ('Example Maintainer', 'maintainer@example.org')


@pytest.fixture
def world(tmp_path, home):
    (home / '.gitconfig').write_text(
        '[user]\n\tname = Example Maintainer\n\temail = maintainer@example.org\n'
    )
    project = tmp_path / 'projects' / 'linux'
    project.mkdir(parents=True)
    git('init', '-q', '-b', 'main', cwd=project)
    (project / 'README').write_text('base\n')
    git('add', 'README', cwd=project)
    git('commit', '-q', '-m', 'start', cwd=project)
    state = tmp_path / 'state'
    service = ForgeService(
        ForgeStore(state / 'forges'), WorkbenchStore(state / 'workbenches'), tmp_path / 'var'
    )
    return project, service


def make_space(service, forge, project, tmp_path):
    """The space's repository as a first worktree launch makes it, and one session's
    checkout of it."""
    repo = service.space_repo(forge)
    clone_project(project, repo, bare=True)
    git('update-ref', f'refs/heads/b4m/bench/{forge.workbench}', 'main', cwd=repo)
    checkout = tmp_path / 'checkout'
    git('worktree', 'add', '-q', '-b', 'b4m/session/s1', str(checkout), 'main', cwd=repo)
    return repo, checkout


def commit_series(checkout, forge, texts):
    """The agent's series: one commit per text, from the bench, left on forge/<id>/next."""
    git('reset', '-q', '--hard', 'main', cwd=checkout)
    for n, text in enumerate(texts, 1):
        (checkout / f'f{n}').write_text(text)
        git('add', f'f{n}', cwd=checkout)
        git('commit', '-q', '-m', f'demo: add f{n}\n\n{text}', cwd=checkout)
    git('branch', '-f', forge.next_branch, 'HEAD', cwd=checkout)


def session(service, forge, sid, profile, files):
    """A finished session of the forge's workbench with *files* in its out/forge/."""
    home = service.sessions / sid
    record = SessionRecord(
        sid, f'2026-11-17T10:{len(list(service.sessions.glob("*"))) if service.sessions.is_dir() else 0:02d}:00+00:00',
        'workbench', forge.project, '', 0, forge.label, profile, 1, '', {}, 'worktree',
        'pi', 'default', (), sid, '', '', 'execute', workbench=forge.workbench,
    )  # fmt: skip
    record.save(home / 'session.json')
    out = home / 'work' / 'out' / 'forge'
    for name, text in files.items():
        (out / name).parent.mkdir(parents=True, exist_ok=True)
        (out / name).write_text(text)
    return home


COVER = 'demo: two files\n\nAdd two files.\n'


def test_the_implement_session_posts_the_first_round(world, tmp_path):
    project, service = world
    forge = service.create('Demo', 'linux', 'main', 'Add two files.\n')
    repo, checkout = make_space(service, forge, project, tmp_path)
    commit_series(checkout, forge, ['one\n', 'two\n'])
    session(service, forge, 's1', 'forge-implement', {'cover.md': COVER})
    said = service.collect(forge)
    assert said == [f'the series on {forge.next_branch}, as v1 r1']
    (first,) = service.rounds(forge)
    series, thread = service.thread(forge, first)
    assert series.subject == 'demo: two files'
    assert [p.message.subject for p in thread.patches] == [
        '[PATCH 1/2] demo: add f1',
        '[PATCH 2/2] demo: add f2',
    ]
    info = service.round_info(forge, first)
    assert git('rev-parse', f'refs/forge/{forge.id}/v1/r1', cwd=repo) == info['tip']
    assert git('rev-parse', f'forge/{forge.id}/v1-r1', cwd=repo) == info['tip']
    assert service.collect(forge) == []  # Nothing new.
    assert git('status', '--porcelain', cwd=project) == ''  # The project is untouched.


def test_a_rework_round_answers_the_review_and_compares(world, tmp_path):
    project, service = world
    forge = service.create('Demo', 'linux', 'main', '')
    repo, checkout = make_space(service, forge, project, tmp_path)
    commit_series(checkout, forge, ['one\n', 'two\n'])
    session(service, forge, 's1', 'forge-implement', {'cover.md': COVER})
    service.collect(forge)
    first = Round(1, 1)
    _series, thread = service.thread(forge, first)
    patch1 = thread.patches[0].message.msgid
    asked = service.post_reply(forge, first, patch1, ME, '> one\n\nWhy one?\n')
    commit_series(checkout, forge, ['uno\n', 'two\n'])
    reply = f'In-Reply-To: <{asked}>\n\n> Why one?\n\nIt is uno now.\n'
    cover = COVER + '\nChanges in r2:\n- f1 says uno\n'
    session(service, forge, 's2', 'forge-rework', {'cover.md': cover, 'replies/1.txt': reply})
    said = service.collect(forge)
    assert said == ['s2: a reply on v1 r1', f'the series on {forge.next_branch}, as v1 r2']
    _series, thread = service.thread(forge, first)
    replies = thread.patches[0].replies
    assert [(r.message.author_email, r.depth) for r in replies] == [
        (ME[1], 0),
        (AGENT[1], 1),
    ]
    assert 'It is uno now.' in replies[1].message.body
    _series, second = service.thread(forge, Round(1, 2))
    assert 'Changes in r2' in second.cover.message.body
    diff = service.range_diff(forge, Round(1, 2))
    assert 'demo: add f1' in diff and '+uno' in diff
    assert git('rev-parse', '--verify', f'forge/{forge.id}/v1-r1', cwd=repo)  # Kept.


def test_aggressive_rewrites_one_branch_and_keeps_snapshots(world, tmp_path):
    project, service = world
    forge = service.create('Demo', 'linux', 'main', '', mode='aggressive')
    repo, checkout = make_space(service, forge, project, tmp_path)
    for n, texts in enumerate((['one\n'], ['uno\n']), 1):
        commit_series(checkout, forge, texts)
        session(service, forge, f's{n}', 'forge-implement', {'cover.md': COVER})
        service.collect(forge)
    tips = [service.round_info(forge, Round(1, n))['tip'] for n in (1, 2)]
    assert tips[0] != tips[1]
    assert git('rev-parse', f'forge/{forge.id}/work', cwd=repo) == tips[1]
    assert git('rev-parse', f'refs/forge/{forge.id}/v1/r1', cwd=repo) == tips[0]
    assert git('branch', '--list', f'forge/{forge.id}/v1-*', cwd=repo) == ''


def test_reviews_and_tests_land_on_the_newest_round(world, tmp_path):
    project, service = world
    forge = service.create('Demo', 'linux', 'main', '')
    _repo, checkout = make_space(service, forge, project, tmp_path)
    commit_series(checkout, forge, ['one\n', 'two\n'])
    session(service, forge, 's1', 'forge-implement', {'cover.md': COVER})
    service.collect(forge)
    findings = {
        'patches': [
            {
                'patch': 2,
                'findings': [
                    {'severity': 'high', 'problem': 'f2 is wrong', 'file': 'f2', 'line': 1},
                    {'severity': 'low', 'problem': 'old', 'preexisting': True},
                ],
            }
        ]
    }
    session(service, forge, 's2', 'forge-review-prompts', {'review.json': json.dumps(findings)})
    session(service, forge, 's3', 'forge-test', {'test.md': 'Built and booted: passed.\n'})
    assert service.collect(forge) == [
        's2: findings on v1 r1',
        's3: a test report on v1 r1',
    ]
    _series, thread = service.thread(forge, Round(1, 1))
    review = service.review(forge, Round(1, 1), thread)
    assert (review.high, review.low, review.findings) == (1, 0, 1)
    (part,) = review.patches
    assert part.msgid == thread.patches[1].message.msgid
    assert part.findings[0].locations == (('f2', 1),)
    (report,) = thread.cover.replies
    assert report.message.author_email == 'test@forge.local'
    assert 'passed' in report.message.body
    # The next test run also replies to each patch with its result.
    patch = thread.patches[0].message.msgid
    reply = f'In-Reply-To: <{patch}>\n\nBuilt; the f1 test passed.\n'
    files = {'test.md': 'Both patches build and pass.\n', 'replies/1.txt': reply}
    session(service, forge, 's4', 'forge-test', files)
    assert service.collect(forge) == ['s4: a reply on v1 r1', 's4: a test report on v1 r1']
    _series, thread = service.thread(forge, Round(1, 1))
    (result,) = thread.patches[0].replies
    assert result.message.author_email == 'test@forge.local'
    assert 'the f1 test passed' in result.message.body


def test_a_plan_is_posted_reviewed_and_accepted(world, tmp_path):
    _project, service = world
    forge = service.create('Demo', 'linux', 'main', 'Add two files.\n')
    session(service, forge, 's1', 'forge-plan', {'design.md': 'Two patches: f1, then f2.\n'})
    assert service.collect(forge) == ['s1: the design, as plan r1; s i implements it']
    plan = Round(0, 1)
    assert service.rounds(forge) == [plan] and plan.label == 'plan r1'
    _series, thread = service.thread(forge, plan)
    assert thread.discussion and 'Two patches' in thread.cover.message.body
    assert thread.cover.message.subject == '[PLAN r1] Demo'
    service.post_reply(forge, plan, thread.cover.message.msgid, ME, 'f2 first?\n')
    assert service.accepted_plan(forge) is None
    service.accept_plan(forge, plan, ME)
    assert service.accepted_plan(forge) == plan
    _series, thread = service.thread(forge, plan)
    assert any('Acked-by: Example Maintainer' in r.message.body for r in thread.cover.replies)
    assert service.design(forge) == 'Two patches: f1, then f2.\n'
    assert service.next_round(forge) == Round(1, 1)  # Plans are not series rounds.
    folder = service.write_context(forge)
    assert (folder / 'design.md').read_text() == 'Two patches: f1, then f2.\n'
    assert (folder / 'accepted.md').read_text() == 'Accepted: plan r1\n'
    assert (folder / 'prompt.md').read_text() == 'Add two files.\n'
    assert forge.next_branch in (folder / 'README.md').read_text()
    assert (folder / 'rounds' / 'v0-r1.mbx').is_file()


def test_a_reply_to_nothing_here_is_left_out(world, tmp_path):
    _project, service = world
    forge = service.create('Demo', 'linux', 'main', '')
    session(service, forge, 's1', 'forge-rework', {'replies/1.txt': 'In-Reply-To: <x@y>\n\nHi\n'})
    assert service.collect(forge) == ['s1: 1.txt answers no message here; left out']


def test_other_workbenches_sessions_are_not_collected(world, tmp_path):
    _project, service = world
    forge = service.create('Demo', 'linux', 'main', '')
    other = service.create('Other', 'linux', 'main', '')
    session(service, other, 's1', 'forge-plan', {'design.md': 'Other plan.\n'})
    assert service.collect(forge) == []
    assert service.rounds(forge) == []


def test_fold_makes_or_rerolls_the_projects_prep_branch(world, tmp_path):
    project, service = world
    forge = service.create('Demo', 'linux', 'main', '')
    _repo, checkout = make_space(service, forge, project, tmp_path)
    head = git('rev-parse', 'HEAD', cwd=project)
    commit_series(checkout, forge, ['one\n', 'two\n'])
    session(service, forge, 's1', 'forge-implement', {'cover.md': COVER})
    service.collect(forge)
    wt = tmp_path / 'var' / 'branches' / 'linux' / forge.id
    branch = service.fold(forge, Round(1, 1), project, wt)
    assert branch == f'b4/{forge.id}'
    log = git('log', '--format=%s', f'main..{branch}', cwd=project).splitlines()
    assert log[:2] == ['demo: add f2', 'demo: add f1'] and len(log) == 3  # And the cover.
    assert git('rev-parse', 'HEAD', cwd=project) == head
    tracking = git('log', '-1', '--format=%B', f'{branch}~2', cwd=project)
    change_id = next(line for line in tracking.splitlines() if 'change-id' in line)
    commit_series(checkout, forge, ['uno\n', 'two\n'])
    session(service, forge, 's2', 'forge-rework', {'cover.md': COVER})
    service.collect(forge)
    assert service.fold(forge, Round(1, 2), project, wt) == branch
    again = git('log', '-1', '--format=%B', f'{branch}~2', cwd=project)
    assert change_id in again  # The same series: the next send is its next revision.
    assert git('show', f'{branch}~1:f1', cwd=project) == 'uno'


@pytest.fixture(scope='module')
def posted(tmp_path_factory):
    return posted_rounds(tmp_path_factory)


def test_an_upstream_posting_is_harvested_as_the_next_cycles_input(world, tmp_path, posted):
    _project, service = world
    upstream = tmp_path / 'thread'
    import mailbox

    box = mailbox.Maildir(upstream, create=True)
    for path in posted[0]:
        box.add(path.read_bytes())
    forge = service.create_from_upstream(
        'Demo upstream', 'linux', 'main', upstream, 1, 'cover@example.org', 'Rework it.\n'
    )
    assert forge.origin == 'cover@example.org'
    (up,) = service.rounds(forge)
    assert up == Round(1, 0) and up.label == 'v1 upstream'
    assert service.next_round(forge) == Round(2, 1)
    _series, thread = service.thread(forge, up)
    assert len(thread.patches) == 2
    folder = service.write_context(forge)
    assert (folder / 'rounds' / 'v1-r0.mbx').is_file()
    assert 'Upstream: v1 upstream' in (folder / 'state.md').read_text()
    # Harvested again (new replies upstream), it is refreshed in place.
    service.harvest(forge, upstream, 1, 'cover@example.org')
    assert service.rounds(forge) == [up]


def test_the_timeline_says_where_the_work_started_and_where_it_is(world, tmp_path):
    project, service = world
    forge = service.create('Demo', 'linux', 'main', '')
    session(service, forge, 's0', 'forge-plan', {'design.md': 'Two patches.\n'})
    service.collect(forge)
    _repo, checkout = make_space(service, forge, project, tmp_path)
    commit_series(checkout, forge, ['one\n', 'two\n'])
    session(service, forge, 's1', 'forge-implement', {'cover.md': COVER})
    service.collect(forge)
    _series, thread = service.thread(forge, Round(1, 1))
    asked = service.post_reply(forge, Round(1, 1), thread.patches[0].message.msgid, ME, 'Why?\n')
    findings = {'patches': [{'patch': 1, 'findings': [{'severity': 'high', 'problem': 'x'}]}]}
    session(service, forge, 's2', 'forge-review-prompts', {'review.json': json.dumps(findings)})
    service.collect(forge)
    commit_series(checkout, forge, ['uno\n', 'two\n'])
    reply = f'In-Reply-To: <{asked}>\n\nFixed.\n'
    session(service, forge, 's3', 'forge-rework', {'cover.md': COVER, 'replies/1.txt': reply})
    service.collect(forge)
    text = service.summary(forge)
    lines = text.splitlines()
    assert lines[0] == 'Demo · linux on main · iterate'
    plan = next(line for line in lines if line.startswith('plan r1'))
    first = next(line for line in lines if line.startswith('v1 r1'))
    second = next(line for line in lines if line.startswith('v1 r2'))
    assert 'design' in plan
    assert '2 patches' in first and 'maintainer 1' in first and 'agent 1' in first
    assert 'findings 1 (high 1)' in first
    assert '2 patches' in second and '1 changed' in second  # Against v1 r1.
    assert 'Started: v1 r1' in text and 'Now: v1 r2' in text
    assert '1 of 2 patches changed since v1 r1' in text


def test_o_has_a_checkout_at_a_patch_of_a_round(world, tmp_path):
    project, service = world
    forge = service.create('Demo', 'linux', 'main', '')
    repo, checkout = make_space(service, forge, project, tmp_path)
    commit_series(checkout, forge, ['one\n', 'two\n'])
    session(service, forge, 's1', 'forge-implement', {'cover.md': COVER})
    service.collect(forge)
    first = service.look(forge, Round(1, 1), 1)
    assert (first / 'f1').read_text() == 'one\n' and not (first / 'f2').exists()
    assert git('rev-parse', '--abbrev-ref', 'HEAD', cwd=first) == 'HEAD'  # Detached.
    whole = service.look(forge, Round(1, 1), 2)
    assert (whole / 'f2').read_text() == 'two\n'
    assert service.look(forge, Round(1, 1), 1) == first  # Reused.
    assert first.is_relative_to(service.space(forge))
    assert git('status', '--porcelain', cwd=project) == ''
    with pytest.raises(ForgeError):
        service.look(forge, Round(1, 1), 3)


def test_older_upstream_versions_are_harvested_only_when_asked(world, tmp_path, posted):
    import mailbox

    _project, service = world
    upstream = tmp_path / 'thread'
    box = mailbox.Maildir(upstream, create=True)
    for path in posted[0]:
        box.add(path.read_bytes())
    forge = service.create_from_upstream('Demo', 'linux', 'main', upstream, 3, 'c@example.org')
    assert [r.label for r in service.rounds(forge)] == ['v3 upstream']
    service.harvest(forge, upstream, 2, 'b@example.org')
    assert [r.label for r in service.rounds(forge)] == ['v2 upstream', 'v3 upstream']
    assert service.next_round(forge) == Round(4, 1)  # Still after the newest.


def test_harvesting_again_keeps_the_forges_own_replies(world, tmp_path, posted):
    import mailbox

    _project, service = world
    upstream = tmp_path / 'thread'
    box = mailbox.Maildir(upstream, create=True)
    for path in posted[0]:
        box.add(path.read_bytes())
    forge = service.create_from_upstream('Demo', 'linux', 'main', upstream, 1, 'c@example.org')
    up = Round(1, 0)
    _series, thread = service.thread(forge, up)
    mine = service.post_reply(forge, up, thread.patches[0].message.msgid, ME, 'Agent: fix this.\n')
    service.harvest(forge, upstream, 1, 'c@example.org')  # New list replies, say.
    _series, thread = service.thread(forge, up)
    assert [r.message.msgid for r in thread.patches[0].replies] == [mine]
    assert mine.endswith('.forge.local')


def upstream_with_review(tmp_path, posted):
    """The posted v1 thread as the mail cache holds it, with a Reviewed-by on patch 1."""
    import email
    import email.policy
    import mailbox
    from email.message import EmailMessage

    upstream = tmp_path / 'thread'
    box = mailbox.Maildir(upstream, create=True)
    for path in posted[0]:
        box.add(path.read_bytes())
    patch1 = email.message_from_bytes(posted[0][1].read_bytes(), policy=email.policy.default)
    reply = EmailMessage()
    reply['From'] = 'Rev Iewer <rev@example.org>'
    reply['Subject'] = f'Re: {patch1["Subject"]}'
    reply['Date'] = patch1['Date']
    reply['Message-Id'] = '<review-1@example.org>'
    reply['In-Reply-To'] = patch1['Message-Id']
    reply['References'] = patch1['Message-Id']
    reply.set_content('Looks right.\n\nReviewed-by: Rev Iewer <rev@example.org>\n')
    box.add(reply.as_bytes())
    return upstream


def test_the_next_version_starts_from_the_posting_with_its_trailers(world, tmp_path, posted):
    project, service = world
    upstream = upstream_with_review(tmp_path, posted)
    forge = service.create_from_upstream('Demo', 'linux', 'main', upstream, 1, 'c@example.org')
    assert service.seed(forge, project) == f'{forge.next_branch} at v1 as posted, with its trailers'
    repo = service.space_repo(forge)
    log = git(
        'log', '--format=%s%n%b%x00', f'b4m/bench/{forge.workbench}..{forge.next_branch}', cwd=repo
    )
    first = next(entry for entry in log.split('\x00') if 'add f1' in entry)
    assert 'Reviewed-by: Rev Iewer <rev@example.org>' in first
    second = next(entry for entry in log.split('\x00') if 'add f2' in entry)
    assert 'Reviewed-by' not in second
    assert git('log', '-1', '--format=%an', forge.next_branch, cwd=repo) == 'Example Maintainer'
    assert service.collect(forge) == []  # The seed is not a round of its own.
    assert service.seed(forge, project) == ''  # Already there.
    # The agent's next version keeps the trailers it did not invalidate.
    checkout = tmp_path / 'checkout'
    git('worktree', 'add', '-q', str(checkout), forge.next_branch, cwd=repo)
    (checkout / 'f2').write_text('two, reworked\n')
    git('commit', '-q', '-a', '--amend', '--no-edit', cwd=checkout)
    git('worktree', 'remove', '--force', str(checkout), cwd=repo)
    assert service.seed(forge, project) == ''  # Never over the agent's work.
    session(service, forge, 's1', 'forge-rework', {'cover.md': COVER})
    assert service.collect(forge) == [f'the series on {forge.next_branch}, as v2 r1']
    _series, thread = service.thread(forge, Round(2, 1))
    assert 'Reviewed-by: Rev Iewer' in thread.patches[0].message.body
    assert thread.patches[0].message.subject.startswith('[PATCH v2 1/2]')


def test_nothing_is_seeded_without_an_upstream_posting(world, tmp_path):
    project, service = world
    forge = service.create('Demo', 'linux', 'main', '')
    assert service.seed(forge, project) == ''
    assert not service.space_repo(forge).exists()


def sashiko_output(subjects):
    """`sashiko review --format json` as a local run writes it (trimmed): findings with
    the patch they are on, and the inline review, one `commit` section per patch."""
    findings = [
        {
            'id': 'VF1',
            'patch_index': 1,
            'patch_subject': subjects[0],
            'severity': 'High',
            'severity_explanation': 'Writes past the allocation.',
            'preexisting': False,
            'problem': 'heap buffer overflow writing f1',
            'locations': [{'file': 'f1', 'line': 1, 'code_snippet': 'one'}],
        },
        {
            'id': 'VF2',
            'patch_index': 2,
            'patch_subject': subjects[1],
            'severity': 'Low',
            'severity_explanation': 'Old.',
            'preexisting': True,
            'problem': 'an old wart',
            'locations': [],
        },
    ]
    inline = ''.join(
        f'commit {str(n) * 40}\nAuthor: Example Maintainer <maintainer@example.org>\n\n'
        f'{subject}\n\n> +text\n\n[Severity: High]\nIs this right in patch {n}?\n\n'
        for n, subject in enumerate(subjects, 1)
    )
    return {
        'patches': [{'index': n, 'status': 'present'} for n in (1, 2)],
        'review': {'findings': findings, 'summary': 'two findings'},
        'inline_review': inline,
        'tokens_in': 1000,
        'tokens_out': 100,
    }


def test_a_sashiko_review_runs_locally_with_pi_and_lands_like_sashikos(world, tmp_path):
    project, service = world
    forge = service.create('Demo', 'linux', 'main', '')
    _repo, checkout = make_space(service, forge, project, tmp_path)
    commit_series(checkout, forge, ['one\n', 'two\n'])
    session(service, forge, 's1', 'forge-implement', {'cover.md': COVER})
    service.collect(forge)
    binary = tmp_path / 'sashiko'
    binary.write_text('')
    service.sashiko_binary = binary
    service.sashiko_prompts = tmp_path / 'prompts'
    calls = []

    def run(argv, cwd, settings):
        calls.append((argv, cwd, settings.read_text()))
        return 1, json.dumps(sashiko_output(['demo: add f1', 'demo: add f2'])), ''

    said = service.sashiko(forge, Round(1, 1), 'provider/model-x', run=run)
    assert said == 'Sashiko on v1 r1 (provider/model-x): 1 new finding (high 1); 1000 in, 100 out'
    ((argv, cwd, settings),) = calls
    info = service.round_info(forge, Round(1, 1))
    patches = git(
        'rev-list', '--reverse', f'{info["fork"]}..{info["tip"]}', cwd=service.space_repo(forge)
    ).split()
    assert argv[:3] == [str(binary), 'review', f'{patches[0]}..{info["tip"]}']
    assert '--format' in argv and 'json' in argv and '--prompts' in argv
    assert 'provider = "pi-cli"' in settings and 'model = "provider/model-x"' in settings
    assert f'repository_path = "{cwd}"' in settings
    assert (pathlib.Path(cwd) / 'f2').read_text() == 'two\n'  # The round's tip.
    _series, thread = service.thread(forge, Round(1, 1))
    review = service.review(forge, Round(1, 1), thread)
    assert (review.high, review.low, review.findings) == (1, 0, 1)
    assert review.patches[0].findings[0].locations == (('f1', 1),)
    replies = [r for p in thread.patches for r in p.replies]
    assert [r.message.author_email for r in replies] == ['sashiko@forge.local'] * 2
    assert 'Is this right in patch 1?' in thread.patches[0].replies[0].message.body
    assert 'patch 2' not in thread.patches[0].replies[0].message.body


def test_sashiko_needs_its_build(world, tmp_path):
    _project, service = world
    forge = service.create('Demo', 'linux', 'main', '')
    service.sashiko_binary = tmp_path / 'missing'
    with pytest.raises(ForgeError, match='build'):
        service.sashiko(forge, Round(1, 1), '', run=lambda *a: (0, '{}', ''))


def test_collecting_again_takes_only_what_is_new(world, tmp_path):
    _project, service = world
    forge = service.create('Demo', 'linux', 'main', '')
    home = session(service, forge, 's1', 'forge-plan', {'design.md': 'One patch.\n'})
    assert service.collect(forge) == ['s1: the design, as plan r1; s i implements it']
    assert service.collect(forge) == []  # Nothing new.
    # The same session, asked again: it rewrites its design.
    (home / 'work' / 'out' / 'forge' / 'design.md').write_text('Two patches.\n')
    assert service.collect(forge) == ['s1: the design, as plan r2; s i implements it']
    assert service.forge_for(forge.workbench) == forge
    assert service.forge_for('no-such-bench') is None


def test_a_forge_without_a_goal_says_the_design_session_asks_for_it(world, tmp_path):
    _project, service = world
    forge = service.create('Demo', 'linux', 'main', '')
    folder = service.write_context(forge)
    assert 'ask the maintainer' in (folder / 'prompt.md').read_text()
    assert 'ask the maintainer' in (folder / 'README.md').read_text()


def test_removing_a_forge_takes_its_rounds_workbench_and_space_not_the_prep_branch(world, tmp_path):
    project, service = world
    forge = service.create('Demo', 'linux', 'main', 'Add two files.\n')
    keep = service.create('Other', 'linux', 'main', '')
    service.post_plan(forge, 'One patch.\n')
    make_space(service, forge, project, tmp_path)
    service.write_context(forge)
    git('branch', f'b4/{forge.id}', 'main', cwd=project)  # As f folds a round.
    assert service.removal(forge, project) == (1, f'b4/{forge.id}')
    service.remove(forge)
    assert service.store.get(forge.id) is None
    assert not service.store.prompt_path(forge.id).exists()
    assert not service.forge_dir(forge).exists() and not service.space(forge).exists()
    assert service.benches.get(forge.workbench) is None
    assert git('rev-parse', '--verify', f'b4/{forge.id}', cwd=project)  # Stays.
    assert [f.id for f in service.store.list()[0]] == [keep.id]
    assert service.benches.get(keep.workbench) is not None


def test_an_mbox_jumpstarts_a_forge_at_its_newest_version(world, tmp_path, posted):
    import mailbox

    _project, service = world
    path = tmp_path / 'series.mbx'
    box = mailbox.mbox(path)
    for msg in posted[0]:
        box.add(msg.read_bytes())
    for msg in posted[1]:  # Its next version, in the same file.
        box.add(msg.read_bytes().replace(b'[PATCH ', b'[PATCH v2 '))
    box.flush()
    series = service.read_mbox(path)
    assert (series.subject, series.revision) == ('demo: two changes', 2)
    forge = service.create_from_upstream(
        series.subject, 'linux', 'main', path, series.revision, series.message_id
    )
    (up,) = service.rounds(forge)
    assert up.kind == 'upstream' and up.cycle == series.revision
    _series, thread = service.thread(forge, up)
    assert [p.message.subject.split('] ')[-1] for p in thread.patches] == [
        'demo: add f1',
        'demo: add f2',
    ]
    path.unlink()  # The forge keeps its own copy.
    assert len(service.thread(forge, up)[1].patches) == 2


def test_an_mbox_without_a_series_is_refused(world, tmp_path):
    _project, service = world
    empty = tmp_path / 'empty.mbx'
    empty.write_text('')
    with pytest.raises(ForgeError, match='no patch series'):
        service.read_mbox(empty)
    with pytest.raises(ForgeError, match='no such file'):
        service.read_mbox(tmp_path / 'missing.mbx')


def test_sashiko_reviews_an_upstream_round_applied_onto_the_base(world, tmp_path, posted):
    import mailbox

    project, service = world
    upstream = tmp_path / 'thread'
    box = mailbox.Maildir(upstream, create=True)
    for path in posted[0]:
        box.add(path.read_bytes())
    forge = service.create_from_upstream('Demo', 'linux', 'main', upstream, 1, 'c@example.org')
    up = Round(1, 0)
    binary = tmp_path / 'sashiko'
    binary.write_text('')
    service.sashiko_binary = binary
    calls = []

    def run(argv, cwd, settings):
        calls.append((argv, cwd))
        return 0, json.dumps(sashiko_output(['demo: add f1', 'demo: add f2'])), ''

    head = git('rev-parse', 'HEAD', cwd=project)
    said = service.sashiko(forge, up, 'provider/model-x', run=run, project=project)
    assert said.startswith('Sashiko on v1 upstream (provider/model-x): 1 new finding')
    ((argv, cwd),) = calls
    assert (pathlib.Path(cwd) / 'f2').read_text() == '2\n'  # The posting, applied.
    info = service.round_info(forge, up)
    patches = git(
        'rev-list', '--reverse', f'{info["fork"]}..{info["tip"]}', cwd=service.space_repo(forge)
    ).split()
    assert len(patches) == 3  # A cover commit, then the two patches, as b4 prep keeps them.
    assert argv[2] == f'{patches[0]}..{info["tip"]}'
    _series, thread = service.thread(forge, up)
    replies = [r for p in thread.patches for r in p.replies]
    assert [r.message.author_email for r in replies] == ['sashiko@forge.local'] * 2
    assert git('rev-parse', 'HEAD', cwd=project) == head  # The project is untouched.
    # Reviewed again, the applied round is reused.
    service.sashiko(forge, up, 'provider/model-x', run=run, project=project)
    assert service.round_info(forge, up)['tip'] == info['tip']


def test_a_base_the_checkout_cannot_resolve_says_so(world, tmp_path, posted):
    import mailbox

    from b4_maintainer.forge import check_base

    project, service = world
    upstream = tmp_path / 'thread'
    box = mailbox.Maildir(upstream, create=True)
    for path in posted[0]:
        box.add(path.read_bytes())
    forge = service.create_from_upstream('Demo', 'linux', 'mm-new', upstream, 1, 'c@example.org')
    binary = tmp_path / 'sashiko'
    binary.write_text('')
    service.sashiko_binary = binary
    with pytest.raises(ForgeError, match="the base 'mm-new' is not a commit in linux"):
        service.sashiko(forge, Round(1, 0), '', run=lambda *a: (0, '{}', ''), project=project)
    check_base(project, 'main')  # Fine.
    check_base(tmp_path / 'not-checked-out', 'anything')  # Nothing to check against.
    with pytest.raises(ForgeError, match='is not a commit'):
        check_base(project, 'mm-new')


def test_a_posting_that_misses_the_forge_base_applies_on_its_own_base(world, tmp_path, posted):
    import mailbox

    project, service = world
    main = git('rev-parse', 'main', cwd=project)
    git('switch', '-q', '-c', 'other', cwd=project)  # The forge's base: f1 already differs.
    (project / 'f1').write_text('clash\n')
    git('add', 'f1', cwd=project)
    git('commit', '-q', '-m', 'f1 differs', cwd=project)
    git('switch', '-q', 'main', cwd=project)
    upstream = tmp_path / 'thread'
    box = mailbox.Maildir(upstream, create=True)
    for path in posted[0]:
        box.add(path.read_bytes())
    forge = service.create_from_upstream('Demo', 'linux', 'other', upstream, 1, 'c@example.org')
    # Reviewed, the posting is checked out where b4 finds it applies (here the HEAD).
    info = service.snapshot(forge, Round(1, 0), project)
    assert info['fork'] == main
    repo = service.space_repo(forge)
    assert git('show', f'{info["tip"]}:f1', cwd=repo) == '1'
    # The next version starts on the forge's base when it can, else there too, and says so.
    said = service.seed(forge, project)
    assert 'does not apply on other' in said and main[:12] in said
    assert git('rev-parse', f'{forge.next_branch}~2', cwd=repo) == main


def test_a_prep_checkout_that_lost_its_entry_is_repaired(world, tmp_path):
    import shutil

    project, service = world
    forge = service.create('Demo', 'linux', 'main', '')
    repo, checkout = make_space(service, forge, project, tmp_path)
    commit_series(checkout, forge, ['one\n', 'two\n'])
    session(service, forge, 's1', 'forge-implement', {'cover.md': COVER})
    service.collect(forge)
    shutil.rmtree(repo / 'worktrees' / 'tree')  # Its directory stays; git forgot it.
    commit_series(checkout, forge, ['uno\n', 'two\n'])
    session(service, forge, 's2', 'forge-rework', {'cover.md': COVER + '\nChanges in r2:\n- f1\n'})
    assert service.collect(forge) == [f'the series on {forge.next_branch}, as v1 r2']
    _series, thread = service.thread(forge, Round(1, 2))
    assert len(thread.patches) == 2


def test_collecting_a_forge_twice_at_once_does_not_overlap(world, tmp_path):
    import threading
    import time

    project, service = world
    forge = service.create('Demo', 'linux', 'main', '')
    _repo, checkout = make_space(service, forge, project, tmp_path)
    commit_series(checkout, forge, ['one\n', 'two\n'])
    session(service, forge, 's1', 'forge-implement', {'cover.md': COVER})
    inside, most = [0], [0]
    post = service.post_series

    def slow(*args, **kwargs):
        inside[0] += 1
        most[0] = max(most[0], inside[0])
        time.sleep(0.2)
        try:
            return post(*args, **kwargs)
        finally:
            inside[0] -= 1

    service.post_series = slow
    errors = []

    def collect():
        try:
            service.collect(forge)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=collect) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and most[0] == 1
    assert [r.label for r in service.rounds(forge)] == ['v1 r1']  # Posted once.


def test_a_damaged_prep_checkout_is_made_again(world, tmp_path):
    project, service = world
    forge = service.create('Demo', 'linux', 'main', '')
    _repo, checkout = make_space(service, forge, project, tmp_path)
    commit_series(checkout, forge, ['one\n', 'two\n'])
    session(service, forge, 's1', 'forge-implement', {'cover.md': COVER})
    service.collect(forge)
    tree = service.forge_dir(forge) / 'tree'
    (tree / 'README').unlink()  # As a rebuilt entry without its sparse settings shows it.
    commit_series(checkout, forge, ['uno\n', 'two\n'])
    session(service, forge, 's2', 'forge-rework', {'cover.md': COVER + '\nChanges in r2:\n- f1\n'})
    assert service.collect(forge) == [f'the series on {forge.next_branch}, as v1 r2']


def fake_sashiko(tmp_path, output, gate):
    """A stand-in for `sashiko review`: progress on stderr as Sashiko writes it when
    redirected, then (once *gate* exists) its JSON on stdout; 1 as with high findings."""
    data = tmp_path / 'sashiko-output.json'
    data.write_text(json.dumps(output))
    binary = tmp_path / 'sashiko'
    binary.write_text(
        '#!/bin/sh\n'
        'echo "Running review for 2 patches" >&2\n'
        'echo "      [Patch 1] demo: add f1 | Planning stages..." >&2\n'
        'echo "      [Patch 1] demo: add f1 | Finished" >&2\n'
        'echo "      [Patch 2] demo: add f2 | Reviewing..." >&2\n'
        f'while [ ! -f {gate} ]; do sleep 0.05; done\n'
        f'cat {data}\n'
        'exit 1\n'
    )
    binary.chmod(0o755)
    return binary


def wait_for(check, timeout=10.0):
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        found = check()
        if found:
            return found
        time.sleep(0.05)
    raise AssertionError('timed out')


def test_sashiko_runs_detached_shows_progress_and_lands_when_it_ends(world, tmp_path):
    project, service = world
    forge = service.create('Demo', 'linux', 'main', '')
    _repo, checkout = make_space(service, forge, project, tmp_path)
    commit_series(checkout, forge, ['one\n', 'two\n'])
    session(service, forge, 's1', 'forge-implement', {'cover.md': COVER})
    service.collect(forge)
    gate = tmp_path / 'go'
    output = sashiko_output(['demo: add f1', 'demo: add f2'])
    service.sashiko_binary = fake_sashiko(tmp_path, output, gate)
    said = service.start_sashiko(forge, Round(1, 1), 'provider/model-x')
    assert 'in the background' in said
    job = service.sashiko_job(forge)
    assert job['label'] == 'v1 r1' and job['model'] == 'provider/model-x'
    with pytest.raises(ForgeError, match='already reviewing v1 r1'):
        service.start_sashiko(forge, Round(1, 1), '')
    progress = wait_for(lambda: service.sashiko_poll(forge)[0] == '1/2' and '1/2')
    assert progress == '1/2'  # One of two patches finished.
    gate.write_text('')
    landed = wait_for(lambda: service.sashiko_poll(forge)[1])
    assert landed.startswith('Sashiko on v1 r1 (provider/model-x): 1 new finding (high 1)')
    assert service.sashiko_job(forge) == {}  # Done.
    _series, thread = service.thread(forge, Round(1, 1))
    assert [r.message.author_email for p in thread.patches for r in p.replies] == [
        'sashiko@forge.local'
    ] * 2
    assert service.sashiko_poll(forge) == ('', '')


def test_a_sashiko_job_can_be_stopped_and_a_lost_one_is_said(world, tmp_path):
    project, service = world
    forge = service.create('Demo', 'linux', 'main', '')
    _repo, checkout = make_space(service, forge, project, tmp_path)
    commit_series(checkout, forge, ['one\n', 'two\n'])
    session(service, forge, 's1', 'forge-implement', {'cover.md': COVER})
    service.collect(forge)
    service.sashiko_binary = fake_sashiko(tmp_path, {}, tmp_path / 'never')
    service.start_sashiko(forge, Round(1, 1), '')
    pid = service.sashiko_job(forge)['pid']
    assert service.stop_sashiko(forge) == 'Stopped Sashiko on v1 r1'
    assert service.sashiko_job(forge) == {}
    from b4_maintainer.forge import _alive

    wait_for(lambda: not _alive(pid))
    # A job whose process is gone without an exit status (the machine restarted, say).
    service.start_sashiko(forge, Round(1, 1), '')
    job = service.sashiko_job(forge)
    import os
    import signal

    os.killpg(job['pid'], signal.SIGKILL)
    wait_for(lambda: not _alive(job['pid']))
    _progress, said = service.sashiko_poll(forge)
    assert 'ended without an exit status' in said
    assert service.sashiko_job(forge) == {}


def test_the_progress_reads_sashikos_own_lines():
    from b4_maintainer.forge import sashiko_progress

    assert sashiko_progress('') == 'starting'
    assert sashiko_progress('   Applying patch 3/62: mm: x\n') == 'applying 3/62'
    log = (
        'Running review for 3 patches\n'
        '      [Patch 1] a | Finished\n'
        '      [Patch 2] b | Incomplete\n'
        '      [Patch 3] c | Reviewing...\n'
    )
    assert sashiko_progress(log) == '2/3'
