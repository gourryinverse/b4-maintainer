# SPDX-License-Identifier: GPL-2.0-or-later
"""Workspace state synced through the private state repository.

A sync exports this machine's state into state/, merges it three-way with the
remote branch (against the last commit both share), imports what others
changed, and commits on top of the remote branch: history stays linear and
Git never sees a conflict. Exported records (b4 series, flags, checkouts)
missing here mean "not known here", never "deleted", so a fresh machine's
empty b4 removes nothing; files that live only in state/ (workspace.toml,
client/) merge with removals.
"""

import datetime
import fnmatch
import hashlib
import json
import os
import pathlib
import re
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional

Tree = dict[str, bytes]
# Exported paths that differ after a merge: path -> new content, or None when removed.
Changes = dict[str, Optional[bytes]]

FLAGS = 'b4/flags.tsv'
SERIES = 'projects/*/series/*.json'
CHECKOUT = 'projects/*/checkout.json'
EXPORTED = (SERIES, CHECKOUT, FLAGS)
HORIZON = datetime.timedelta(days=90)
# Statuses backed by a review branch: imported as new until branches are rebuilt
# (b4 marks branch-less ones gone), and never overwritten by such an import.
BRANCH_STATUSES = frozenset({'reviewing', 'replied', 'partial', 'waiting', 'snoozed'})


class SyncError(RuntimeError):
    pass


def is_exported(path: str) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in EXPORTED)


# Commit messages name no machine: hostnames are private.
SYNC_MESSAGE = 'b4-maintainer sync'

# Flags: one line per message: msgid, date, flags, and the tracked or watched
# message-id anchoring its thread (empty if none), so pruning is decided from
# synced data alone and every machine prunes alike.


@dataclass(frozen=True)
class FlagLine:
    msgid: str
    date: str
    flags: str
    anchor: str = ''


def parse_flags(data: bytes) -> list[FlagLine]:
    lines = []
    for line in data.decode(errors='replace').splitlines():
        fields = line.split('\t')
        if len(fields) >= 3 and fields[0]:
            lines.append(FlagLine(*fields[:3], fields[3] if len(fields) > 3 else ''))
    return lines


def flags_text(lines: Iterable[FlagLine]) -> bytes:
    rows = sorted(lines, key=lambda f: f.msgid)
    return ''.join(f'{f.msgid}\t{f.date}\t{f.flags}\t{f.anchor}\n' for f in rows).encode()


def _merge_flags(ours: Optional[bytes], theirs: Optional[bytes]) -> dict[str, FlagLine]:
    merged: dict[str, FlagLine] = {}
    for line in parse_flags(theirs or b'') + parse_flags(ours or b''):
        old = merged.get(line.msgid)
        if old is None:
            merged[line.msgid] = line
            continue
        flags = ' '.join(sorted(set(old.flags.split()) | set(line.flags.split())))
        merged[line.msgid] = FlagLine(
            line.msgid, line.date or old.date, flags, line.anchor or old.anchor
        )
    return merged


def _anchors(tree: Tree) -> set[str]:
    """Message-ids that keep old flags: tracked series and watched discussions."""
    found: set[str] = set()
    for path, data in tree.items():
        if fnmatch.fnmatchcase(path, SERIES):
            rec = _json(data)
            rows = rec.get('series', []) if isinstance(rec, dict) else []
            if rows and all(r.get('status') == 'archived' for r in rows):
                continue
            for row in rows:
                found.add(row.get('message_id', ''))
                found.update(p.get('message_id', '') for p in row.get('patches', []))
            found.update(r.get('message_id', '') for r in rec.get('revisions', []))
        elif path == 'client/watched.json':
            entries = _json(data)
            if isinstance(entries, list):
                found.update(e.get('msgid', '') for e in entries if isinstance(e, dict))
    return found - {''}


# Series records: b4's rows for one change-id, one file each.


def series_path(project: str, change_id: str) -> str:
    if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,150}', change_id):
        name = change_id
    else:
        name = hashlib.sha1(change_id.encode()).hexdigest()
    return f'projects/{project}/series/{name}.json'


def _json(data: Optional[bytes]) -> Any:
    try:
        return json.loads(data) if data else None
    except ValueError:
        return None


def _dump(data: Any) -> bytes:
    return (json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True) + '\n').encode()


def series_files(
    project: str, records: Mapping[str, dict[str, Any]], current: Tree, now: datetime.datetime
) -> Tree:
    """Each change's record file; `updated` moves only when the content changes.

    A row this machine has as `new` keeps a branch-backed status from the
    current record: it was imported as `new`, not changed here.
    """
    files: Tree = {}
    for change_id, rec in records.items():
        path = series_path(project, change_id)
        old = _json(current.get(path))
        old = old if isinstance(old, dict) else {}
        old_rows = {r.get('revision'): r for r in old.get('series', [])}
        rows = []
        for row in rec.get('series', []):
            prior = old_rows.get(row.get('revision'), {})
            if row.get('status') == 'new' and prior.get('status') in BRANCH_STATUSES:
                row = {**row, 'status': prior['status']}
            rows.append(row)
        rec = {**rec, 'series': rows}
        content = {k: v for k, v in rec.items() if k != 'updated'}
        if {k: v for k, v in old.items() if k != 'updated'} == content and old.get('updated'):
            files[path] = current[path]
        else:
            files[path] = _dump({**content, 'updated': now.isoformat()})
    return files


def _write(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.tmp')
    tmp.write_bytes(data)
    os.replace(tmp, path)


# Merging.


def _pick(base: Any, ours: Any, theirs: Any, conflict: Callable[[Any, Any], Any]) -> Any:
    if ours == theirs or theirs == base:
        return ours
    if ours == base:
        return theirs
    return conflict(ours, theirs)


def _newer(ours: bytes, theirs: bytes) -> bytes:
    def stamp(data: bytes) -> str:
        rec = _json(data)
        return str(rec.get('updated', '')) if isinstance(rec, dict) else ''

    return theirs if stamp(theirs) > stamp(ours) else ours


def _merge_entries(
    base: Optional[bytes], ours: Optional[bytes], theirs: Optional[bytes]
) -> Optional[bytes]:
    """Client entries, per identity; both sides changed one: ours."""

    def entries(data: Optional[bytes]) -> dict[str, Any]:
        found = _json(data)
        if not isinstance(found, list):
            return {}
        return {e['identity']: e for e in found if isinstance(e, dict) and 'identity' in e}

    b, o, t = entries(base), entries(ours), entries(theirs)
    result = []
    for key in dict.fromkeys([*o, *t]):
        entry = _pick(b.get(key), o.get(key), t.get(key), lambda mine, _: mine)
        if entry is not None:
            result.append(entry)
    result.sort(key=lambda e: (str(e.get('subject', '')), e['identity']))
    return (json.dumps(result, indent=1, ensure_ascii=False) + '\n').encode()


def _merge_text(
    path: str, base: Optional[bytes], ours: bytes, theirs: bytes, notes: list[str]
) -> bytes:
    with tempfile.TemporaryDirectory(prefix='b4m-merge-') as tmp:
        files = []
        for name, data in (('ours', ours), ('base', base or b''), ('theirs', theirs)):
            target = pathlib.Path(tmp) / name
            target.write_bytes(data)
            files.append(str(target))
        result = subprocess.run(
            ['git', 'merge-file', '-p', *files], capture_output=True, check=False
        )
    if result.returncode == 0:
        return result.stdout
    notes.append(f"{path}: changed here and on another machine; kept this machine's version")
    return ours


def merge_states(
    base: Tree, ours: Tree, theirs: Tree, now: datetime.datetime
) -> tuple[Tree, list[str]]:
    """Merge this machine's state with the remote's; notes report kept local versions."""
    merged: Tree = {}
    notes: list[str] = []
    for path in sorted(set(base) | set(ours) | set(theirs)):
        if path == FLAGS:
            continue
        b, o, t = base.get(path), ours.get(path), theirs.get(path)
        if is_exported(path) and o is None:
            value = t  # Not known here: no opinion.
        elif path.startswith('client/') and path.endswith('.json'):
            value = None if o is None and t is None else _merge_entries(b, o, t)
        elif fnmatch.fnmatchcase(path, SERIES):
            value = _pick(b, o, t, _newer)
        elif path == 'workspace.toml' and o is not None and t is not None:
            value = _pick(b, o, t, lambda mine, other: _merge_text(path, b, mine, other, notes))
        else:
            value = _pick(b, o, t, lambda mine, _: mine)
        if value is not None:
            merged[path] = value
    cutoff = (now - HORIZON).isoformat()
    for path in [p for p in merged if fnmatch.fnmatchcase(p, SERIES)]:
        rec = _json(merged[path])
        rows = rec.get('series', []) if isinstance(rec, dict) else []
        if rows and all(r.get('status') == 'archived' for r in rows):
            if str(rec.get('updated', '')) < cutoff:
                del merged[path]
    if FLAGS in ours or FLAGS in theirs:
        anchors = _anchors(merged)
        lines = [
            f
            for f in _merge_flags(ours.get(FLAGS), theirs.get(FLAGS)).values()
            if (f.date and f.date >= cutoff) or f.anchor in anchors
        ]
        merged[FLAGS] = flags_text(lines)
    return merged, notes


# The repository.


class StateRepo:
    """state/: a clone of the private state repository (or a local repository, unsynced)."""

    def __init__(self, path: pathlib.Path, remote: str = 'origin', timeout: float = 60) -> None:
        self.path = pathlib.Path(path)
        self.remote = remote
        self.timeout = timeout

    def exists(self) -> bool:
        return (self.path / '.git').exists()

    def _git(
        self, *args: str, input: Optional[bytes] = None, timeout: Optional[float] = None
    ) -> subprocess.CompletedProcess[bytes]:
        env = dict(os.environ)
        # Sync commits need an author; the maintainer's own when configured.
        if not self._identity_configured():
            env.update(
                GIT_AUTHOR_NAME='b4-maintainer',
                GIT_AUTHOR_EMAIL='b4-maintainer@localhost',
                GIT_COMMITTER_NAME='b4-maintainer',
                GIT_COMMITTER_EMAIL='b4-maintainer@localhost',
            )
        try:
            return subprocess.run(
                ['git', '-C', str(self.path), *args],
                input=input,
                capture_output=True,
                timeout=timeout or self.timeout,
                env=env,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as ex:
            raise SyncError(f'git {args[0]}: {ex}') from ex

    def _identity_configured(self) -> bool:
        if not hasattr(self, '_has_identity'):
            result = subprocess.run(
                ['git', '-C', str(self.path), 'config', 'user.email'],
                capture_output=True,
                check=False,
            )
            self._has_identity = bool(result.stdout.strip())
        return self._has_identity

    def _out(self, *args: str) -> Optional[str]:
        result = self._git(*args)
        return result.stdout.decode().strip() if result.returncode == 0 else None

    def _check(self, *args: str, input: Optional[bytes] = None) -> str:
        result = self._git(*args, input=input)
        if result.returncode != 0:
            raise SyncError(f'git {args[0]} failed: {result.stderr.decode().strip()}')
        return result.stdout.decode().strip()

    def branch(self) -> str:
        return self._out('symbolic-ref', '--short', 'HEAD') or 'main'

    def has_remote(self) -> bool:
        return self.remote in (self._out('remote') or '').split()

    def _rev(self, ref: str) -> Optional[str]:
        return self._out('rev-parse', '--verify', '-q', f'{ref}^{{commit}}')

    def _remote_ref(self) -> str:
        return f'refs/remotes/{self.remote}/{self.branch()}'

    def fetch(self, timeout: Optional[float] = None) -> None:
        branch = self.branch()
        result = self._git(
            'fetch',
            '-q',
            self.remote,
            f'+refs/heads/{branch}:refs/remotes/{self.remote}/{branch}',
            timeout=timeout,
        )
        if result.returncode != 0:
            err = result.stderr.decode()
            if "couldn't find remote ref" in err:
                return  # An empty remote: nothing to merge yet.
            raise SyncError(f'fetch failed: {err.strip()}')

    def tree(self, rev: Optional[str]) -> Tree:
        if not rev:
            return {}
        listing = self._check('ls-tree', '-r', '-z', rev)
        entries = [e.split('\t', 1) for e in listing.split('\0') if e]
        blobs = [(meta.split()[2], path) for meta, path in entries if meta.split()[1] == 'blob']
        if not blobs:
            return {}
        out = self._git('cat-file', '--batch', input=''.join(f'{s}\n' for s, _ in blobs).encode())
        data, files, pos = out.stdout, {}, 0
        for _, path in blobs:
            header_end = data.index(b'\n', pos)
            size = int(data[pos:header_end].split()[2])
            files[path] = data[header_end + 1 : header_end + 1 + size]
            pos = header_end + 1 + size + 1
        return files

    def working(self) -> Tree:
        files: Tree = {}
        for path in self.path.rglob('*'):
            rel = path.relative_to(self.path)
            if rel.parts[0] == '.git' or not path.is_file():
                continue
            if any(part.startswith('.') for part in rel.parts):
                continue  # Temporary files and editor leftovers.
            files[rel.as_posix()] = path.read_bytes()
        return files

    def _write_tree(self, current: Tree, merged: Tree) -> None:
        for path, data in merged.items():
            if current.get(path) != data:
                _write(self.path / path, data)
        for path in set(current) - set(merged):
            target = self.path / path
            target.unlink(missing_ok=True)
            for parent in target.parents:
                if parent == self.path or any(parent.iterdir()):
                    break
                parent.rmdir()

    def _commit(self, parent: Optional[str], message: str) -> None:
        self._check('add', '-A', '.')
        tree = self._check('write-tree')
        if parent and self._out('rev-parse', f'{parent}^{{tree}}') == tree:
            self._check('update-ref', '-m', 'b4-maintainer sync', 'HEAD', parent)
            return
        args = ['commit-tree', tree, '-m', message]
        if parent:
            args += ['-p', parent]
        self._check('update-ref', '-m', 'b4-maintainer sync', 'HEAD', self._check(*args))

    def sync(
        self,
        export: Callable[[Tree], Tree],
        import_: Callable[[Changes], None],
        now: Optional[datetime.datetime] = None,
        timeout: Optional[float] = None,
    ) -> list[str]:
        """Export, merge with the remote, import, commit, push; returns merge notes.

        *export* gets the current files and returns every exported file this
        machine has (what its b4 and checkouts hold); other exported files
        here are only what earlier syncs wrote.
        """
        now = now or datetime.datetime.now(datetime.timezone.utc)
        current = self.working()
        ours = {p: d for p, d in current.items() if not is_exported(p)}
        ours.update(export(current))
        # This machine's export, committed before any network step: kept if that fails.
        self._write_tree(current, {**{p: d for p, d in current.items() if is_exported(p)}, **ours})
        self._commit(self._rev('HEAD'), SYNC_MESSAGE)
        remote = self.has_remote()
        notes: list[str] = []
        for _ in range(2):
            if remote:
                self.fetch(timeout)
            theirs_rev = self._rev(self._remote_ref()) if remote else None
            head = self._rev('HEAD')
            base_rev = head
            if head and theirs_rev:
                base_rev = self._out('merge-base', head, theirs_rev) or None
            theirs = self.tree(theirs_rev) if theirs_rev else self.tree(head)
            merged, notes = merge_states(self.tree(base_rev), ours, theirs, now)
            self._write_tree(self.working(), merged)
            changes: Changes = {
                p: merged.get(p)
                for p in set(ours) | set(merged)
                if is_exported(p) and ours.get(p) != merged.get(p)
            }
            if changes:
                import_(changes)
            ours = merged
            self._commit(theirs_rev or head, SYNC_MESSAGE)
            if not remote:
                return notes
            if self._rev('HEAD') == theirs_rev:
                return notes  # Nothing new to publish.
            result = self._git(
                'push', '-q', self.remote, f'HEAD:refs/heads/{self.branch()}', timeout=timeout
            )
            if result.returncode == 0:
                self._git('update-ref', self._remote_ref(), 'HEAD')
                return notes
            err = result.stderr.decode()
            if 'rejected' not in err and 'fetch first' not in err:
                raise SyncError(f'push failed: {err.strip()}')
        raise SyncError('push rejected twice: the remote kept changing')


def known_anchors(thread_of: Mapping[str, str], anchors: Iterable[str]) -> dict[str, str]:
    """msgid -> the anchor message-id in its cached thread, given msgid -> thread key."""
    by_thread = {thread_of[a]: a for a in anchors if a in thread_of}
    return {m: by_thread[key] for m, key in thread_of.items() if key in by_thread}
