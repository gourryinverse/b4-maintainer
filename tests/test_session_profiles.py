"""Contract: task profiles are data; the dialog's choices compose a session's prompts."""

import pytest
from sessiondata import PROFILE

from b4_maintainer.sessions.profiles import ProfileError, compose, load_profiles, parse_profile


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
    assert request.files == ('review-prompts:agents/review.md',)
    assert request.mode == 'lightweight' and request.notes == ()


def test_free_text_choices_fill_their_prompt():
    profile = parse_profile(PROFILE)
    request = compose(
        profile,
        {'goal': 'custom', 'artifacts': ['custom']},
        text={'goal': 'find the leak', 'artifacts': 'a timeline'},
    )
    assert request.prompt == 'Goal: find the leak\n\nAlso produce: a timeline'


def test_a_choice_needing_a_tree_switches_to_worktree_with_a_note():
    profile = parse_profile(PROFILE)
    request = compose(profile, {'goal': 'review', 'artifacts': ['selftest']})
    assert request.mode == 'worktree'
    assert request.notes and 'Selftest' in request.notes[0]
    kept = compose(profile, {'goal': 'review', 'artifacts': ['selftest']}, mode='lightweight')
    assert kept.mode == 'lightweight'  # The maintainer may switch back.


def test_required_fields_and_unknown_choices_are_refused():
    profile = parse_profile(PROFILE)
    with pytest.raises(ProfileError, match='Goal'):
        compose(profile, {})
    with pytest.raises(ProfileError, match='nope'):
        compose(profile, {'goal': 'nope'})
    with pytest.raises(ProfileError, match='text'):
        compose(profile, {'goal': 'custom'})


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
        ('id = "x"\nversion = 1\nlabel = "X"\npermissions = "root"', 'permissions'),
        ('not toml = = 1', 'TOML'),
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
    assert {c.id for c in goal.choices} >= {'review', 'bugs', 'reproduce', 'performance', 'custom'}
