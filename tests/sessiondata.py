"""Synthetic data for agent-session tests: profiles, backend entries, hook payloads."""

import json

PROFILE = """
id = "series-test"
version = 1
label = "Series session"
scope = "series"
system_prompt = "You assist a Linux kernel maintainer."
permissions = "read-only"

[[fields]]
id = "goal"
label = "Goal"
kind = "choice"
required = true

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

[[fields.choices]]
id = "custom"
label = "Custom"
text = true
prompt = "Goal: {text}"

[[fields]]
id = "artifacts"
label = "Artifacts"
kind = "multi"

[[fields.choices]]
id = "draft"
label = "Inline email draft"
prompt = "Write an inline review reply to out/draft/reply.txt."

[[fields.choices]]
id = "selftest"
label = "Selftest"
prompt = "Write a kselftest under out/selftest/."
needs = "worktree"

[[fields.choices]]
id = "custom"
label = "Custom artifact"
text = true
prompt = "Also produce: {text}"
"""


def payload(event, **fields):
    """A hook payload as an agent CLI passes it on stdin (synthetic)."""
    return json.dumps(
        {'session_id': '00000000-0000-4000-8000-000000000001', 'hook_event_name': event, **fields}
    )


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
        backend='claude',
        adapter='claude',
        command=('claude',),
        native_id='00000000-0000-4000-8000-000000000001',
        model='',
        permissions='read-only',
    )
    return base.replace(**changes)


class FakeSessionRunner:
    """Records launches, stops, resumes, switches, and ends; emits hook events on demand."""

    def __init__(self, store):
        self.store = store
        self.launches = []
        self.stops = []
        self.resumes = []
        self.switches = []
        self.ends = []
        self.windows = set()

    def add(self, record, alive=True):
        """A session as if launched earlier (not counted as a launch)."""
        self.store.create(record)
        if alive:
            self.windows.add(record.id)
        return record

    def emit(self, sid, event, time=NOW, **fields):
        from b4_maintainer.sessions.records import event_from_payload

        self.store.append(sid, event_from_payload(payload(event, **fields), time))

    def launch(self, record, request):
        self.launches.append((record.id, request))
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
