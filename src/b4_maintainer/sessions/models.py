# SPDX-License-Identifier: GPL-2.0-or-later
"""The models a session can use: the default (pi's, or the one `[sessions] default_model`
names, at `default_thinking`), `[[models]]` presets, dry-run, pi's catalog.

pi owns providers, credentials, and its model catalog; b4-maintainer only lists what pi
reports (`pi --list-models`) and passes the choice on (`--model`, `--thinking`). Presets
are configured by hand in workspace.toml; the UI never edits them.
"""

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

THINKING = ('off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')
# Registered by the b4-maintainer pi extension: replays a script instead of calling a model.
DRY_RUN_MODEL = 'b4m/dry-run'


@dataclass(frozen=True)
class CatalogModel:
    id: str  # provider/model, as `--model` takes it.
    thinking: bool


@dataclass(frozen=True)
class ModelChoice:
    id: str  # The dialog's key: default, a preset id, dry-run, or provider/model.
    label: str
    kind: str  # default, preset, dry-run, or catalog
    model: str = ''  # pi's --model; empty for pi's default.
    thinking: str = ''  # pi's --thinking; empty for pi's default.
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    available: bool = True
    reason: str = ''  # Why it is unavailable.


def parse_catalog(text: str) -> list[CatalogModel]:
    """Models from `pi --list-models`: a header row, then one row per model."""
    rows = text.splitlines()
    header = next(
        (n for n, line in enumerate(rows) if line.split()[:2] == ['provider', 'model']), None
    )
    if header is None:
        return []
    columns = rows[header].split()
    thinking = columns.index('thinking') if 'thinking' in columns else None
    found = []
    for line in rows[header + 1 :]:
        cells = line.split()
        if len(cells) < 2:
            continue
        has_thinking = thinking is not None and len(cells) > thinking and cells[thinking] == 'yes'
        found.append(CatalogModel(f'{cells[0]}/{cells[1]}', has_thinking))
    return found


def _preset(raw: Mapping[str, Any], known: Optional[set[str]]) -> Optional[ModelChoice]:
    pid = raw.get('id')
    if not isinstance(pid, str) or not pid or raw.get('enabled', True) is False:
        return None
    choice = ModelChoice(pid, pid, 'preset')
    model, thinking = raw.get('model'), raw.get('thinking', '')
    args, env = raw.get('args', []), raw.get('env', {})
    if not isinstance(model, str) or not model:
        return dataclasses.replace(choice, available=False, reason='no model configured')
    if thinking and thinking not in THINKING:
        reason = f'thinking must be one of {", ".join(THINKING)}'
        return dataclasses.replace(choice, model=model, available=False, reason=reason)
    choice = dataclasses.replace(
        choice,
        label=f'{pid} ({model})',
        model=model,
        thinking=str(thinking),
        args=tuple(str(a) for a in args) if isinstance(args, list) else (),
        env={str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {},
    )
    if known is not None and model not in known:
        return dataclasses.replace(
            choice, available=False, reason=f"{model} is not in pi's model list"
        )
    return choice


def model_choices(
    catalog: Optional[Sequence[CatalogModel]],
    presets: Sequence[Any],
    default: str = '',
    thinking: str = '',
) -> list[ModelChoice]:
    """The default, presets in config order, dry-run, then the catalog (None: unknown).

    The default (`default`, what a launch without a choice uses) is pi's default model;
    with *default* (`[sessions] default_model`: a preset's id or pi's `provider/model`),
    that model, and with *thinking* (`[sessions] default_thinking`) at that thinking level
    (over a preset's own); either set, pi's own default stays offered (`pi-default`)."""
    known = {m.id for m in catalog} if catalog is not None else None
    pi_default = ModelChoice('default', "pi's default model", 'default')
    parsed = [p for raw in presets if isinstance(raw, Mapping) and (p := _preset(raw, known))]
    if default or thinking:
        named = next((p for p in parsed if p.id == default), None) if default else None
        if named is not None:
            chosen = dataclasses.replace(named, id='default', kind='default')
            name = named.label
        else:
            chosen = ModelChoice('default', '', 'default', default)
            name = default or "pi's default model"
            if default and known is not None and default not in known:
                chosen = dataclasses.replace(
                    chosen, available=False, reason=f"{default} is not in pi's model list"
                )
        if thinking:
            chosen = dataclasses.replace(chosen, thinking=thinking)
            name = f'{name}, thinking {thinking}'
            if thinking not in THINKING:
                reason = f'thinking must be one of {", ".join(THINKING)}'
                chosen = dataclasses.replace(chosen, available=False, reason=reason)
        chosen = dataclasses.replace(chosen, label=f'default: {name}')
        choices = [chosen, dataclasses.replace(pi_default, id='pi-default')]
    else:
        choices = [pi_default]
    choices.extend(parsed)
    choices.append(ModelChoice('dry-run', 'dry-run (scripted, no model)', 'dry-run', DRY_RUN_MODEL))
    choices.extend(ModelChoice(m.id, m.id, 'catalog', m.id) for m in catalog or ())
    return choices
