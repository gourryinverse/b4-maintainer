# SPDX-License-Identifier: GPL-2.0-or-later
"""A synthetic live workspace for trying the app and taking screenshots.

`b4-maintainer --showcase DIR` writes it (once) and opens it like any live workspace:
workspace.toml, two project checkouts enrolled with workspace-local b4 state, upstream
branches holding some of the series, a mail cache of series, discussions, and robot
reports, tracked series, a draft, a workbench with sessions and their artifacts.

Every identity is synthetic (example.org) and every list too, so F5 finds nothing on
lore; replies are sent with --email-dry-run. Nothing outside DIR is read or written
(b4's data and cache are DIR/var/b4).
"""

import datetime
import email.message
import email.utils
import pathlib
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Optional

ME = ('Example Maintainer', 'maintainer@example.org')
MM, CXL = 'mm.lists.example.org', 'cxl.lists.example.org'
PEOPLE = [
    ('Ada Contributor', 'ada@example.org'),
    ('Ben Developer', 'ben@example.org'),
    ('Chloe Hacker', 'chloe@example.org'),
    ('Dev Patchwright', 'dev@example.org'),
    ('Eli Kernelton', 'eli@example.org'),
    ('Fay Reviewer', 'fay@example.org'),
    ('Gus Tester', 'gus@example.org'),
]
REVIEWER = ('Fay Reviewer', 'fay@example.org')
TESTER = ('Gus Tester', 'gus@example.org')

CONFIG = """\
# A synthetic workspace (b4-maintainer --showcase): example.org identities and lists.
me = ["maintainer@example.org"]

[browse]
window_days = 30
exclude = []

[refs]
fetch = false  # The upstream branches are local refs: nothing to fetch.

[[profiles]]
id = "mm"
label = "MM"
project = "linux"
lists = ["mm@lists.example.org"]

[[profiles]]
id = "cxl"
label = "CXL"
project = "linux"
lists = ["cxl@lists.example.org"]

[[projects]]
id = "linux"
label = "Linux"
path = "projects/linux"
lists = ["mm@lists.example.org", "cxl@lists.example.org"]
get_maintainer = [{python}, "scripts/get_maintainer.py"]
refs = [
  {{ label = "m", name = "mainline/master", authority = "mainline" }},
  {{ label = "n", name = "next/master", authority = "acceptance", ends_review = true }},
  {{ label = "s", name = "mm/mm-stable", authority = "acceptance", view = "MM-stable" }},
  {{ label = "u", name = "mm/mm-unstable", authority = "preliminary", view = "MM-unstable" }},
  {{ label = "c", name = "core/for-next", authority = "acceptance", view = "Core" }},
  {{ label = "g", name = "maintainer/base", authority = "preliminary", view = "Mine" }},
]
bases = [{{ branch = "maintainer/base", upstream = "mm/mm-stable" }}]

[[projects]]
id = "cxl-cli"
label = "cxl-cli"
path = "projects/cxl-cli"
lists = ["cxl@lists.example.org"]
subject_tags = ["cxl-cli"]
refs = [{{ label = "m", name = "origin/main", authority = "mainline" }}]
"""

MAINTAINERS = """\
MEMORY MANAGEMENT
M:\tExample Maintainer <maintainer@example.org>
R:\tFay Reviewer <fay@example.org>
F:\tmm/
F:\ttools/testing/selftests/mm/

CXL (Compute Express Link)
M:\tChloe Hacker <chloe@example.org>
R:\tExample Maintainer <maintainer@example.org>
F:\tdrivers/cxl/
"""

# A stand-in for get_maintainer.pl: owners by path prefix, from the patch on stdin.
GET_MAINTAINER = """\
import sys
owners = {
    'mm/': ['Example Maintainer <maintainer@example.org>', 'Fay Reviewer <fay@example.org>'],
    'tools/testing/selftests/mm/': ['Example Maintainer <maintainer@example.org>'],
    'drivers/cxl/': ['Chloe Hacker <chloe@example.org>'],
}
paths = [l[6:].strip() for l in sys.stdin if l.startswith('+++ b/')]
seen = []
for path in paths:
    for prefix, names in owners.items():
        if path.startswith(prefix):
            seen += [n for n in names if n not in seen]
print('\\n'.join(seen))
"""

COVER = """\
{why}

This series {what}. Each patch stands alone and keeps behaviour the same
for existing users; the last one adds a selftest.

Tested on x86_64 and arm64 with the mm selftests and a kernel build of
allmodconfig.

Changes since v{prev}:
- Reworded the changelogs after review.
- Dropped the unrelated cleanup from patch 2.
"""


@dataclass
class Posting:
    """One series to write: its subject, patches (summary, path), and what holds it."""

    key: str
    subject: str
    author: tuple[str, str]
    patches: list[tuple[str, str]]
    lists: tuple[str, ...] = (MM,)
    revision: int = 1
    days_ago: float = 1
    why: str = 'Users hit this under memory pressure.'
    what: str = 'fixes it with the smallest change that works'
    replies: list[tuple[int, tuple[str, str], str]] = field(default_factory=list)
    trailers: dict[int, list[str]] = field(default_factory=dict)
    tag: str = ''
    rfc: bool = False
    older: tuple[int, ...] = ()  # Earlier revisions also posted.


POSTINGS = [
    Posting(
        'foo',
        'mm: fix foo leak on the error path',
        PEOPLE[0],
        [
            ('mm/foo: free the cache on the error path', 'mm/foo.c'),
            ('mm/foo: return -ENOMEM instead of -EINVAL', 'mm/foo.c'),
            ('mm/foo: drop the unused flags argument', 'mm/foo.h'),
            ('selftests/mm: cover the foo error path', 'tools/testing/selftests/mm/foo.c'),
        ],
        revision=3,
        days_ago=2,
        older=(1, 2),
        replies=[
            (0, REVIEWER, 'Acked-by: Fay Reviewer <fay@example.org>'),
            (2, TESTER, 'Tested-by: Gus Tester <gus@example.org>'),
        ],
        trailers={1: ['Reviewed-by: Fay Reviewer <fay@example.org>']},
    ),
    Posting(
        'lru',
        'mm/vmscan: batch LRU isolation',
        PEOPLE[1],
        [
            ('mm/vmscan: isolate folios in batches', 'mm/vmscan.c'),
            ('mm/vmscan: count batched isolations', 'mm/vmstat.c'),
            ('mm/vmscan: document the batch size', 'mm/vmscan.c'),
        ],
        revision=2,
        days_ago=4,
        why='Reclaim spends most of its time on the LRU lock.',
        what='takes the lock once per batch',
        older=(1,),
    ),
    Posting(
        'zone',
        'mm/page_alloc: tidy the zone fallback order',
        PEOPLE[2],
        [('mm/page_alloc: tidy the zone fallback order', 'mm/page_alloc.c')],
        days_ago=6,
    ),
    Posting(
        'interleave',
        'mm/mempolicy: weighted interleave defaults',
        ME,
        [
            ('mm/mempolicy: compute default weights from bandwidth', 'mm/mempolicy.c'),
            ('mm/mempolicy: recompute weights on memory hotplug', 'mm/mempolicy.c'),
            ('mm/mempolicy: expose the defaults in sysfs', 'mm/mempolicy.c'),
            ('selftests/mm: weighted interleave defaults', 'tools/testing/selftests/mm/wi.c'),
        ],
        revision=4,
        days_ago=3,
        older=(3,),
        replies=[
            (
                2,
                REVIEWER,
                'Should this take the hotplug lock? The weights can change '
                'underneath the reader otherwise.',
            ),
            (0, PEOPLE[4], 'Nice. One question on the sysfs naming below.'),
        ],
    ),
    Posting(
        'collapse',
        'mm/khugepaged: retry collapse after a failed isolation',
        PEOPLE[3],
        [
            ('mm/khugepaged: retry once after a failed isolation', 'mm/khugepaged.c'),
            ('mm/khugepaged: report retries in vmstat', 'mm/vmstat.c'),
        ],
        days_ago=1.5,
        rfc=True,
    ),
    Posting(
        'migrate',
        'selftests/mm: add a migration stress test',
        PEOPLE[4],
        [('selftests/mm: add a migration stress test', 'tools/testing/selftests/mm/migrate.c')],
        days_ago=0.5,
    ),
    Posting(
        'shmem',
        'mm/shmem: truncate folios in batches',
        PEOPLE[1],
        [
            (f'mm/shmem: {s}', 'mm/shmem.c')
            for s in (
                'add a folio batch helper',
                'truncate full folios in batches',
                'truncate partial folios last',
                'drop the per-page loop',
                'update the truncation comments',
                'add a truncation benchmark',
            )
        ],
        revision=2,
        days_ago=8,
        older=(1,),
        trailers={
            2: [
                'Fixes: 0123456789ab ("mm/shmem: truncate under the lock")',
                'Cc: stable@vger.kernel.org',
            ]
        },
    ),
    Posting(
        'gup',
        'mm/gup: simplify pin accounting',
        PEOPLE[5],
        [
            ('mm/gup: count pins in one place', 'mm/gup.c'),
            ('mm/gup: drop the duplicate overflow check', 'mm/gup.c'),
        ],
        days_ago=5,
    ),
    Posting(
        'tiers',
        'mm/memory-tiers: name the default tier',
        ME,
        [('mm/memory-tiers: name the default tier', 'mm/memory-tiers.c')],
        days_ago=10,
    ),
    Posting(
        'region',
        'cxl/region: fix decoder ordering',
        PEOPLE[2],
        [
            ('cxl/region: sort decoders by HPA', 'drivers/cxl/core/region.c'),
            ('cxl/region: reject overlapping decoders', 'drivers/cxl/core/region.c'),
        ],
        lists=(CXL,),
        revision=2,
        days_ago=3,
        older=(1,),
    ),
    Posting(
        'ras',
        'cxl/pci: log RAS events for the root port',
        PEOPLE[6],
        [
            ('cxl/pci: read the root port RAS capability', 'drivers/cxl/pci.c'),
            ('cxl/pci: log correctable errors', 'drivers/cxl/pci.c'),
            ('cxl/pci: log uncorrectable errors', 'drivers/cxl/pci.c'),
        ],
        lists=(CXL,),
        days_ago=2.5,
    ),
    Posting(
        'list',
        'cxl-cli: list regions by decoder',
        PEOPLE[2],
        [('cxl-cli: list regions by decoder', 'cxl/list.c')],
        lists=(CXL,),
        days_ago=1,
        tag='cxl-cli',
    ),
]

# Where each series is (by key): every patch in these refs.
HELD = {
    'mm/mm-stable': ['foo', 'zone'],
    'mm/mm-unstable': ['lru'],
    'next/master': ['region'],
    'core/for-next': ['gup'],
    'mainline/master': ['tiers'],  # Merged: out of the queues.
}
TRACKED = ['lru', 'collapse', 'ras']  # In To Review (b4 tracks them).


def _git(repo: pathlib.Path, *args: str, env: Optional[dict] = None) -> str:
    ident = ['-c', f'user.name={ME[0]}', '-c', f'user.email={ME[1]}']
    run = subprocess.run(
        ['git', *ident, *args], cwd=repo, capture_output=True, text=True, check=True, env=env
    )
    return run.stdout.strip()


def _checkout(path: pathlib.Path, files: dict[str, str]) -> str:
    """A repository at *path* with *files* committed on master; its commit."""
    path.mkdir(parents=True)
    _git(path, 'init', '-q', '-b', 'master')
    _git(path, 'config', 'user.name', ME[0])
    _git(path, 'config', 'user.email', ME[1])
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    _git(path, 'add', '-A')
    _git(path, 'commit', '-q', '-m', 'Initial import')
    return _git(path, 'rev-parse', 'HEAD')


def _commit(repo: pathlib.Path, parent: str, message: str) -> str:
    return _git(repo, 'commit-tree', f'{parent}^{{tree}}', '-p', parent, '-m', message)


def _series_mail(posting: Posting, now: datetime.datetime, tmp: pathlib.Path) -> tuple[list, dict]:
    """The postings of *posting* (each revision) as messages, and the newest's ids."""
    import b4

    from .fixtures import Followup, PatchSpec, write_series_mbox

    msgs: list = []
    newest: dict = {}
    for revision in (*posting.older, posting.revision):
        days = posting.days_ago + (posting.revision - revision) * 7
        change_id = f'20261001-{posting.key}-{"0123456789ab"}'
        followups = (
            [
                Followup(
                    target,
                    body='' if ':' in text.split(' ', 1)[0] else text,
                    trailer=text if ':' in text.split(' ', 1)[0] else '',
                    sender=who,
                )
                for target, who, text in posting.replies
            ]
            if revision == posting.revision
            else []
        )
        path = tmp / f'{posting.key}-v{revision}.mbx'
        sent = write_series_mbox(
            path,
            posting.subject,
            revision=revision,
            patches=[
                PatchSpec(p, summary=s, trailers=tuple(posting.trailers.get(n, ())))
                for n, (s, p) in enumerate(posting.patches, 1)
            ],
            lists=posting.lists,
            change_id=change_id,
            followups=followups,
            author=posting.author,
            date=now - datetime.timedelta(days=days),
            cover_text=COVER.format(why=posting.why, what=posting.what, prev=max(revision - 1, 1)),
            tag=posting.tag,
        )
        found = b4.get_msgs_from_mailbox_or_maildir(str(path))
        if posting.rfc:
            for m in found:
                m.replace_header('Subject', str(m['Subject']).replace('[PATCH', '[RFC PATCH'))
        msgs += found
        if revision == posting.revision:
            newest = {'change_id': change_id, 'msgids': sent.msgids, 'revision': revision}
    return msgs, newest


def _message(
    msgid: str,
    sender: tuple[str, str],
    subject: str,
    when: datetime.datetime,
    body: str,
    parent: Optional[str] = None,
    root: Optional[str] = None,
    lst: str = MM,
) -> email.message.EmailMessage:
    msg = email.message.EmailMessage()
    msg['From'] = email.utils.formataddr(sender)
    msg['To'] = lst.replace('.', '@', 1)
    msg['Subject'] = subject if parent is None else f'Re: {subject}'
    msg['Date'] = email.utils.format_datetime(when)
    msg['Message-ID'] = f'<{msgid}>'
    msg['List-Id'] = f'<{lst}>'
    if parent:
        msg['In-Reply-To'] = f'<{parent}>'
        msg['References'] = ' '.join(f'<{m}>' for m in dict.fromkeys([root or parent, parent]))
    msg.set_content(body)
    return msg


def _talk(now: datetime.datetime) -> list:
    """Discussions and robot reports: threads without [PATCH]."""
    day = datetime.timedelta(days=1)
    subject = 'MM: plans for the next merge window'
    root = 'plans-0@example.org'
    plans = [
        _message(
            root,
            PEOPLE[4],
            subject,
            now - 3 * day,
            'Collecting what people expect to land next cycle. Reply with your series\n'
            'and anything blocking them.\n',
        ),
        _message(
            'plans-1@example.org',
            PEOPLE[0],
            subject,
            now - 2.8 * day,
            'The foo fixes are ready from my side.\n',
            root,
            root,
        ),
        _message(
            'plans-2@example.org',
            ME,
            subject,
            now - 2.5 * day,
            'Weighted interleave defaults (v4) once the hotplug question is settled.\n',
            root,
            root,
        ),
        _message(
            'plans-3@example.org',
            PEOPLE[1],
            subject,
            now - 1.2 * day,
            'Can we get the LRU batching into mm-unstable for wider testing?\n',
            'plans-2@example.org',
            root,
        ),
    ]
    agenda = [
        _message(
            'sync-0@example.org',
            PEOPLE[2],
            'CXL sync agenda for this week',
            now - day,
            'Topics: region decoder ordering, RAS logging, cxl-cli release.\n',
            lst=CXL,
        ),
    ]
    robots = [
        _message(
            'syz-0@example.org',
            ('syzbot', 'syzbot+0a1b2c3d@example.org'),
            '[syzbot] [mm?] BUG: bad page state in foo_release',
            now - 2 * day,
            'syzbot found the following issue on a synthetic tree.\n',
        ),
        _message(
            'lkp-0@example.org',
            ('kernel test robot', 'lkp@example.org'),
            '[maintainer:base] BUILD SUCCESS 0123abcd',
            now - 0.7 * day,
            'tree/branch: maintainer/base\nbranch HEAD: 0123abcd\n\nelapsed time: 1442m\n',
        ),
        _message(
            'lkp-1@example.org',
            ('kernel test robot', 'lkp@example.org'),
            '[akpm-mm:mm-unstable 2/3] mm/vmscan.c:42: warning: unused variable',
            now - 0.3 * day,
            'tree: mm-unstable\nhead: 0123abcd\n',
        ),
    ]
    return plans + agenda + robots


def build_showcase(
    root: 'pathlib.Path | str', now: Optional[datetime.datetime] = None
) -> pathlib.Path:
    """Write the showcase workspace into *root* (new or empty); return it."""
    from .adapters import b4state
    from .adapters.drafts import DraftStore
    from .adapters.live import LiveSource
    from .adapters.lorelist import MailCache
    from .projectsetup import _enroll

    root = pathlib.Path(root).resolve()
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f'{root} is not empty')
    root.mkdir(parents=True, exist_ok=True)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    (root / 'workspace.toml').write_text(CONFIG.format(python=f'"{sys.executable}"'))

    linux = root / 'projects' / 'linux'
    base = _checkout(
        linux,
        {
            'MAINTAINERS': MAINTAINERS,
            'scripts/get_maintainer.py': GET_MAINTAINER,
            **{
                f'mm/{n}.c': f'/* {n} */\n'
                for n in (
                    'foo',
                    'vmscan',
                    'vmstat',
                    'page_alloc',
                    'mempolicy',
                    'khugepaged',
                    'shmem',
                    'gup',
                    'memory-tiers',
                )
            },
            'mm/foo.h': '/* foo */\n',
            'drivers/cxl/pci.c': '/* pci */\n',
            'drivers/cxl/core/region.c': '/* region */\n',
            'tools/testing/selftests/mm/foo.c': '/* test */\n',
        },
    )
    cli = root / 'projects' / 'cxl-cli'
    cli_base = _checkout(cli, {'cxl/list.c': '/* list */\n', 'README.md': '# cxl-cli\n'})

    with tempfile.TemporaryDirectory() as tmp:
        msgs, ids = [], {}
        for posting in POSTINGS:
            found, newest = _series_mail(posting, now, pathlib.Path(tmp))
            msgs += found
            ids[posting.key] = newest
        msgs += _talk(now)
        cache = MailCache(root / 'var' / 'mail')
        cache.refresh_query('showcase', lambda since: '', 30, lambda query: msgs, now=now)

    # Upstream branches: each holds its series' patches (found by their Link trailers).
    def landed(parent: str, keys: list[str]) -> str:
        for key in keys:
            posting = next(p for p in POSTINGS if p.key == key)
            for n, (summary, _path) in enumerate(posting.patches, 1):
                link = f'https://lore.kernel.org/r/{ids[key]["msgids"][n]}'
                parent = _commit(linux, parent, f'{summary}\n\nLink: {link}\n')
        return parent

    mainline = landed(
        _commit(linux, base, 'Merge tag for the synthetic release'), HELD['mainline/master']
    )
    stable = landed(mainline, HELD['mm/mm-stable'])
    unstable = landed(stable, HELD['mm/mm-unstable'])
    nxt = landed(stable, HELD['next/master'])  # Not mm-unstable: its series stay in review.
    core = landed(mainline, HELD['core/for-next'])
    mine = _commit(  # The maintainer base: mm-stable and a pulled-ahead fix.
        linux, stable, 'mm/mempolicy: recompute weights on memory hotplug\n\nPulled ahead.\n'
    )
    for ref, sha in (
        ('refs/remotes/mainline/master', mainline),
        ('refs/remotes/mm/mm-stable', stable),
        ('refs/remotes/mm/mm-unstable', unstable),
        ('refs/remotes/next/master', nxt),
        ('refs/remotes/core/for-next', core),
        ('refs/heads/maintainer/base', mine),
    ):
        _git(linux, 'update-ref', ref, sha)
    _git(cli, 'update-ref', 'refs/remotes/origin/main', cli_base)

    # b4: enrolled with the workspace's own state; some series tracked (To Review).
    _enroll(root, linux, 'linux', {}, None)
    _enroll(root, cli, 'cxl-cli', {}, None)
    records = {}
    for key in TRACKED:
        posting = next(p for p in POSTINGS if p.key == key)
        newest = ids[key]
        cover = newest['msgids'].get(0) or newest['msgids'][1]
        sent = (now - datetime.timedelta(days=posting.days_ago)).isoformat()
        records[newest['change_id']] = {
            'revisions': [
                {'revision': newest['revision'], 'message_id': cover, 'subject': posting.subject}
            ],
            'series': [
                {
                    'revision': newest['revision'],
                    'status': 'new',
                    'message_id': cover,
                    'subject': posting.subject,
                    'sender_name': posting.author[0],
                    'sender_email': posting.author[1],
                    'sent_at': sent,
                    'added_at': now.isoformat(),
                    'num_patches': len(posting.patches),
                    'patches': [
                        {'position': n, 'message_id': newest['msgids'][n], 'subject': s}
                        for n, (s, _p) in enumerate(posting.patches, 1)
                    ],
                }
            ],
        }
    b4state.import_series(linux, 'linux', records)

    _prep(root, linux)
    # A saved reply draft, owners from the stand-in get_maintainer, and a workbench.
    foo = ids['foo']['msgids']
    DraftStore(root / 'var' / 'drafts').save(
        foo[2],
        '> Synthetic quoted text.\n',
        '> Synthetic quoted text.\n\nThis looks right to me; one nit on the changelog.\n',
    )
    source = LiveSource(root, identity=lambda checkout: ME[1])
    source._check_owners(source.workspace(), lambda _message: None)
    source._scan_refs(source.workspace(), lambda _message: None)  # As F5 would (no fetch).
    _workbench(root, ids)
    return root


PREP_COVER = """\
mm/mempolicy: weighted interleave on memory hotplug

Weighted interleave computes its default weights once, at boot. Memory
added later gets no weight until an administrator writes one. These
patches recompute the defaults when memory comes and goes.
"""


def _prep(root: pathlib.Path, repo: pathlib.Path) -> None:
    """A b4 prep branch on the maintainer base, as F4 makes them (n), with two commits."""
    from .adapters.prep import cut_prep

    worktree = root / 'var' / 'branches' / 'linux' / 'hotplug-weights'
    branch = cut_prep(repo, worktree, 'hotplug-weights', 'maintainer/base', PREP_COVER)
    tip = _git(repo, 'rev-parse', branch)
    for message in (
        'mm/mempolicy: recompute default weights on memory hotplug',
        'mm/mempolicy: document the hotplug behaviour',
    ):
        tip = _commit(repo, tip, f'{message}\n\nSigned-off-by: {ME[0]} <{ME[1]}>\n')
    _git(repo, 'update-ref', f'refs/heads/{branch}', tip)


def _workbench(root: pathlib.Path, ids: dict) -> None:
    """A workbench with two series on the maintainer base, and two ended sessions on it
    with their artifacts."""
    from .projectsetup import config_path
    from .sessions.records import SessionRecord
    from .sessions.store import SessionStore
    from .workbench import Entry, Item, Workbench, WorkbenchStore

    bench = Workbench(
        'mm-fixes',
        'MM fixes',
        (
            Entry(
                'linux',
                'maintainer/base',
                (
                    Item(
                        f'change-id:{ids["foo"]["change_id"]}',
                        3,
                        'mm: fix foo leak on the error path',
                    ),
                    Item(
                        f'change-id:{ids["interleave"]["change_id"]}',
                        4,
                        'mm/mempolicy: weighted interleave defaults',
                    ),
                ),
            ),
        ),
    )
    WorkbenchStore(config_path(root).parent / 'workbenches').save(bench)  # As the app finds them.
    store = SessionStore(root / 'var' / 'sessions')
    sessions = (
        (
            '20261101-091500-a1b2',
            'foo-review',
            'review',
            {
                'reports/review.md': '# Review of mm: fix foo leak\n\nPatch 2 changes the error '
                'code seen by callers; the changelog should say so.\n',
                'findings/error-code.md': '# Finding: changed error code\n\nSeverity: low.\n',
            },
        ),
        (
            '20261101-140000-c3d4',
            'interleave-retest',
            'test',
            {
                'results/selftests.log': 'ok 1 weighted interleave defaults\nok 2 hotplug\n',
                'diffs/hotplug-lock.diff': '--- a/mm/mempolicy.c\n+++ b/mm/mempolicy.c\n'
                '@@ -1 +1 @@\n-/* mempolicy */\n+/* mempolicy, under the hotplug lock */\n',
            },
        ),
    )
    for sid, name, goal, files in sessions:
        record = SessionRecord(
            id=sid,
            created=f'{sid[:4]}-{sid[4:6]}-{sid[6:8]}T{sid[9:11]}:{sid[11:13]}:00+00:00',
            scope='workbench',
            project='linux',
            series='workbench:mm-fixes',
            revision=0,
            subject='MM fixes',
            profile_id='workbench',
            profile_version=1,
            profile_hash='',
            choices={'goal': goal},
            mode='lightweight',
            harness='pi',
            preset='default',
            command=('pi',),
            native_id=sid,
            model='',
            thinking='',
            permissions='read-only',
            state='ended',
            workbench='mm-fixes',
        )
        store.create(record)
        store.rename(sid, name)
        work = store.directory(sid) / 'work'
        (work / 'context').mkdir(parents=True, exist_ok=True)
        (work / 'context' / 'task.md').write_text(f'Goal: {goal} the series on MM fixes.\n')
        for rel, text in files.items():
            (work / 'out' / rel).parent.mkdir(parents=True, exist_ok=True)
            (work / 'out' / rel).write_text(text)
