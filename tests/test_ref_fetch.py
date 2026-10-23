"""Contract: refresh fetches the remotes behind configured refs before scanning them."""

from gitutil import git, make_checkout
from test_live import MM, NOW, Fakes, mail

from b4_maintainer.adapters.live import LiveSource

IDENT = ['-c', 'user.name=A', '-c', 'user.email=a@example.org']


def config(fetch=None):
    extra = '' if fetch is None else f'\n[refs]\nfetch = {fetch}\n'
    return f"""\
[[projects]]
id = "demo"
path = "projects/demo"
lists = ["linux-mm@kvack.org"]
refs = [
  {{ label = "m", name = "up/master", authority = "mainline" }},
  {{ label = "n", name = "up/next", authority = "acceptance", ends_review = true }},
  {{ label = "l", name = "master", authority = "preliminary" }},
]

[[profiles]]
id = "mm"
label = "MM"
project = "demo"
lists = ["linux-mm@kvack.org"]
{extra}"""


def workspace(tmp_path, fetch=None):
    upstream = make_checkout(tmp_path / 'upstream')
    git('branch', 'next', cwd=upstream)
    root = tmp_path / 'ws'
    checkout = make_checkout(root / 'projects' / 'demo')
    git('remote', 'add', 'up', f'file://{upstream}', cwd=checkout)
    git('fetch', '-q', 'up', cwd=checkout)
    (root / 'workspace.toml').write_text(config(fetch))
    fakes = Fakes()
    source = LiveSource(
        root,
        tracked_series=fakes.tracked_series,
        search_for=fakes.search_for,
        track=fakes.track,
        b4_command=['b4'],
        now=lambda: NOW,
        identity=lambda c: None,
    )
    return source, fakes, upstream, checkout


def test_refresh_fetches_each_refs_remote_then_scans(tmp_path):
    source, fakes, upstream, checkout = workspace(tmp_path)
    sent, msgs = mail(tmp_path, 'a', 'mm: in next')
    fakes.answers[MM] = msgs
    git('checkout', '-q', 'next', cwd=upstream)
    for n in (1, 2):
        link = f'Link: https://lore.kernel.org/{sent.msgids[n]}'
        git(
            *IDENT,
            'commit',
            '-q',
            '--allow-empty',
            '-m',
            f'mm: in next {n}\n\n{link}',
            cwd=upstream,
        )
    git('checkout', '-q', 'master', cwd=upstream)
    git(*IDENT, 'commit', '-q', '--allow-empty', '-m', 'mainline moves too', cwd=upstream)

    result = source.refresh()
    assert git('rev-parse', 'up/master', cwd=checkout) == git('rev-parse', 'master', cwd=upstream)
    assert git('rev-parse', 'up/next', cwd=checkout) == git('rev-parse', 'next', cwd=upstream)
    assert 'failed' not in result
    [item] = source.load().items
    assert [(label, cov.value) for label, _, cov in item.state.tree][:2] == [
        ('m', 'none'),
        ('n', 'full'),
    ]


def test_a_failed_fetch_is_reported_and_the_scan_goes_on(tmp_path):
    source, fakes, upstream, checkout = workspace(tmp_path)
    git('remote', 'set-url', 'up', str(tmp_path / 'gone'), cwd=checkout)
    fakes.answers[MM] = mail(tmp_path, 'a', 'mm: something')[1]
    result = source.refresh()
    assert 'fetch up failed' in result
    assert 'refs scanned' in result


def test_fetching_can_be_turned_off(tmp_path):
    source, fakes, upstream, checkout = workspace(tmp_path, fetch='false')
    before = git('rev-parse', 'up/master', cwd=checkout)
    git(*IDENT, 'commit', '-q', '--allow-empty', '-m', 'later', cwd=upstream)
    fakes.answers[MM] = mail(tmp_path, 'a', 'mm: something')[1]
    source.refresh()
    assert git('rev-parse', 'up/master', cwd=checkout) == before
