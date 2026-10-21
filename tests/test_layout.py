from b4_maintainer.layout import Breakpoints, layout_mode


def test_default_breakpoints():
    assert layout_mode(200) == 'wide'
    assert layout_mode(160) == 'wide'
    assert layout_mode(130) == 'medium'
    assert layout_mode(100) == 'medium'
    assert layout_mode(99) == 'narrow'


def test_breakpoints_are_configurable():
    bp = Breakpoints(three_pane=120, single_pane=80)
    assert layout_mode(130, bp) == 'wide'
    assert layout_mode(90, bp) == 'medium'
    assert layout_mode(70, bp) == 'narrow'


def test_breakpoints_from_config():
    bp = Breakpoints.from_config({'three_pane': 140, 'single_pane': 90})
    assert bp == Breakpoints(140, 90)
    assert Breakpoints.from_config({}) == Breakpoints()
