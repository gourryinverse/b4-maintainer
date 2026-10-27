"""Contract: task profiles are data; the dialog's choices compose a session's prompts."""

import pytest
from sessiondata import PROFILE

from b4_maintainer.sessions.profiles import (
    ProfileError,
    compose,
    load_profiles,
    parse_profile,
    suggested_skills,
)


def write(directory, name, text):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(text)


def test_choices_compose_the_prompts_in_field_order():
    profile = parse_profile(PROFILE)
    request = compose(profile, {'goal': 'review', 'artifacts': ['draft']})
    assert request.prompt == (
        'Help me review this series.\n\nWrite an inline review reply to out/draft/reply.txt.'
    )
    assert request.system_prompt == 'You assist a Linux kernel maintainer.'
    assert request.skills == ('kernel',)
    assert request.mode == 'lightweight' and request.notes == ()


def test_custom_text_adds_to_the_picked_choices():
    profile = parse_profile(PROFILE)
    request = compose(
        profile,
        {'goal': 'review', 'artifacts': ['draft']},
        text={'goal': 'find the leak', 'artifacts': 'a timeline'},
    )
    assert request.prompt == (
        'Help me review this series.\n\nGoal: find the leak\n\n'
        'Write an inline review reply to out/draft/reply.txt.\n\nAlso produce: a timeline'
    )


def test_custom_text_alone_answers_a_required_field():
    profile = parse_profile(PROFILE)
    request = compose(profile, {}, text={'goal': 'find the leak'})
    assert request.prompt == 'Goal: find the leak'


def test_multi_choice_goals_compose_each():
    profile = parse_profile(PROFILE.replace('kind = "choice"', 'kind = "multi"'))
    request = compose(profile, {'goal': ['review', 'reproduce']})
    assert request.prompt.startswith('Help me review this series.\n\nReproduce the problem')
    assert request.mode == 'worktree'


def test_a_choice_needing_a_tree_switches_to_worktree_with_a_note():
    profile = parse_profile(PROFILE)
    request = compose(profile, {'goal': 'review', 'artifacts': ['selftest']})
    assert request.mode == 'worktree'
    assert request.notes and 'Selftest' in request.notes[0]
    kept = compose(profile, {'goal': 'review', 'artifacts': ['selftest']}, mode='lightweight')
    assert kept.mode == 'lightweight'  # The maintainer may switch back.


def test_profile_and_choice_skills_are_suggested_in_order():
    profile = parse_profile('skills = ["style"]\n' + PROFILE)
    picked = {'goal': 'reproduce', 'artifacts': ['draft']}
    assert compose(profile, picked).skills == ('style', 'vng')
    assert suggested_skills(profile, {'goal': 'reproduce', 'artifacts': ['nope']}) == (
        'style',
        'vng',
    )


def test_required_fields_and_unknown_choices_are_refused():
    profile = parse_profile(PROFILE)
    with pytest.raises(ProfileError, match='Goal'):
        compose(profile, {})
    with pytest.raises(ProfileError, match='nope'):
        compose(profile, {'goal': 'nope'})
    with pytest.raises(ProfileError, match='Goal'):
        compose(profile, {}, text={'goal': '   '})  # Blank custom text is no answer.


@pytest.mark.parametrize(
    'text, message',
    [
        (
            'id = "x"\nversion = 1\nlabel = "X"\n[[fields]]\nid = "g"\nlabel = "G"\nkind = "dial"',
            'kind',
        ),
        ('version = 1\nlabel = "X"', 'id'),
        (
            'id = "x"\nversion = 1\nlabel = "X"\n[[fields]]\nid = "g"\nlabel = "G"\nkind = "choice"',
            'needs choices',
        ),
        (
            'id = "x"\nversion = 1\nlabel = "X"\n'
            '[[fields]]\nid = "g"\nlabel = "G"\nkind = "text"\n'
            '[[fields]]\nid = "g"\nlabel = "H"\nkind = "text"',
            'duplicate',
        ),
        ('not toml = = 1', 'TOML'),
        (
            'id = "x"\nversion = 1\nlabel = "X"\n[[fields]]\nid = "g"\nlabel = "G"\n'
            'kind = "multi"\ncustom = "no placeholder"\n'
            '[[fields.choices]]\nid = "a"\nlabel = "A"',
            '{text}',
        ),
        ('id = "x"\nversion = 1\nlabel = "X"\nskills = "vng"', 'skills'),
        (
            'id = "x"\nversion = 1\nlabel = "X"\n[[fields]]\nid = "g"\nlabel = "G"\n'
            'kind = "choice"\n[[fields.choices]]\nid = "a"\nlabel = "A"\nskills = [1]',
            'skills',
        ),
    ],
)
def test_invalid_profiles_are_reported(text, message):
    with pytest.raises(ProfileError, match=message):
        parse_profile(text)


def test_workspace_profiles_add_to_and_replace_built_ins(tmp_path):
    builtin, mine = tmp_path / 'builtin', tmp_path / 'state' / 'profiles'
    write(builtin, 'series.toml', PROFILE)
    write(builtin, 'other.toml', PROFILE.replace('series-test', 'other'))
    write(mine, 'series.toml', PROFILE.replace('label = "Series session"', 'label = "Mine"'))
    write(mine, 'broken.toml', 'id = ')
    profiles, problems = load_profiles(builtin, mine)
    assert {p.id: p.label for p in profiles} == {'series-test': 'Mine', 'other': 'Series session'}
    assert len(problems) == 1 and 'broken.toml' in problems[0]


def test_profiles_record_a_content_hash():
    one, two = parse_profile(PROFILE), parse_profile(PROFILE + '\n# comment\n')
    assert one.hash and one.hash != two.hash
    assert parse_profile(PROFILE).hash == one.hash


def test_the_built_in_profiles_load():
    from b4_maintainer.sessions.profiles import BUILTIN_DIR

    profiles, problems = load_profiles(BUILTIN_DIR, None)
    assert problems == []
    series = next(p for p in profiles if p.scope == 'series')
    goal = next(f for f in series.fields if f.id == 'goal')
    assert goal.kind == 'multi' and goal.custom
    assert {c.id for c in goal.choices} >= {'review', 'bugs', 'reproduce', 'performance'}


def test_the_series_profile_brings_in_guidance_by_choice():
    from b4_maintainer.sessions.profiles import BUILTIN_DIR, suggested_skills

    series = parse_profile((BUILTIN_DIR / 'series.toml').read_text())
    assert suggested_skills(series, {'goal': ['review']}) == ('kernel',)
    assert suggested_skills(series, {'goal': ['bugs']}) == ('kernel',)
    assert suggested_skills(series, {'goal': ['reproduce']}) == ('kernel',)
    assert set(suggested_skills(series, {'artifacts': ['draft']})) == {'kernel', 'kernel-style'}
    worktree = suggested_skills(series, {'goal': ['review']}, 'worktree')
    assert {'kernel', 'kernel-style', 'b4m-branches'} <= set(worktree)
    review = compose(series, {'goal': ['review']})
    assert 'review-core.md' in review.prompt
    assert 'debugging.md' in compose(series, {'goal': ['reproduce']}).prompt
    assert (
        'inline-template.md' in compose(series, {'goal': ['review'], 'artifacts': ['draft']}).prompt
    )


def test_a_command_field_goes_first_in_the_prompt():
    from b4_maintainer.sessions.profiles import BUILTIN_DIR

    series = parse_profile((BUILTIN_DIR / 'series.toml').read_text())
    command = next(f for f in series.fields if f.id == 'command')
    assert command.kind == 'text' and command.prefix
    with_goal = compose(series, {'goal': ['review'], 'command': '/goal'})
    assert with_goal.prompt.startswith('/goal Help me review this series')
    plain = compose(series, {'goal': ['review']})
    assert plain.prompt.startswith('Help me review this series')
    workspace = parse_profile((BUILTIN_DIR / 'workspace.toml').read_text())
    assert any(f.id == 'command' and f.prefix for f in workspace.fields)


def test_each_kind_of_test_is_asked_for_by_what_it_is():
    """Reproducer: out of tree, shows a bug. Selftest: in tree, needs a running kernel.
    KUnit: in tree, no running kernel."""
    from b4_maintainer.sessions.profiles import BUILTIN_DIR

    series = parse_profile((BUILTIN_DIR / 'series.toml').read_text())

    def prompt(choice):
        return compose(series, {'goal': ['review'], 'artifacts': [choice]}).prompt

    reproducer = prompt('reproducers')
    assert 'never sent upstream' in reproducer and 'out/reproducers/' in reproducer
    assert 'kind = "reproducer"' in reproducer and 'b4m/tests/' not in reproducer
    selftest = prompt('selftest')
    assert 'tools/testing/selftests/' in selftest and 'running kernel' in selftest
    assert 'b4m/tests/' in selftest and 'kind = "test"' in selftest
    assert 'out/selftest' not in selftest
    assert 'vng' in suggested_skills(series, {'artifacts': ['selftest']})
    kunit = prompt('unittests')
    assert 'KUnit' in kunit and 'kunit.py run' in kunit and 'without' in kunit
    assert 'b4m/tests/' in kunit and 'kind = "test"' in kunit and 'out/kunit' not in kunit
    for choice in ('reproducers', 'selftest', 'unittests'):
        assert (
            next(c for f in series.fields for c in f.choices if c.id == choice).needs == 'worktree'
        )
    skill = (BUILTIN_DIR.parent / 'skills' / 'b4m-branches' / 'SKILL.md').read_text()
    assert '## Which kind of test' in skill
