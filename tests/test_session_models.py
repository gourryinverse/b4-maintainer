"""Contract: the dialog's models are pi's default, configured presets, dry-run, and pi's catalog."""

from b4_maintainer.sessions.models import DRY_RUN_MODEL, model_choices, parse_catalog

# `pi --list-models` output (synthetic models), banner lines included.
LISTING = """\
Example pi build
provider   model                 context  max-out  thinking  images
alpha      alpha-large-2         1M       128K     yes       yes
alpha      alpha-small-2         200K     64K      no        yes
beta       beta-pro              1M       64K      yes       no
"""


def test_the_catalog_is_parsed_from_pi_list_models():
    catalog = parse_catalog(LISTING)
    assert [(m.id, m.thinking) for m in catalog] == [
        ('alpha/alpha-large-2', True),
        ('alpha/alpha-small-2', False),
        ('beta/beta-pro', True),
    ]
    assert parse_catalog('') == [] and parse_catalog('no header\nat all\n') == []


def by_id(choices):
    return {c.id: c for c in choices}


def test_default_presets_dry_run_then_catalog():
    presets = [
        {'id': 'deep', 'model': 'alpha/alpha-large-2', 'thinking': 'high', 'args': ['--x']},
        {'id': 'gone', 'model': 'gamma/old'},
        {'id': 'hidden', 'model': 'beta/beta-pro', 'enabled': False},
    ]
    choices = model_choices(parse_catalog(LISTING), presets)
    assert [c.id for c in choices] == [
        'default',
        'deep',
        'gone',
        'dry-run',
        'alpha/alpha-large-2',
        'alpha/alpha-small-2',
        'beta/beta-pro',
    ]
    found = by_id(choices)
    assert (found['default'].model, found['default'].kind) == ('', 'default')
    assert (found['deep'].model, found['deep'].thinking, found['deep'].args) == (
        'alpha/alpha-large-2',
        'high',
        ('--x',),
    )
    assert (
        not found['gone'].available
        and "gamma/old is not in pi's model list" in found['gone'].reason
    )
    assert found['dry-run'].model == DRY_RUN_MODEL and found['dry-run'].kind == 'dry-run'
    assert found['beta/beta-pro'].kind == 'catalog'


def test_invalid_presets_are_shown_disabled_with_the_reason():
    presets = [
        {'id': 'x', 'model': ''},
        {'id': 'y', 'model': 'alpha/alpha-large-2', 'thinking': 'extreme'},
        {'model': 'alpha/alpha-large-2'},  # No id: skipped.
        'not a table',
    ]
    found = by_id(model_choices(parse_catalog(LISTING), presets))
    assert not found['x'].available and 'model' in found['x'].reason
    assert not found['y'].available and 'thinking' in found['y'].reason
    assert len(found) == 2 + 2 + 3  # default, x, y, dry-run, catalog


def test_without_a_catalog_presets_cannot_be_checked_but_stay_usable():
    found = by_id(model_choices(None, [{'id': 'deep', 'model': 'alpha/alpha-large-2'}]))
    assert list(found) == ['default', 'deep', 'dry-run']
    assert found['deep'].available
