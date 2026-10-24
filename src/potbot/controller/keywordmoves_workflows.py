"""Guarded DanceFlow/KeywordMoves workflow composition and atomic evidence receipts.

The host supplies the installed KeywordMoves adapter and explicitly grants operations.
This module never imports, downloads, installs, or executes a third-party tool itself.
Offline results are *source-labelled evidence*, not measured cross-platform demand.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from potbot.domain.errors import InvalidInputError, StateMismatchError
from potbot.domain.serialization import canonical_json
from potbot.tools.temporary import allocate_file, create_directory

_NAME = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_SECRET = re.compile(r"(?:^|_)(?:api_key|key|secret|password|cookie|token|authorization|credential)(?:$|_)", re.I)
_MAX_ARTIFACT = 2_000_000
_MAX_STEPS = 64
# Safe means no network or paid call is initiated by the *named operation*.
# The adapter is still an explicitly trusted host-provided code dependency.
OFFLINE_OPERATIONS: frozenset[tuple[str, str]] = frozenset({
    ("text-library", "extract-local"), ("text-library", "extract-literal"),
    ("candidate-focus", "related"), ("candidate-focus", "matching-terms"),
    ("candidate-shortlist", "select"), ("research-batch", "validate-plan"),
    ("google-trends", "import-interest"), ("google-trends", "import-related"),
    ("google-trends", "assess-interest"), ("google-trends", "plan-event-cycles"),
    ("observed-evidence", "import-observations"),
    ("hashtag-planner", "profiles"), ("hashtag-planner", "discover"),
    ("hashtag-planner", "plan"), ("hashtag-planner", "audit"),
    ("hashtag-planner", "plan-native"), ("hashtag-planner", "audit-native"),
    ("native-export", "import-csv"),
    ("nltk", "extract"), ("spacy", "extract"),
    ("@potbot", "bind-target"), ("@potbot", "merge-candidates"),
})


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(value: object) -> bytes:
    try:
        result = canonical_json(value).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        raise InvalidInputError("workflow contains invalid JSON values") from None
    if len(result) > _MAX_ARTIFACT:
        raise InvalidInputError("workflow record exceeds the artifact bound")
    return result


def _atomic(path: Path, content: bytes) -> None:
    if len(content) > _MAX_ARTIFACT:
        raise InvalidInputError("workflow artifact exceeds two megabytes")
    if path.is_symlink():
        raise InvalidInputError("workflow destination cannot be a symlink")
    create_directory(path.parent)
    fd, temporary = allocate_file(path.parent, prefix="kwm-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _input_path(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or "\\" in name:
        raise InvalidInputError("workflow input must be a relative POSIX path")
    relative = Path(name)
    if relative.is_absolute() or any(p in {".", ".."} for p in relative.parts):
        raise InvalidInputError("workflow input escapes its selected sandbox")
    root = root.resolve(strict=True)
    target = root / relative
    # Reject symlinked inputs, not merely paths resolving outside the root.
    parent = root
    for part in relative.parts:
        parent = parent / part
        if parent.is_symlink():
            raise InvalidInputError("workflow input symlink is not permitted")
    if not target.resolve(strict=False).is_relative_to(root):
        raise InvalidInputError("workflow input escapes its selected sandbox")
    return target


@dataclass(frozen=True, slots=True)
class WorkflowStep:
    name: str
    plugin: str
    operation: str
    inputs: tuple[str, ...] = ()  # file:path or step:name, in declared order
    keywords: tuple[str, ...] = ()
    options: tuple[tuple[str, str | int | float | bool | None], ...] = ()
    approval: str | None = None

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name):
            raise InvalidInputError("workflow step name must be a bounded identifier")
        if not isinstance(self.plugin, str) or not _NAME.fullmatch(self.plugin.removeprefix("@")):
            raise InvalidInputError("workflow plugin must be a bounded identifier")
        if not isinstance(self.operation, str) or not _NAME.fullmatch(self.operation):
            raise InvalidInputError("workflow operation must be a bounded identifier")
        if not isinstance(self.inputs, tuple) or len(self.inputs) > 20:
            raise InvalidInputError("workflow requires at most twenty inputs")
        for item in self.inputs:
            if not isinstance(item, str) or not (
                item.startswith("file:") or item.startswith("step:")
            ) or not 6 <= len(item) <= 260:
                raise InvalidInputError("workflow input must select a file or a previous step")
        if not isinstance(self.keywords, tuple) or len(self.keywords) > 100 or any(
            not isinstance(k, str) or not k.strip() or len(k) > 256 for k in self.keywords
        ):
            raise InvalidInputError("workflow keywords must be bounded")
        if not isinstance(self.options, tuple) or len(self.options) > 30:
            raise InvalidInputError("workflow options must be bounded")
        names: set[str] = set()
        for k, v in self.options:
            if not isinstance(k, str) or not _NAME.fullmatch(k) or _SECRET.search(k):
                raise InvalidInputError("workflow options cannot carry credentials")
            if k in names or type(v) not in {str, int, float, bool, type(None)}:
                raise InvalidInputError("workflow options must be unique JSON scalars")
            names.add(k)
        _canonical(dict(self.options))
        if self.approval is not None and (
            not isinstance(self.approval, str) or not 1 <= len(self.approval) <= 128
        ):
            raise InvalidInputError("workflow approval reference is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "plugin": self.plugin, "operation": self.operation,
                "inputs": list(self.inputs), "keywords": list(self.keywords),
                "options": dict(self.options), "approval": self.approval}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> WorkflowStep:
        if not isinstance(raw, dict) or set(raw) != {
            "name", "plugin", "operation", "inputs", "keywords", "options", "approval"
        } or not isinstance(raw["inputs"], list) or not isinstance(
            raw["keywords"], list
        ) or not isinstance(raw["options"], dict):
            raise InvalidInputError("workflow step schema is invalid")
        return cls(raw["name"], raw["plugin"], raw["operation"],
                   tuple(raw["inputs"]), tuple(raw["keywords"]),
                   tuple(raw["options"].items()), raw["approval"])


@dataclass(frozen=True, slots=True)
class WorkflowPlan:
    source_revision: str
    subject: str
    steps: tuple[WorkflowStep, ...]
    schema: str = "potbot-keywordmoves-workflow/v1"

    def __post_init__(self) -> None:
        if self.schema != "potbot-keywordmoves-workflow/v1" or not _REVISION.fullmatch(
            self.source_revision
        ):
            raise InvalidInputError("workflow requires a pinned KeywordMoves commit")
        if not isinstance(self.subject, str) or not 1 <= len(self.subject) <= 256:
            raise InvalidInputError("workflow requires a bounded subject")
        if not isinstance(self.steps, tuple) or not 1 <= len(self.steps) <= _MAX_STEPS:
            raise InvalidInputError("workflow requires 1-64 steps")
        seen: set[str] = set()
        for step in self.steps:
            if not isinstance(step, WorkflowStep) or step.name in seen:
                raise InvalidInputError("workflow has an invalid or repeated step")
            for reference in step.inputs:
                if reference.startswith("step:") and reference[5:] not in seen:
                    raise InvalidInputError("workflow step depends on a missing or later stage")
            seen.add(step.name)

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "source_revision": self.source_revision,
                "subject": self.subject, "steps": [s.to_dict() for s in self.steps]}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> WorkflowPlan:
        if not isinstance(raw, dict) or set(raw) != {
            "schema", "source_revision", "subject", "steps"
        } or not isinstance(raw["steps"], list):
            raise InvalidInputError("workflow manifest schema is invalid")
        return cls(raw["source_revision"], raw["subject"],
                   tuple(WorkflowStep.from_dict(step) for step in raw["steps"]),
                   raw["schema"])

    @property
    def sha256(self) -> str:
        return _digest(_canonical(self.to_dict()))


class KeywordMovesInvoke(Protocol):
    """Explicitly approved host executor; must enforce sandbox and provider policy."""

    def __call__(self, *, plugin: str, operation: str, inputs: tuple[Path, ...],
                 keywords: tuple[str, ...], options: Mapping[str, object]) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class WorkflowExecutionPolicy:
    """Trusted host grant, NOT a model-controlled field in a workflow manifest."""

    allowed_operations: frozenset[tuple[str, str]] = OFFLINE_OPERATIONS
    approved_external: frozenset[str] = frozenset()

    def inventory(self, catalogue: Mapping[str, tuple[str, ...]]) -> list[dict[str, str]]:
        """Describe every installed operation without granting a capability."""
        rows = []
        for plugin, operations in sorted(catalogue.items()):
            for operation in sorted(operations):
                op = (plugin, operation)
                if op in OFFLINE_OPERATIONS:
                    state = "offline_registered"
                elif op in self.allowed_operations:
                    state = "host_policy_gated"
                else:
                    state = "not_enabled"
                rows.append({"plugin": plugin, "operation": operation,
                             "policy": state})
        return rows

    def authorize(self, step: WorkflowStep) -> None:
        op = (step.plugin, step.operation)
        if op not in self.allowed_operations:
            raise InvalidInputError("workflow operation is not enabled by the host")
        if op not in OFFLINE_OPERATIONS and (
            step.approval is None or step.approval not in self.approved_external
        ):
            raise InvalidInputError("remote or paid workflow operation requires explicit approval")
        if step.plugin == "@potbot" and step.operation not in {"bind-target", "merge-candidates"}:
            raise InvalidInputError("unknown built-in transformation")


@dataclass(frozen=True, slots=True)
class WorkflowRunResult:
    status: Literal["completed", "awaiting_input"]
    completed: tuple[str, ...]
    waiting_for: str | None
    artifacts: dict[str, str]
    plan_sha256: str


class KeywordMovesWorkflow:
    """Bounded sequential DAG over host-authorized KeywordMoves operations.

    A stage receipt is committed as `running` BEFORE invoking third-party code.
    A crashed invocation is never automatically re-run; manual reconciliation is
    needed. Missing human review is a safe pause, not an automatic approval.
    """

    def __init__(self, *, plan: WorkflowPlan, input_root: Path, output_root: Path,
                 invoke: KeywordMovesInvoke, policy: WorkflowExecutionPolicy | None = None,
                 emit: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self.plan = plan
        self.input_root = Path(input_root).resolve(strict=True)
        self.output_root = Path(output_root)
        # Same immutable workflow identity survives process restarts at this root.
        self.workflow_id = _digest((str(self.output_root.absolute()) + plan.sha256).encode())[:32]
        self.invoke = invoke
        self.policy = policy or WorkflowExecutionPolicy()
        self.emit = emit or (lambda _event, _data: None)
        if self.output_root.is_symlink() or self.input_root == self.output_root.resolve():
            raise InvalidInputError("workflow output must have its own non-symlink directory")
        if not callable(invoke):
            raise InvalidInputError("workflow requires a registered host invocation")
        for step in plan.steps:
            self.policy.authorize(step)

    def _result_path(self, name: str) -> Path:
        return self.output_root / "results" / f"{name}.json"

    def _receipt_path(self, name: str) -> Path:
        return self.output_root / "receipts" / f"{name}.json"

    def run(self, *, resume: bool = False) -> WorkflowRunResult:
        """One writer per output tree, including across Python processes.

        An interrupted process leaves a visible lock requiring operator review,
        rather than permitting two concurrent or blind-resumed invocations.
        """
        create_directory(self.output_root)
        lock = self.output_root / ".keywordmoves-writer.lock"
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise StateMismatchError("workflow has an active or unreviewed writer") from None
        try:
            create_directory(self.output_root / "results")
            create_directory(self.output_root / "receipts")
            return self._run_locked(resume=resume)
        finally:
            os.close(fd)
            lock.unlink(missing_ok=True)

    def _run_locked(self, *, resume: bool = False) -> WorkflowRunResult:
        manifest = self.output_root / "plan.json"
        planned = _canonical(self.plan.to_dict())
        if manifest.exists():
            if not resume or manifest.is_symlink() or manifest.read_bytes() != planned:
                raise StateMismatchError("a different workflow already owns this output")
        else:
            if resume or any((self.output_root / "results").iterdir()) or any(
                (self.output_root / "receipts").iterdir()
            ):
                raise StateMismatchError("cannot resume without a durable workflow manifest")
            _atomic(manifest, planned)
        completed: list[str] = []
        artifacts: dict[str, str] = {}
        for step in self.plan.steps:
            inputs: list[Path] = []
            input_hashes: list[str] = []
            for reference in step.inputs:
                if reference.startswith("step:"):
                    source = reference[5:]
                    if source not in artifacts:
                        raise StateMismatchError("workflow dependency has not completed")
                    path = self._result_path(source)
                else:
                    path = _input_path(self.input_root, reference[5:])
                    if not path.is_file():
                        self.emit("keywordmoves.awaiting_input", {
                            "workflow_id": self.workflow_id, "step": step.name,
                            "input": reference,
                        })
                        return WorkflowRunResult("awaiting_input", tuple(completed),
                                                 reference, artifacts, self.plan.sha256)
                if path.is_symlink() or path.stat().st_size > _MAX_ARTIFACT:
                    raise InvalidInputError("workflow source is missing or exceeds its bound")
                inputs.append(path)
                input_hashes.append(_digest(path.read_bytes()))
            receipt_path = self._receipt_path(step.name)
            output_path = self._result_path(step.name)
            if receipt_path.exists():
                if not resume or receipt_path.is_symlink():
                    raise StateMismatchError("stage already has an execution receipt")
                receipt = json.loads(receipt_path.read_bytes())
                if (receipt.get("state") != "completed" or
                    receipt.get("input_hashes") != input_hashes or
                    receipt.get("plan_sha256") != self.plan.sha256 or
                    not output_path.is_file() or output_path.is_symlink() or
                    _digest(output_path.read_bytes()) != receipt.get("output_sha256")):
                    raise StateMismatchError("workflow stage may be uncertain or source changed")
                artifacts[step.name] = receipt["output_sha256"]
                completed.append(step.name)
                continue
            if output_path.exists() or output_path.is_symlink():
                raise StateMismatchError("unacknowledged workflow artifact; refuse replay")
            common = {"workflow_id": self.workflow_id, "step": step.name,
                      "plugin": step.plugin, "operation": step.operation,
                      "plan_sha256": self.plan.sha256, "source_revision": self.plan.source_revision,
                      "input_hashes": input_hashes}
            # A possible effect has been recorded before the first invocation.
            _atomic(receipt_path, _canonical({**common, "state": "running"}))
            self.emit("keywordmoves.step.started", common)
            try:
                if step.plugin == "@potbot":
                    data = (self._bind_target(step, inputs, input_hashes)
                            if step.operation == "bind-target"
                            else self._merge_candidates(step, inputs, input_hashes))
                else:
                    data = self.invoke(plugin=step.plugin, operation=step.operation,
                                       inputs=tuple(inputs), keywords=step.keywords,
                                       options=dict(step.options))
                self._validate_result(step, data)
                # Prevent result attribution to inputs changed during an invocation.
                if any(_digest(path.read_bytes()) != original for path, original in
                       zip(inputs, input_hashes, strict=True)):
                    raise StateMismatchError("workflow source changed during execution")
                output = _canonical(data)
                _atomic(output_path, output)
                checksum = _digest(output)
                _atomic(receipt_path, _canonical({**common, "state": "completed",
                                                 "output_sha256": checksum,
                                                 "candidate_count": len(data["keywords"])}))
            except Exception:
                # The existing running receipt forbids replay after an uncertain call.
                self.emit("keywordmoves.step.indeterminate", {"workflow_id": self.workflow_id,
                                                                "step": step.name})
                raise
            completed.append(step.name)
            artifacts[step.name] = checksum
            self.emit("keywordmoves.step.completed", {"workflow_id": self.workflow_id,
                                                       "step": step.name,
                                                       "sha256": checksum})
        return WorkflowRunResult("completed", tuple(completed), None, artifacts,
                                 self.plan.sha256)

    @staticmethod
    def _validate_result(step: WorkflowStep, data: dict[str, Any]) -> None:
        if not isinstance(data, dict) or data.get("plugin") != step.plugin or data.get(
            "operation"
        ) != step.operation or not isinstance(data.get("keywords"), list) or not isinstance(
            data.get("metadata", {}), dict
        ):
            raise InvalidInputError("KeywordMoves produced a mismatched result contract")
        if len(data["keywords"]) > 50000:
            raise InvalidInputError("KeywordMoves returned too many candidates")
        for row in data["keywords"]:
            if not isinstance(row, dict) or not isinstance(row.get("phrase"), str):
                raise InvalidInputError("KeywordMoves returned malformed candidates")
        _canonical(data)

    @staticmethod
    def _bind_target(step: WorkflowStep, inputs: list[Path], hashes: list[str]) -> dict[str, Any]:
        if len(inputs) != 1 or step.keywords or set(dict(step.options)) != {
            "subject", "platform"
        }:
            raise InvalidInputError("target binding needs one result and explicit target")
        settings = dict(step.options)
        if any(not isinstance(settings[k], str) or not settings[k] for k in settings):
            raise InvalidInputError("target binding needs nonempty subject and platform")
        data = json.loads(inputs[0].read_bytes())
        if not isinstance(data, dict) or not isinstance(data.get("keywords"), list):
            raise InvalidInputError("target binding requires a KeywordMoves result")
        pool = dict(data)
        pool["plugin"], pool["operation"] = step.plugin, step.operation
        pool["metadata"] = {**data.get("metadata", {}),
                            "target_subject": settings["subject"],
                            "target_platform": settings["platform"],
                            "potbot_source_sha256": hashes[0],
                            "potbot_source_plugin": data.get("plugin")}
        return pool

    @staticmethod
    def _merge_candidates(step: WorkflowStep, inputs: list[Path],
                          hashes: list[str]) -> dict[str, Any]:
        """Merge literal matching phrases, never aggregate distinct demand units."""
        if len(inputs) < 2 or step.keywords or step.options:
            raise InvalidInputError("candidate merge requires multiple saved results")
        merged: dict[str, dict[str, Any]] = {}
        total = 0
        for path, source_digest in zip(inputs, hashes, strict=True):
            value = json.loads(path.read_bytes())
            if not isinstance(value, dict) or not isinstance(value.get("keywords"), list):
                raise InvalidInputError("candidate merge expects KeywordMoves result objects")
            for candidate in value["keywords"]:
                if not isinstance(candidate, dict) or not isinstance(candidate.get("phrase"), str):
                    raise InvalidInputError("candidate merge received invalid row")
                phrase = candidate["phrase"].strip()
                if not phrase or len(phrase) > 256:
                    raise InvalidInputError("candidate phrase is missing or unbounded")
                key = " ".join(phrase.casefold().split())
                if not isinstance(candidate.get("evidence", []), list):
                    raise InvalidInputError("candidate evidence is not a list")
                if not isinstance(candidate.get("metadata", {}), dict):
                    raise InvalidInputError("candidate metadata is not a dictionary")
                row = merged.setdefault(key, {"phrase": phrase, "relationship": "source-merged",
                                              "score": None, "evidence": [], "metadata": {
                                                  "source_lineage": []}})
                row["evidence"].extend(candidate.get("evidence", []))
                row["metadata"]["source_lineage"].append({
                    "source_sha256": source_digest,
                    "source_plugin": value.get("plugin"),
                    "source_operation": value.get("operation"),
                    "original_score": candidate.get("score"),
                    "original_metadata": candidate.get("metadata", {}),
                })
                total += 1
                if total > 50000:
                    raise InvalidInputError("candidate merge input exceeds row bound")
        return {"plugin": "@potbot", "operation": "merge-candidates",
                "keywords": list(merged.values()), "notes": [
                    "Exact normalized phrases merged; source metrics remain independent.",
                    "There is no combined popularity, search-volume or confidence score."],
                "metadata": {"source_hashes": hashes, "input_rows": total,
                             "merged_rows": len(merged), "external_demand": "unvalidated"}}

    def as_evidence_tool(self, stages: tuple[str, ...]) -> Any:
        """Host-admitted R4 tool over completed artifacts; never triggers a new step."""
        from potbot.controller.reasoning_investigation import RegisteredEvidenceTool

        if not isinstance(stages, tuple) or not stages or len(stages) > _MAX_STEPS:
            raise InvalidInputError("R4 workflow evidence requires a bounded stage allowlist")
        if any(not _NAME.fullmatch(stage) for stage in stages):
            raise InvalidInputError("R4 evidence stage has an invalid name")
        if len(set(stages)) != len(stages):
            raise InvalidInputError("R4 evidence stages cannot be repeated")

        def search(*, query: str, limit: int, run_id: str) -> tuple[Any, ...]:
            query = query.casefold().strip()
            found = []
            for stage in stages:
                if len(found) >= limit:
                    break
                path = self._result_path(stage)
                # `evidence` checks the completed receipt and original digest.
                if not path.is_file() or path.is_symlink():
                    continue
                raw = path.read_bytes()
                value = json.loads(raw)
                if any(query in str(row.get("phrase", "")).casefold()
                       for row in value.get("keywords", [])):
                    found.append(self.evidence(stage, run_id=run_id))
            return tuple(found)

        return RegisteredEvidenceTool(
            "keywordmoves-completed-evidence", "internal_workflows",
            "syn://type/potbot-keywordmoves-evidence-cog", search,
        )

    def evidence(self, step_name: str, *, run_id: str) -> Any:
        """Make a completed atomic step available to R4 without new execution."""
        from potbot.controller.reasoning_investigation import EvidenceFinding

        if not _NAME.fullmatch(step_name) or not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise InvalidInputError("evidence needs bounded stage and run identifiers")
        receipt_path = self._receipt_path(step_name)
        if not receipt_path.is_file() or receipt_path.is_symlink():
            raise InvalidInputError("only completed stage receipts are evidence")
        raw = receipt_path.read_bytes()
        receipt = json.loads(raw)
        if receipt.get("state") != "completed" or receipt.get("plan_sha256") != self.plan.sha256:
            raise InvalidInputError("uncommitted KeywordMoves output cannot be evidence")
        result_path = self._result_path(step_name)
        if not result_path.is_file() or result_path.is_symlink():
            raise StateMismatchError("KeywordMoves evidence result is missing or linked")
        output = result_path.read_bytes()
        if _digest(output) != receipt["output_sha256"]:
            raise StateMismatchError("KeywordMoves source output changed")
        data = json.loads(output)
        excerpt = (f"KeywordMoves {receipt['plugin']}/{receipt['operation']}: "
                   f"{len(data['keywords'])} source-labelled candidates; "
                   "no cross-platform search demand inferred")
        return EvidenceFinding(
            source_ref=f"syn://thing/keywordmoves/{run_id}/{step_name}",
            source_sha256=receipt["output_sha256"],
            excerpt=excerpt,
            receipt_ref=f"sha256:{_digest(raw)}",
        )
