"""Atomic task contracts and stop conditions are controller-owned."""

from __future__ import annotations

import pytest

from potbot.controller.atomic_tasks import ATOMIC_KINDS, AtomicOperation, TaskGraph
from potbot.domain.errors import InvalidInputError


def test_taxonomy_has_coverage_and_independent_verification() -> None:
    codes = [x.name for x in ATOMIC_KINDS]
    assert len(codes) == len(set(codes)) >= 35
    assert {"detect_urgent_stop", "check_job_status", "classify_atomicity",
            "run_registered_tool", "check_hash", "compare_two_answers"} <= set(codes)
    assert all(x.verifiable_by for x in ATOMIC_KINDS)


def test_recursive_planning_stops_at_contract_bound_leaf() -> None:
    graph = TaskGraph("Find and review suitable keywords")
    graph.decompose("root", (
        ("extract", "Extract literal phrases", ()),
        ("review", "Assess editorial relevance", ("extract",)),
    ))
    graph.admit_leaf("extract", operation=AtomicOperation("parse_json", "input", 1024, 1000),
                     estimated_input_bytes=30)
    assert graph.unresolved() == ("review",)
    assert graph.executable(set()) == ("extract",)
    assert graph.executable({"extract"}) == ()  # review is not yet a leaf
    with pytest.raises(InvalidInputError):
        graph.decompose("extract", (("a", "a", ()), ("b", "b", ())))
    graph.decompose("review", (
        ("rank", "Rank given candidates", ("extract",)),
        ("approve", "Request human review", ("rank",)),
    ))
    with pytest.raises(InvalidInputError):
        graph.admit_leaf("approve", operation=AtomicOperation(
            "request_human_approval", "steering", 1024, 1000, True), estimated_input_bytes=30)
    graph.admit_leaf("approve", operation=AtomicOperation(
        "request_human_approval", "steering", 1024, 1000, True),
        estimated_input_bytes=30, approved=True)
    assert not graph.executable({"extract"})  # rank still unadmitted


def test_cycle_and_invalid_decompositions_are_atomic_rejections() -> None:
    graph = TaskGraph("root", max_depth=2, max_nodes=6)
    with pytest.raises(InvalidInputError):
        graph.decompose("root", (("a", "A", ("b",)), ("b", "B", ("a",))))
    assert list(graph.nodes) == ["root"]
    with pytest.raises(InvalidInputError):
        graph.decompose("root", (("one", "X", ("unknown",)), ("two", "Y", ())))
    assert list(graph.nodes) == ["root"]
    graph.decompose("root", (("a", "A", ()), ("b", "B", ("a",))))
    with pytest.raises(InvalidInputError):
        graph.admit_leaf("a", operation=AtomicOperation("check_hash", "verification", 10, 20),
                         estimated_input_bytes=100)
    assert graph.nodes["a"].state == "needs_decomposition"
