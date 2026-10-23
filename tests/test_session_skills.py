"""Contract: skills are SKILL.md directories, built in or the maintainer's, chosen per session."""

import pytest

from b4_maintainer.sessions.skills import BUILTIN_DIR, SkillError, load_skills, parse_skill


def write(root, name, text):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / 'SKILL.md').write_text(text)
    return directory


def skill_md(name, description='Does one thing.', body='Steps.\n'):
    return f'---\nname: {name}\ndescription: {description}\n---\n\n{body}'


def test_a_skill_is_its_directory_with_frontmatter(tmp_path):
    directory = write(tmp_path, 'kselftest', skill_md('kselftest', 'Run kselftests.'))
    skill = parse_skill(directory)
    assert (skill.id, skill.description, skill.path) == ('kselftest', 'Run kselftests.', directory)


@pytest.mark.parametrize(
    'name, text, problem',
    [
        ('a', 'no frontmatter\n', 'frontmatter'),
        ('a', '---\nname: a\n', 'frontmatter'),
        ('a', '---\nname: b\ndescription: x\n---\n', 'match its directory'),
        ('a', '---\nname: a\n---\n', 'description'),
        ('Bad_Name', skill_md('Bad_Name'), 'lowercase'),
    ],
)
def test_malformed_skills_are_refused(tmp_path, name, text, problem):
    with pytest.raises(SkillError, match=problem):
        parse_skill(write(tmp_path, name, text))


def test_maintainer_skills_replace_built_ins_and_problems_are_reported(tmp_path):
    builtin, mine = tmp_path / 'builtin', tmp_path / 'mine'
    write(builtin, 'vng', skill_md('vng', 'Built in.'))
    write(builtin, 'other', skill_md('other'))
    write(mine, 'vng', skill_md('vng', 'Mine.'))
    write(mine, 'broken', 'no frontmatter\n')
    (mine / 'not-a-skill').mkdir()
    skills, problems = load_skills(builtin, mine)
    assert {s.id: s.description for s in skills} == {'vng': 'Mine.', 'other': 'Does one thing.'}
    assert [p.split(':')[0] for p in problems] == ['broken']


def test_built_in_skills_load():
    skills, problems = load_skills(BUILTIN_DIR, None)
    assert problems == []
    assert 'vng' in {s.id for s in skills}
