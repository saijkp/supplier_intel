"""The post-merge helper cannot silently exceed a reveal allowance or send."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest


@pytest.fixture
def rollout(monkeypatch):
    scripts = Path(__file__).parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location('planning_rollout_test', scripts/'planning_rollout.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'architects', lambda: {})
    return module


def test_reveal_cap_prevents_any_paid_call(rollout, tmp_path, monkeypatch):
    state = tmp_path/'state.json'
    state.write_text(json.dumps({'attempted_reveals': 59, 'reveal_previews': [{'id':'b', 'calls':2}]}))
    calls = []
    monkeypatch.setattr(rollout, 'call', lambda *a: calls.append(a))
    monkeypatch.setattr(sys, 'argv', ['rollout', '--state', str(state), 'reveal', '--approved-cap', '60'])
    with pytest.raises(SystemExit, match='cap exceeded'):
        rollout.main()
    assert not calls


def test_uncertain_reveal_is_persisted_before_call(rollout, tmp_path, monkeypatch):
    state = tmp_path/'state.json'
    state.write_text(json.dumps({'reveal_previews': [{'id':'b', 'calls':1, 'maximum_credit_cost':1}]}))
    def fail(*args):
        recorded = json.loads(state.read_text())
        assert recorded['attempted_reveals'] == 1 and recorded['reveal_previews'][0]['attempted']
        raise SystemExit('network unknown')
    monkeypatch.setattr(rollout, 'call', fail)
    monkeypatch.setattr(sys, 'argv', ['rollout', '--state', str(state), 'reveal', '--approved-cap', '60'])
    with pytest.raises(SystemExit, match='network unknown'):
        rollout.main()


def test_empty_render_writes_blocker_not_invented_contacts(rollout, tmp_path, monkeypatch):
    output = tmp_path/'approval.txt'
    monkeypatch.setattr(sys, 'argv', ['rollout', '--state', str(tmp_path/'state.json'), 'render', '--output', str(output)])
    rollout.main()
    assert '0 verified drafts' in output.read_text() and 'BLOCKED' in output.read_text()
