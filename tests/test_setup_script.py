"""Contract: ./setup.sh prepares a fresh clone from setup.conf; ./b4-maintainer runs it anywhere."""

import os
import pathlib
import shutil
import stat
import subprocess

import pytest
from gitutil import git

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ('setup.sh', 'setup.conf.example', 'b4-maintainer')

FAKE_PYTHON = r"""#!/bin/sh
# A Python 3.12 stand-in: answers the version probe and makes a .venv of fakes.
log="$FAKE_LOG"
case "$1" in
-c)
    case "$2" in
    *executable*) echo "$0" ;;
    esac
    exit 0
    ;;
-m)
    for dir; do :; done
    echo "venv $*" >> "$log"
    mkdir -p "$dir/bin"
    printf '#!/bin/sh\nexit 0\n' > "$dir/bin/python"
    printf '#!/bin/sh\necho "pip $*" >> "%s"\n' "$log" > "$dir/bin/pip"
    printf '#!/bin/sh\necho "b4-maintainer $PWD $*${SITE_MARK:+ site=$SITE_MARK}" >> "%s"\n' "$log" > "$dir/bin/b4-maintainer"
    chmod +x "$dir/bin/python" "$dir/bin/pip" "$dir/bin/b4-maintainer"
    ;;
esac
"""


def executable(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def clone(tmp_path, home):
    """A fresh clone holding the scripts, a fake python on PATH, and a state repository."""
    repo = tmp_path / 'b4-maintainer'
    repo.mkdir()
    for name in SCRIPTS:
        shutil.copy2(ROOT / name, repo / name)
    git('init', '-q', '-b', 'main', cwd=repo)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    executable(bin_dir / 'python-fake', FAKE_PYTHON)

    state = tmp_path / 'state-src'
    state.mkdir()
    git('init', '-q', '-b', 'main', cwd=state)
    (state / 'workspace.toml').write_text('[[projects]]\nid = "demo"\n')
    git('add', '.', cwd=state)
    git('-c', 'user.name=M', '-c', 'user.email=m@example.org', 'commit', '-q', '-m', 's', cwd=state)
    log = tmp_path / 'log'
    env = {
        **os.environ,
        'PATH': f'{bin_dir}:{os.environ["PATH"]}',
        'FAKE_LOG': str(log),
    }
    env.pop('VIRTUAL_ENV', None)
    return repo, state, log, env


def run(command, cwd, env):
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=60)


def configure(repo, state):
    conf = (repo / 'setup.conf.example').read_text()
    conf = conf.replace("STATE_REPO=''", f"STATE_REPO='{state}'")
    conf = conf.replace("PYTHON=''", "PYTHON='python-fake'")
    (repo / 'setup.conf').write_text(conf)


def test_first_run_creates_the_config_and_stops(clone):
    repo, _, log, env = clone
    result = run(['./setup.sh'], repo, env)
    assert result.returncode != 0
    assert (repo / 'setup.conf').read_text() == (repo / 'setup.conf.example').read_text()
    assert 'setup.conf' in result.stdout + result.stderr
    assert not log.exists()


def test_setup_builds_the_venv_clones_state_and_sets_up_the_workspace(clone):
    repo, state, log, env = clone
    configure(repo, state)
    result = run(['./setup.sh'], repo, env)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = log.read_text().splitlines()
    assert lines[0].startswith('venv -m venv') and lines[0].endswith('.venv')
    assert lines[1] == 'pip install -q -e deps/liblore -e deps/b4[tui] -e .'
    assert lines[2] == f'b4-maintainer {repo} setup'
    assert (repo / 'state' / 'workspace.toml').is_file()

    log.unlink()
    result = run(['./setup.sh'], repo, env)  # Again: nothing recreated or recloned.
    assert result.returncode == 0, result.stdout + result.stderr
    assert [line.split()[0] for line in log.read_text().splitlines()] == ['pip', 'b4-maintainer']


def test_dev_tools_and_no_state_repository(clone):
    repo, state, log, env = clone
    configure(repo, state)
    conf = (repo / 'setup.conf').read_text()
    conf = conf.replace(f"STATE_REPO='{state}'", "STATE_REPO=''").replace(
        'DEV_TOOLS=no', 'DEV_TOOLS=yes'
    )
    (repo / 'setup.conf').write_text(conf)
    result = run(['./setup.sh'], repo, env)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = log.read_text().splitlines()
    assert lines[1].endswith('-e . pytest pytest-asyncio pytest-xdist ruff')
    assert len(lines) == 2  # No state, so no workspace to set up.
    assert 'STATE_REPO' in result.stdout


def test_a_missing_interpreter_is_reported(clone):
    repo, state, _, env = clone
    configure(repo, state)
    conf = (repo / 'setup.conf').read_text().replace("PYTHON='python-fake'", "PYTHON='nope3'")
    (repo / 'setup.conf').write_text(conf)
    result = run(['./setup.sh'], repo, env)
    assert result.returncode != 0
    assert '3.11' in result.stderr


def test_the_wrapper_runs_the_venv_on_this_workspace_from_anywhere(clone, tmp_path):
    repo, state, log, env = clone
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    result = run([str(repo / 'b4-maintainer')], elsewhere, env)
    assert result.returncode != 0 and 'setup.sh' in result.stderr

    configure(repo, state)
    assert run(['./setup.sh'], repo, env).returncode == 0
    log.unlink()
    result = run([str(repo / 'b4-maintainer'), 'sync'], elsewhere, {**env, 'VIRTUAL_ENV': '/x'})
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == [f'b4-maintainer {repo} sync']


def test_without_a_site_file_setup_uses_plain_pip(clone):
    repo, state, log, env = clone
    configure(repo, state)
    result = run(['./setup.sh'], repo, env)
    assert 'site' not in result.stdout.lower()
    assert 'pip install -q -e deps/liblore -e deps/b4[tui] -e .' in log.read_text()


def test_a_site_file_adds_pip_arguments_and_a_default_python(clone):
    repo, state, log, env = clone
    configure(repo, state)
    conf = (repo / 'setup.conf').read_text().replace("PYTHON='python-fake'", "PYTHON=''")
    (repo / 'setup.conf').write_text(conf)
    (repo / 'setup.site.conf').write_text(
        "PYTHON='python-fake'\nPIP_ARGS=(--index-url https://mirror.example.org/simple)\n"
    )
    result = run(['./setup.sh'], repo, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'setup.site.conf' in result.stdout
    lines = log.read_text().splitlines()
    assert lines[1] == (
        'pip install -q --index-url https://mirror.example.org/simple '
        '-e deps/liblore -e deps/b4[tui] -e .'
    )


def test_a_site_install_function_replaces_pip(clone, tmp_path):
    repo, state, log, env = clone
    configure(repo, state)
    site = tmp_path / 'internal' / 'b4-maintainer.site.conf'
    site.parent.mkdir()
    site.write_text(f'site_install() {{\n    echo "site_install $VENV $*" >> "{log}"\n}}\n')
    conf = (repo / 'setup.conf').read_text().replace("SITE_CONF=''", f"SITE_CONF='{site}'")
    (repo / 'setup.conf').write_text(conf)
    result = run(['./setup.sh'], repo, env)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = log.read_text().splitlines()
    assert lines[1] == f'site_install {repo}/.venv -e deps/liblore -e deps/b4[tui] -e .'
    assert not any(line.startswith('pip ') for line in lines)


def test_a_named_site_file_that_is_missing_is_an_error(clone, tmp_path):
    repo, state, _, env = clone
    configure(repo, state)
    conf = (
        (repo / 'setup.conf')
        .read_text()
        .replace("SITE_CONF=''", f"SITE_CONF='{tmp_path / 'absent.conf'}'")
    )
    (repo / 'setup.conf').write_text(conf)
    result = run(['./setup.sh'], repo, env)
    assert result.returncode != 0 and 'absent.conf' in result.stderr


def test_the_wrapper_applies_the_site_environment(clone, tmp_path):
    repo, state, log, env = clone
    configure(repo, state)
    assert run(['./setup.sh'], repo, env).returncode == 0
    log.unlink()
    (repo / 'setup.site.conf').write_text('export SITE_MARK=internal\n')
    assert run([str(repo / 'b4-maintainer')], tmp_path, env).returncode == 0
    assert log.read_text().splitlines() == [f'b4-maintainer {repo}  site=internal']


def test_an_existing_venv_older_than_3_11_is_rebuilt(clone):
    repo, state, log, env = clone
    configure(repo, state)
    (repo / '.venv' / 'bin').mkdir(parents=True)
    # Runs, but as Python 3.9: fails only the 3.11 probe.
    executable(
        repo / '.venv' / 'bin' / 'python',
        '#!/bin/sh\ncase "$2" in *version_info*) exit 1 ;; esac\nexit 0\n',
    )
    assert run(['./setup.sh'], repo, env).returncode == 0
    assert log.read_text().splitlines()[0].startswith('venv -m venv --clear')


def test_first_run_continues_when_state_is_already_cloned(clone):
    repo, state, log, env = clone
    git('clone', '-q', str(state), 'state', cwd=repo)
    (repo / 'setup.site.conf').write_text("PYTHON='python-fake'\n")
    result = run(['./setup.sh'], repo, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (repo / 'setup.conf').is_file()
    assert log.read_text().splitlines()[-1] == f'b4-maintainer {repo} setup'
