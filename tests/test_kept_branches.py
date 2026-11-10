"""Contract: Branches keeps session output past its workbench: after a session's commits
are plucked onto a base, its other artifacts are offered to keep with the new commit; any
can be kept with a commit a base has beyond its upstream, or on a shelf; a commit shows
what was kept with it."""

from test_branches import ws  # noqa: F401

from b4_maintainer.branches import is_commit


def leak_row(service, workspace):
    found = service.snapshot(workspace)
    return next(r for r in found.details['tree:s1'] if is_commit(r) and 'leak' in r.text)


def test_a_pluck_offers_the_sessions_other_artifacts_with_the_new_commit(ws):  # noqa: F811
    service, workspace, project = ws
    assert [a.title for a in service.keepable('tree:s1')] == ['test run']  # Not commits.
    row = leak_row(service, workspace)
    tip = service.snapshot(workspace).tips['base:linux:me/base']
    result = service.pluck(workspace, 'tree:s1', [row], 'base:linux:me/base', tip)
    assert result.ok
    artifacts, commits = service.pluck_keep_offer(
        workspace, 'tree:s1', 'base:linux:me/base', result.picked
    )
    new = result.picked[0][1]
    assert [a.title for a in artifacts] == ['test run']
    assert commits == [(f'commit:linux:{new}', 'mm: fix the leak')]
    # Copies into another session tree are not offered (nothing leaves the sessions).
    assert service.pluck_keep_offer(workspace, 'tree:s1', 'tree:s2', result.picked) == ([], [])

    note = service.keep(workspace, 'tree:s1', artifacts, f'commit:linux:{new}')
    assert note.startswith('Kept 1 with mm: fix the leak')
    [kept] = service.kept_items()
    assert kept.session == 's1' and kept.commit == new and kept.patch_id
    assert kept.path is not None and kept.path.read_text() == 'all 3 tests pass\n'
    base_row = next(
        r
        for r in service.snapshot(workspace).details['base:linux:me/base']
        if is_commit(r) and 'leak' in r.text
    )
    shown = service.show(base_row)
    assert shown.startswith('Kept with this commit') and 'results: test run' in shown


def test_k_targets_are_unlanded_base_commits_and_shelves(ws):  # noqa: F811
    service, workspace, _project = ws
    targets = service.keep_targets(workspace)
    assert [label for _key, label in targets] == [
        'mm: pulled ahead fix  (me/base)',
        'A new shelf…',
    ]
    assert targets[-1][0] == 'shelf:'
    [artifact] = service.keepable('s1')
    service.keep(workspace, 's1', [artifact], 'shelf:leak work')
    assert ('shelf:leak work', 'Shelf: leak work') in service.keep_targets(workspace)
    assert service.unkeep(service.kept_items()[0].id) and service.kept_items() == []


def test_a_workbench_artifact_row_is_kept_as_its_manifest_entry(ws):  # noqa: F811
    from b4_maintainer.attachments import Attachment

    service, workspace, _project = ws
    [artifact] = service.keepable('s1')
    row = Attachment('file', 'results: test run', 's1', path=artifact.path, title='test run')
    assert service.as_artifact(row) == artifact  # With its summary and kind.
    other = Attachment('file', 'x', 's1', path=artifact.path.parent, title='logs', group='results')
    assert service.as_artifact(other).kind == 'results'
