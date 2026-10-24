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
files = ["review-prompts:agents/review.md"]

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

    def resume(self, record):
        self.resumes.append(record.id)
        self.windows.add(record.id)

    def end(self, record):
        self.ends.append(record.id)
        self.windows.discard(record.id)


class FakeWorktrees:
    """Session worktrees with scripted sizes."""

    def __init__(self, *infos):
        self.infos = list(infos)

    def list(self, records):
        return list(self.infos)
