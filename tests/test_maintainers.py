"""Contract: ownership comes from the project's get_maintainer.pl; roles from MAINTAINERS."""

import stat

from b4_maintainer.adapters.maintainers import OwnerCache, author_roles

MAINTAINERS = """\
List of maintainers

MEMORY MANAGEMENT - NUMA
M:\tExample Maintainer <Maintainer@Example.org>
R:\tExample Reviewer <reviewer@example.org>
L:\tlinux-mm@kvack.org
F:\tmm/mempolicy.c

MEMORY MANAGEMENT - MIGRATION
M:\tOther Maintainer <other@example.org>
R:\tExample Maintainer <maintainer@example.org>
F:\tmm/migrate.c
"""

# Stands in for scripts/get_maintainer.pl: reads a patch on stdin, logs each call.
FAKE = """\
#!/bin/sh
input=$(cat)
echo "$input" | grep '^+++ ' >> "$(dirname "$0")/calls.log"
echo "--" >> "$(dirname "$0")/calls.log"
case "$input" in *fail.c*) echo "boom" >&2; exit 2;; esac
echo "$input" | grep -q 'mm/mempolicy' && echo "Example Maintainer <maintainer@example.org>"
echo "$input" | grep -q 'mm/migrate' && echo "Other Maintainer <other@example.org>"
exit 0
"""


def checkout(tmp_path):
    root = tmp_path / 'linux'
    (root / 'scripts').mkdir(parents=True)
    (root / 'MAINTAINERS').write_text(MAINTAINERS)
    script = root / 'scripts' / 'get_maintainer.pl'
    script.write_text(FAKE)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return root


def calls(root):
    log = root / 'scripts' / 'calls.log'
    return log.read_text().count('--\n') if log.exists() else 0


def test_author_roles_prefer_maintainer_and_ignore_case(tmp_path):
    roles = author_roles(checkout(tmp_path) / 'MAINTAINERS')
    assert roles['maintainer@example.org'] == 'maintainer'
    assert roles['reviewer@example.org'] == 'reviewer'
    assert roles['other@example.org'] == 'maintainer'
    assert 'nobody@example.org' not in roles


def test_missing_maintainers_file_has_no_roles(tmp_path):
    assert author_roles(tmp_path / 'MAINTAINERS') == {}


def test_owners_come_from_get_maintainer_and_are_cached(tmp_path):
    root = checkout(tmp_path)
    cache = OwnerCache(tmp_path / 'owners.json', root)
    paths = ('mm/mempolicy.c', 'mm/new-file.c')
    assert cache.get(paths) is None
    assert cache.update([paths, ('mm/migrate.c',)]) == 2
    assert cache.get(paths) == {'maintainer@example.org'}
    assert cache.get(('mm/migrate.c',)) == {'other@example.org'}
    # New files reach the script as patch paths; nothing needs to exist in the tree.
    assert '+++ b/mm/new-file.c' in (root / 'scripts' / 'calls.log').read_text()

    again = OwnerCache(tmp_path / 'owners.json', root)
    assert again.update([paths]) == 0
    assert again.get(paths) == {'maintainer@example.org'}
    assert calls(root) == 2


def test_maintainers_change_invalidates(tmp_path):
    root = checkout(tmp_path)
    OwnerCache(tmp_path / 'owners.json', root).update([('mm/mempolicy.c',)])
    (root / 'MAINTAINERS').write_text(MAINTAINERS + '\nNEW SECTION\nM:\tX <x@example.org>\n')
    cache = OwnerCache(tmp_path / 'owners.json', root)
    assert cache.get(('mm/mempolicy.c',)) is None
    assert cache.update([('mm/mempolicy.c',)]) == 1


def test_failures_are_not_cached(tmp_path):
    root = checkout(tmp_path)
    cache = OwnerCache(tmp_path / 'owners.json', root)
    assert cache.update([('mm/fail.c',)]) == 0
    assert cache.get(('mm/fail.c',)) is None
    assert cache.errors == 1


def test_no_get_maintainer_means_no_owners(tmp_path):
    root = tmp_path / 'other'
    root.mkdir()
    cache = OwnerCache(tmp_path / 'owners.json', root)
    assert not cache.available
    assert cache.update([('a.c',)]) == 0
    assert cache.get(('a.c',)) is None
