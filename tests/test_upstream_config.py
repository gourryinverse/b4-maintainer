"""Contract: Settings writes a project's tracked branches into workspace.toml in place:
only that project's refs = [...] is rewritten (one ref per line); comments, other keys,
and every other table stay byte for byte. The file holding the project's refs is the
one written (this machine's var/workspace.toml when it sets them). A result that would
not read back as the refs asked for is never written."""

import pytest

from b4_maintainer.model import Authority
from b4_maintainer.upstreams import ref_toml, refs_file, write_refs
from b4_maintainer.workspace import Workspace, make_ref

CONFIG = """\
# My workspace.
me = ["me@example.org"]

[[projects]]
id = "linux"
path = "projects/linux"  # The kernel.
lists = ["linux-mm@kvack.org"]
refs = [
  { label = "m", name = "mainline/master", authority = "mainline" },
  { label = "n", name = "next/master", authority = "acceptance", ends_review = true },
]
bases = [{ branch = "me/base", upstream = "mm/mm-stable" }]

[[profiles]]
id = "mm"
project = "linux"
lists = ["linux-mm@kvack.org"]

[[projects]]
id = "ndctl"
path = "projects/ndctl"
lists = ["nvdimm@lists.linux.dev"]
"""

MAIN = make_ref('m', 'mainline/master', Authority.MAINLINE)
NEXT = make_ref('n', 'next/master', Authority.ACCEPTANCE, ends_review=True)
CORE = make_ref('c', 'mm-core/for-next', Authority.ACCEPTANCE, 'MM-core')


@pytest.fixture
def root(tmp_path):
    (tmp_path / 'workspace.toml').write_text(CONFIG)
    return tmp_path


def refs_of(root, project):
    from b4_maintainer.projectsetup import read_config

    return Workspace.from_config(read_config(root)).project(project).refs


def test_a_ref_is_one_toml_line_with_only_what_differs_from_the_defaults():
    assert ref_toml(MAIN) == '{ label = "m", name = "mainline/master", authority = "mainline" }'
    assert ref_toml(CORE) == (
        '{ label = "c", name = "mm-core/for-next", authority = "acceptance", view = "MM-core" }'
    )
    assert ref_toml(NEXT).endswith('authority = "acceptance", ends_review = true }')
    assert 'ends_review = false' in ref_toml(make_ref('m', 'x', Authority.MAINLINE, '', False))


def test_only_the_projects_refs_are_rewritten(root):
    write_refs(root, 'linux', [MAIN, NEXT, CORE])
    text = (root / 'workspace.toml').read_text()
    assert text == CONFIG.replace(
        '  { label = "n", name = "next/master", authority = "acceptance", ends_review = true },\n',
        '  { label = "n", name = "next/master", authority = "acceptance", ends_review = true },\n'
        '  { label = "c", name = "mm-core/for-next", authority = "acceptance", view = "MM-core" },\n',
    )
    assert refs_of(root, 'linux') == (MAIN, NEXT, CORE)
    write_refs(root, 'linux', [MAIN])
    assert refs_of(root, 'linux') == (MAIN,)
    assert '# The kernel.' in (root / 'workspace.toml').read_text()


def test_a_project_without_refs_gets_them_at_the_end_of_its_table(root):
    write_refs(root, 'ndctl', [make_ref('m', 'origin/main', Authority.MAINLINE)])
    text = (root / 'workspace.toml').read_text()
    assert text.startswith(CONFIG)
    assert text.endswith(
        'refs = [\n  { label = "m", name = "origin/main", authority = "mainline" },\n]\n'
    )
    assert refs_of(root, 'ndctl')[0].name == 'origin/main'


def test_this_machines_overrides_are_written_when_they_set_the_refs(root):
    local = root / 'var' / 'workspace.toml'
    local.parent.mkdir()
    local.write_text(
        '[[projects]]\nid = "linux"\nrefs = [\n  { label = "m", name = "mainline/master", authority = "mainline" },\n]\n'
    )
    assert refs_file(root, 'linux') == local
    assert refs_file(root, 'ndctl') == root / 'workspace.toml'
    write_refs(root, 'linux', [MAIN, CORE])
    assert (root / 'workspace.toml').read_text() == CONFIG
    assert refs_of(root, 'linux') == (MAIN, CORE)


def test_a_write_that_would_not_read_back_is_refused(root):
    with pytest.raises(ValueError):
        write_refs(root, 'qemu', [MAIN])  # No such project.
    with pytest.raises(ValueError):
        write_refs(root, 'linux', [MAIN, make_ref('m', 'other/branch', Authority.ACCEPTANCE)])
    assert (root / 'workspace.toml').read_text() == CONFIG
