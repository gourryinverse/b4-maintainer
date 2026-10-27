"""Contract: a session's artifact manifest is read strictly; a bad entry is reported with
its reason and the rest still load."""

from b4_maintainer.sessions.artifacts import KINDS, load_artifacts

GOOD = '''
[[artifact]]
kind = "pull-ahead"
title = "mm/foo: fix refcount leak on error"
summary = """The error path leaks a reference."""
branch = "b4m/pull-ahead/foo-refcount"
answers = "sashiko:p2/1"
tested = "built; selftests/mm/foo pass"

[[artifact]]
kind = "results"
title = "selftests on v3"
summary = "All pass."
path = "out/tests/mm.log"
'''


def write(tmp_path, text):
    out = tmp_path / 'out'
    (out / 'tests').mkdir(parents=True)
    (out / 'tests' / 'mm.log').write_text('ok\n')
    (out / 'artifacts.toml').write_text(text)
    return out


def test_a_valid_manifest_loads(tmp_path):
    out = write(tmp_path, GOOD)
    found, problems = load_artifacts(out, branches={'b4m/pull-ahead/foo-refcount'})
    assert problems == []
    first, second = found
    assert (first.kind, first.branch, first.answers) == (
        'pull-ahead',
        'b4m/pull-ahead/foo-refcount',
        'sashiko:p2/1',
    )
    assert first.summary == 'The error path leaks a reference.'
    assert (second.kind, second.path) == ('results', out / 'tests' / 'mm.log')
    assert set(KINDS) == {
        'finding',
        'pull-ahead',
        'fixup',
        'test',
        'results',
        'reproducer',
        'analysis',
        'diff',
    }


def test_bad_entries_are_reported_and_the_rest_load(tmp_path):
    bad = (
        GOOD
        + """
[[artifact]]
kind = "patchbomb"
title = "x"
summary = "x"

[[artifact]]
kind = "fixup"
title = "no branch"
summary = "x"

[[artifact]]
kind = "test"
title = "missing branch"
summary = "x"
branch = "b4m/tests/gone"

[[artifact]]
kind = "reproducer"
title = "escapes out/"
summary = "x"
path = "../../etc/passwd"

[[artifact]]
kind = "analysis"
title = "extra key"
summary = "x"
colour = "red"

[[artifact]]
kind = "analysis"
summary = "no title"
"""
    )
    out = write(tmp_path, bad)
    found, problems = load_artifacts(out, branches={'b4m/pull-ahead/foo-refcount'})
    assert [a.title for a in found] == ['mm/foo: fix refcount leak on error', 'selftests on v3']
    assert len(problems) == 6
    text = '\n'.join(problems)
    for reason in (
        "unknown kind 'patchbomb'",
        'a fixup needs a branch',
        'no branch b4m/tests/gone',
        'outside out/',
        "unknown key 'colour'",
        'title is required',
    ):
        assert reason in text, reason


def test_no_manifest_is_no_artifacts_and_a_broken_one_says_why(tmp_path):
    out = tmp_path / 'out'
    out.mkdir()
    assert load_artifacts(out, set()) == ([], [])
    (out / 'artifacts.toml').write_text('[[artifact]\nkind = ')
    found, problems = load_artifacts(out, set())
    assert found == [] and len(problems) == 1 and 'artifacts.toml' in problems[0]


def test_a_proposed_change_without_a_tree_is_a_diff_file(tmp_path):
    out = tmp_path / 'work' / 'out'
    (out / 'diffs').mkdir(parents=True)
    (out / 'diffs' / 'fix.diff').write_text('--- a/mm/x.c\n+++ b/mm/x.c\n')
    (out / 'artifacts.toml').write_text(
        '[[artifact]]\nkind = "diff"\ntitle = "mm: take the second approach"\n'
        'summary = "Patch 2 of series B, rebased onto series A."\npath = "out/diffs/fix.diff"\n\n'
        '[[artifact]]\nkind = "diff"\ntitle = "no file"\nsummary = "x"\n'
    )
    artifacts, problems = load_artifacts(out, set())
    assert [(a.kind, a.path) for a in artifacts] == [('diff', out / 'diffs' / 'fix.diff')]
    assert len(problems) == 1 and 'a diff needs a path' in problems[0]


def test_every_session_learns_to_record_artifacts():
    from b4_maintainer.sessions.profiles import BUILTIN_DIR, parse_profile, suggested_skills
    from b4_maintainer.sessions.skills import BUILTIN_DIR as SKILLS

    for name in ('series.toml', 'workspace.toml'):
        profile = parse_profile((BUILTIN_DIR / name).read_text())
        assert 'b4m-artifacts' in suggested_skills(profile, {}, 'lightweight')
    text = (SKILLS / 'b4m-artifacts' / 'SKILL.md').read_text()
    assert 'out/artifacts.toml' in text and '`diff`' in text and 'analysis' in text
    branches = (SKILLS / 'b4m-branches' / 'SKILL.md').read_text()
    assert 'b4m-artifacts' in branches  # The branches skill points at it for the manifest.
