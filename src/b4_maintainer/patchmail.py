# SPDX-License-Identifier: GPL-2.0-or-later
"""One commit as a patch email, built as git format-patch builds it, for sending through
b4 (Branches, m): a fixup as a reply to the series patch it fixes, a pull-ahead as its
own thread addressed as b4 addresses a new patch.

An agent's commit becomes the maintainer's patch: Assisted-by, then their Signed-off-by.
Another author's commit keeps its author in a From: line at the top of the body.
"""

import email.policy
import email.utils
import json
import pathlib
import subprocess
import sys
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Sequence

Address = tuple[str, str]  # (name, email)


@dataclass(frozen=True)
class ReplyTo:
    """The message a fixup answers: its id and references, and who it went to."""

    msgid: str
    references: str
    to: tuple[Address, ...]  # Its author.
    cc: tuple[Address, ...]  # Everyone else it went to.


def _format_patch(repo: pathlib.Path, sha: str) -> EmailMessage:
    run = subprocess.run(
        ['git', 'format-patch', '-1', '--stdout', '--no-signature', '--subject-prefix=PATCH', sha],
        cwd=repo,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )
    if run.returncode != 0:
        raise ValueError(run.stderr.decode(errors='replace').strip() or 'git format-patch failed')
    raw = run.stdout.split(b'\n', 1)[1] if run.stdout.startswith(b'From ') else run.stdout
    parsed = email.message_from_bytes(raw, policy=email.policy.default)
    assert isinstance(parsed, EmailMessage)
    return parsed


def _with_trailers(message: str, trailers: Sequence[str]) -> str:
    """*message* (a commit message) with *trailers* appended to its trailer block."""
    lines = message.rstrip('\n').split('\n')
    have = {line.strip() for line in lines}
    added = [t for t in trailers if t not in have]
    if not added:
        return message.rstrip('\n') + '\n'
    last = lines[-1] if lines else ''
    in_block = ':' in last and not last.startswith(' ') and len(lines) > 1
    gap = [] if in_block else ['']
    return '\n'.join([*lines, *gap, *added]) + '\n'


def _addresses(addrs: Sequence[Address]) -> str:
    return ', '.join(email.utils.formataddr(a) for a in addrs)


def patch_mail(
    repo: pathlib.Path,
    sha: str,
    me: Address,
    agent_email: str,
    assisted_by: str,
    reply: ReplyTo | None = None,
    recipients: tuple[Sequence[Address], Sequence[Address]] = ((), ()),
) -> EmailMessage:
    """Commit *sha* of *repo* as a patch email from *me*: a reply (*reply*), else a new
    thread to *recipients* (to, cc)."""
    made = _format_patch(repo, sha)
    author_name, author_email = email.utils.parseaddr(str(made['From']))
    body = made.get_content()
    message, sep, rest = body.partition('\n---\n')
    mine = f'Signed-off-by: {email.utils.formataddr(me)}'
    prefix = ''
    if author_email.lower() == agent_email.lower():
        message = _with_trailers(message, [f'Assisted-by: {assisted_by}', mine])
    else:
        message = _with_trailers(message, [mine])
        if author_email.lower() != me[1].lower():
            prefix = f'From: {email.utils.formataddr((author_name, author_email))}\n\n'
    out = EmailMessage(policy=email.policy.default)
    out['From'] = email.utils.formataddr(me)
    to, cc = recipients
    if reply is not None:
        to = reply.to
        cc = tuple(a for a in reply.cc if a[1].lower() != me[1].lower())
    if to:
        out['To'] = _addresses(to)
    if cc:
        out['Cc'] = _addresses([a for a in cc if a not in to])
    out['Subject'] = str(made['Subject'])
    out['Date'] = email.utils.formatdate(localtime=True)
    out['Message-ID'] = email.utils.make_msgid(domain=me[1].rsplit('@', 1)[-1] or None)
    if reply is not None:
        out['In-Reply-To'] = reply.msgid
        out['References'] = reply.references or reply.msgid
    out.set_content(prefix + message + (sep + rest if sep else ''), cte='8bit')
    return out


_RECIPIENTS = """
import argparse, email.utils, json, sys
import b4, b4.ez
b4.setup_config(argparse.Namespace(config=None), topdir='.')
msg = sys.stdin.buffer.read()
tocmd, cccmd = b4.ez.get_auto_to_cc_cmds()
to = b4.ez.get_addresses_from_cmd(tocmd, msg)
cc = b4.ez.get_addresses_from_cmd(cccmd, msg)
print(json.dumps({'to': to, 'cc': cc}))
"""


def auto_recipients(repo: pathlib.Path, msg: EmailMessage) -> tuple[list[Address], list[Address]]:
    """(to, cc) for a new patch as b4 addresses one (b4.send-auto-to-cmd and -cc-cmd, else
    the checkout's get_maintainer.pl). Run in the checkout: b4 finds it by the working
    directory, which this process does not change."""
    run = subprocess.run(
        [sys.executable, '-c', _RECIPIENTS],
        cwd=repo,
        input=msg.as_bytes(),
        capture_output=True,
    )
    if run.returncode != 0:
        lines = run.stderr.decode(errors='replace').strip().splitlines()
        raise ValueError(lines[-1] if lines else 'could not find the recipients')
    found = json.loads(run.stdout)
    to = [(str(n), str(a)) for n, a in found['to']]
    cc = [(str(n), str(a)) for n, a in found['cc'] if (n, a) not in found['to']]
    return to, cc
