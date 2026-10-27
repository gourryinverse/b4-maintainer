"""Contract: guidance repositories (review-prompts, kernel-style) reach sessions as pi
skills, from the pinned deps/ checkouts or the maintainer's own, each recorded by commit."""

import pathlib

from gitutil import git

from b4_maintainer.sessions.guidance import DEFAULTS, guidance_skills, provenance, sources

TEMPLATE = """---
name: kernel
description: Kernel review protocols.
---
Load `{{KERNEL_REVIEW_PROMPTS_DIR}}/review-core.md` first.
"""
STYLE = """---
name: kernel-style
description: Use when planning, writing, or reviewing kernel patches.
---
The repository root is two levels up.
"""


def repo(path: pathlib.Path, files: dict[str, str]) -> pathlib.Path:
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    git('init', '-q', '-b', 'main', cwd=path)
    git('add', '-A', cwd=path)
    git('-c', 'user.name=E', '-c', 'user.email=e@example.org', 'commit', '-q', '-m', 'x', cwd=path)
    return path


def test_the_pinned_checkouts_are_the_defaults_and_config_overrides_them(tmp_path):
    found = sources({}, tmp_path)
    assert found == DEFAULTS
    assert DEFAULTS['review-prompts'].name == 'review-prompts'
    assert DEFAULTS['kernel-style'].parent.name == 'deps'
    mine = sources({'review-prompts': '~/git/review-prompts', 'kernel-style': 'style'}, tmp_path)
    assert mine['review-prompts'] == pathlib.Path('~/git/review-prompts').expanduser()
    assert mine['kernel-style'] == tmp_path / 'style'  # Relative to the workspace.


def test_each_source_becomes_a_skill_with_its_checkout_readable(tmp_path):
    prompts = repo(
        tmp_path / 'rp', {'kernel/skills/kernel.md': TEMPLATE, 'kernel/review-core.md': 'x'}
    )
    style = repo(tmp_path / 'ks', {'skills/kernel-style/SKILL.md': STYLE, 'README.md': 'x'})
    skills, problems = guidance_skills(
        {'review-prompts': prompts, 'kernel-style': style}, tmp_path / 'cache'
    )
    assert problems == []
    by_id = {s.id: s for s in skills}
    kernel, kstyle = by_id['kernel'], by_id['kernel-style']
    # review-prompts' template, its prompts directory filled in, generated outside the repo.
    text = (kernel.path / 'SKILL.md').read_text()
    assert f'`{prompts / "kernel"}/review-core.md`' in text
    assert kernel.path.is_relative_to(tmp_path / 'cache')
    assert (kernel.root, kernel.source) == (prompts / 'kernel', 'review-prompts')
    # kernel-style's own skill, its whole repository readable (the skill reads from there).
    assert kstyle.path == style / 'skills' / 'kernel-style'
    assert (kstyle.root, kstyle.source) == (style, 'kernel-style')
    assert kernel.version == git('rev-parse', '--short=12', 'HEAD', cwd=prompts)


def test_a_missing_source_is_a_problem_not_an_error(tmp_path):
    skills, problems = guidance_skills({'review-prompts': tmp_path / 'nope'}, tmp_path / 'cache')
    assert skills == [] and len(problems) == 1 and 'review-prompts' in problems[0]


def test_provenance_names_each_source_used_and_its_commit(tmp_path):
    style = repo(tmp_path / 'ks', {'skills/kernel-style/SKILL.md': STYLE})
    skills, _ = guidance_skills({'kernel-style': style}, tmp_path / 'cache')
    head = git('rev-parse', 'HEAD', cwd=style)
    assert provenance(skills, ['kernel-style', 'vng']) == {'kernel-style': head}
    (style / 'README.md').write_text('edited\n')
    assert provenance(skills, ['kernel-style']) == {'kernel-style': f'{head}-dirty'}
