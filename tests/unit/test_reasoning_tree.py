from pathlib import Path

import pytest

from potbot.controller.reasoning_tree import ReasoningTree
from potbot.domain.errors import InvalidInputError


def test_durable_claim_recovery_and_no_implicit_retry(tmp_path: Path) -> None:
    path = tmp_path / 'tree.json'
    tree = ReasoningTree(path, token_budget=100)
    node = tree.add('Try indexed lookup', max_tokens=60)
    claimed = tree.claim()
    assert claimed.id == node.id
    recovered = ReasoningTree.load(path)
    assert recovered.nodes[node.id].state == 'indeterminate'
    assert recovered.claim() is None
    with pytest.raises(InvalidInputError):
        recovered.finish(node.id, claimed.claim_token, 'stale', tokens=2)


def test_steering_reopens_and_is_idempotent(tmp_path: Path) -> None:
    tree = ReasoningTree(tmp_path / 'tree.json')
    node = tree.add('Use large in-memory index')
    tree.reject(node.id, 'Memory cost')
    result = tree.steer('More RAM is now available', decision='reopen_branch',
                        explanation='Memory constraint changed', branch_id=node.id,
                        request_id='steer-1')
    assert tree.nodes[node.id].state == 'pending'
    assert tree.steer('More RAM is now available', decision='reopen_branch',
                      explanation='Memory constraint changed', branch_id=node.id,
                      request_id='steer-1') == result
    assert tree.revision == 1
    with pytest.raises(InvalidInputError):
        tree.steer('Different prompt', decision='no_change', explanation='no', request_id='steer-1')


def test_steering_invalidates_inflight_and_can_spawn(tmp_path: Path) -> None:
    tree = ReasoningTree(tmp_path / 'tree.json', token_budget=100)
    node = tree.add('Initial hypothesis', max_tokens=30)
    token = tree.claim().claim_token
    tree.steer('New constraint', decision='spawn_hypothesis',
               hypothesis='Alternative hypothesis', explanation='Investigate alternative')
    tree.finish(node.id, token, 'old result', tokens=12)
    assert tree.nodes[node.id].state == 'indeterminate'
    assert len(tree.nodes) == 2
    assert tree.spent_tokens == 12


def test_no_change_does_not_stale_active_claim(tmp_path: Path) -> None:
    tree = ReasoningTree(tmp_path / 'tree.json', token_budget=100)
    node = tree.add('Unchanged', max_tokens=30)
    token = tree.claim().claim_token
    tree.steer('Thanks', decision='no_change', explanation='No task change')
    tree.finish(node.id, token, 'Done', tokens=10)
    assert tree.nodes[node.id].state == 'completed'
