"""Synthetic data for agent-session tests: profiles, backend entries, hook payloads."""

import json

PROFILE = """
id = "series-test"
version = 1
label = "Series session"
scope = "series"
system_prompt = "You assist a Linux kernel maintainer."

[[fields]]
id = "goal"
label = "Goal"
kind = "choice"
required = true
custom = "Goal: {text}"
custom_label = "Custom goal"

[[fields.choices]]
id = "review"
label = "Help me review"
prompt = "Help me review this series."
skills = ["kernel"]

[[fields.choices]]
id = "reproduce"
label = "Reproduce and validate"
prompt = "Reproduce the problem the series fixes and validate the fix."
needs = "worktree"
skills = ["vng"]

[[fields]]
id = "artifacts"
label = "Artifacts"
kind = "multi"
custom = "Also produce: {text}"

[[fields.choices]]
id = "draft"
label = "Inline email draft"
prompt = "Write an inline review reply to out/draft/reply.txt."

[[fields.choices]]
id = "selftest"
label = "Selftest"
prompt = "Write a kselftest under out/selftest/."
needs = "worktree"
"""


def payload(event, **fields):
    """An event as the b4-maintainer pi extension passes it to the hook command (synthetic)."""
    return json.dumps({'event': event, **fields})


NOW = '2026-10-23T12:00:00+00:00'


def make_record(sid='s1', **changes):
    """A series session record (synthetic series and identities)."""
    from b4_maintainer.sessions.records import SessionRecord

    base = SessionRecord(
        id=sid,
        created=NOW,
        scope='series',
        project='linux',
        series='change-id:20261020-mm-foo',
        revision=3,
        subject='mm: fix foo leak',
        profile_id='series',
        profile_version=1,
        profile_hash='abc',
        choices={'goal': 'review', 'artifacts': ['draft']},
        mode='lightweight',
        harness='pi',
        preset='default',
        command=('pi',),
        native_id=sid,
        model='',
        thinking='',
        permissions='read-only',
    )
    return base.replace(**changes)


class FakeSessionRunner:
    """Records launches, stops, resumes, switches, and ends; emits hook events on demand."""

    def __init__(self, store):
        self.store = store
        self.launches = []  # (session id, request)
        self.specs = []
        self.stops = []
        self.resumes = []
        self.switches = []
        self.ends = []
        self.windows = set()
        self.fail = None  # Message for launch to fail with.
        self.blocker_reason = None
        self.beside = None  # The session joined beside the app.
        self.joins = []  # (session id, width)
        self.above = []  # The pane each join went above ('' beside the app).
        self.releases = 0
        self.focused = None  # 'app' or a session id.
        self.zoomed = False

    def add(self, record, alive=True):
        """A session as if launched earlier (not counted as a launch)."""
        self.store.create(record)
        if alive:
            self.windows.add(record.id)
        return record

    def emit(self, sid, event, time=NOW, **fields):
        from b4_maintainer.sessions.records import event_from_payload

        self.store.append(sid, event_from_payload(payload(event, **fields), time))

    def blocker(self):
        return self.blocker_reason

    def launch(self, record, spec):
        if self.fail:
            raise RuntimeError(self.fail)
        self.launches.append((record.id, spec.request))
        self.specs.append(spec)
        self.windows.add(record.id)

    def alive(self, records):
        return {r.id for r in records if r.id in self.windows}

    def switch(self, record):
        self.switches.append(record.id)

    def stop(self, record):
        self.stops.append(record.id)
        self.windows.discard(record.id)
        if self.beside == record.id:
            self.beside = None

    def resume(self, record):
        self.resumes.append(record.id)
        self.windows.add(record.id)

    def end(self, record):
        self.ends.append(record.id)
        self.windows.discard(record.id)
        if self.beside == record.id:
            self.beside = None

    def joined(self, app_pane):
        return self.beside

    def join(self, record, app_pane, width=90, above=''):
        self.joins.append((record.id, width))
        self.above.append(above)
        self.beside = record.id

    def release(self, app_pane):
        if self.beside is not None:
            self.releases += 1
            self.beside = None

    def focus(self, record, zoom=False):
        self.focused, self.zoomed = record.id, zoom

    def focus_app(self, app_pane):
        self.focused, self.zoomed = 'app', False

    def announce(self, app_pane):
        self.announced = app_pane

    def pane_of(self, record):
        return f'%{record.id}' if record.id in self.windows else None


class FakeWorktrees:
    """Session worktrees with scripted sizes; prepares by recording the request."""

    def __init__(self, *infos):
        self.infos = list(infos)
        self.prepared = []  # (session id, source, base)
        self.fail = None  # (message, output) for prepare to fail with.
        self.why = ''  # Why trees cannot be prepared.
        self.states = {}  # Path -> TreeState, as inspect finds it.
        self.removed = []
        self.during = None  # Called with the record while preparing.
        self.rebases = []  # (session id, base)
        self.starts = []  # (session id, start, the session whose tree holds it)
        self.also = []  # Further series per prepared tree.

    def list(self, records):
        return list(self.infos)

    def reason(self, project):
        return self.why

    def prepare_start(self, record, start, start_from):
        from b4_maintainer.sessions.records import Worktree

        self.starts.append((record.id, start, start_from))
        path = f'/w/var/worktrees/{record.id}'
        return Worktree(path, f'b4m/session/{record.id}', start, start)

    def prepare_rebase(self, record, base):
        from b4_maintainer.sessions.hub import TreeError
        from b4_maintainer.sessions.records import Worktree

        if self.fail is not None:
            raise TreeError(*self.fail)
        self.rebases.append((record.id, base))
        path = f'/w/var/worktrees/{record.id}'
        return Worktree(path, f'b4m/session/{record.id}', 'fork', 'tip')

    def bases(self, project):
        return ['HEAD', 'mm/mm-unstable'] if project else []

    def inspect(self, path, tree):
        from b4_maintainer.sessions.trees import TreeState

        return self.states.get(path, TreeState())

    def remove(self, path):
        self.removed.append(path)
        self.infos = [w for w in self.infos if w.path != path]

    def prepare(self, record, source, base, also=()):
        from b4_maintainer.sessions.hub import TreeError
        from b4_maintainer.sessions.records import Worktree

        if self.during is not None:
            self.during(record)  # While the tree is being prepared.
        if self.fail is not None:
            raise TreeError(*self.fail)
        self.prepared.append((record.id, source, base))
        if also:
            self.also.append(tuple(also))
        return Worktree(f'/w/var/worktrees/{record.id}', f'b4m/session/{record.id}', 'b', 'a')
