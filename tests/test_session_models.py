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


def test_the_settings_default_model_is_the_default_and_pis_own_stays_offered():
    catalog = parse_catalog(LISTING)
    choices = model_choices(catalog, [], default='beta/beta-pro')
    assert [c.id for c in choices][:2] == ['default', 'pi-default']
    found = by_id(choices)
    assert (found['default'].model, found['default'].kind) == ('beta/beta-pro', 'default')
    assert 'beta/beta-pro' in found['default'].label
    assert found['pi-default'].model == '' and found['pi-default'].label == "pi's default model"


def test_a_preset_named_as_the_default_brings_its_thinking_and_arguments():
    presets = [{'id': 'deep', 'model': 'alpha/alpha-large-2', 'thinking': 'high', 'args': ['--x']}]
    found = by_id(model_choices(parse_catalog(LISTING), presets, default='deep'))
    default = found['default']
    assert (default.model, default.thinking, default.args) == (
        'alpha/alpha-large-2',
        'high',
        ('--x',),
    )
    assert default.kind == 'default' and 'deep' in default.label
    assert 'deep' in found  # Still offered as itself.


def test_a_default_model_pi_does_not_list_is_unavailable_and_pis_own_remains():
    found = by_id(model_choices(parse_catalog(LISTING), [], default='gamma/old'))
    assert not found['default'].available and "not in pi's model list" in found['default'].reason
    assert found['pi-default'].available


def test_without_a_default_model_pis_default_is_the_default():
    choices = model_choices(parse_catalog(LISTING), [], default='')
    assert choices[0].id == 'default' and choices[0].model == ''
    assert 'pi-default' not in by_id(choices)


def test_the_default_thinking_level_rides_with_the_default_model():
    catalog = parse_catalog(LISTING)
    found = by_id(model_choices(catalog, [], default='beta/beta-pro', thinking='high'))
    assert (found['default'].model, found['default'].thinking) == ('beta/beta-pro', 'high')
    assert 'thinking high' in found['default'].label
    assert found['pi-default'].thinking == ''  # pi's own, as pi has it.


def test_a_thinking_level_alone_applies_to_pis_default_model():
    found = by_id(model_choices(parse_catalog(LISTING), [], thinking='low'))
    assert (found['default'].model, found['default'].thinking) == ('', 'low')
    assert "pi's default model" in found['default'].label and 'pi-default' in found


def test_the_default_thinking_level_overrides_a_named_presets():
    presets = [{'id': 'deep', 'model': 'alpha/alpha-large-2', 'thinking': 'high'}]
    found = by_id(model_choices(parse_catalog(LISTING), presets, default='deep', thinking='low'))
    assert found['default'].thinking == 'low' and found['deep'].thinking == 'high'


def test_an_unknown_thinking_level_makes_the_default_unavailable():
    found = by_id(model_choices(parse_catalog(LISTING), [], thinking='lots'))
    assert not found['default'].available and 'thinking must be one of' in found['default'].reason
    assert found['pi-default'].available
