"""Contract: the summary's Tree evidence lists only the tracked branches holding the
series (fully or in part); branches without it are left out, and with none holding it
it says so in one line."""

from b4_maintainer.app import MaintainerApp
from b4_maintainer.fixtures import make_item
from b4_maintainer.model import Authority, Coverage


def summary(tree):
    app = MaintainerApp(lambda: None)
    item = make_item(subject='mm: x', tree=tree)
    text = app._summary_for(item)
    return text.split('Tree evidence:\n', 1)[1].split('\n\n', 1)[0].splitlines()


def test_only_branches_holding_the_series_are_listed():
    tree = [
        ('m', Authority.MAINLINE, Coverage.NONE),
        ('n', Authority.ACCEPTANCE, Coverage.PARTIAL),
        ('s', Authority.ACCEPTANCE, Coverage.NONE),
        ('u', Authority.PRELIMINARY, Coverage.FULL),
    ]
    assert summary(tree) == ['  n n (acceptance): partial', '  u u (preliminary): full']


def test_held_nowhere_is_one_line():
    tree = [('m', Authority.MAINLINE, Coverage.NONE), ('n', Authority.ACCEPTANCE, Coverage.NONE)]
    assert summary(tree) == ['  in none of the 2 tracked branches']
    assert summary([]) == ['  unchecked']
