"""Task-bound reasoning inference accounting with a fenced dispatch journal.

Task budgeting is opt-in through an explicit task identity. This adapter never
makes provider requests: it records the possible-dispatch boundary *before*
the HTTP transport runs. Uncertain requests retain their budget reservation.
A terminal observation survives crashes between receipt and settlement.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from potbot.controller.budgets import BudgetLedger
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.models.hosted_policy import HostedPolicy
from potbot.models.providers.contracts import ProviderRequest, ProviderSelection
from potbot.persistence.database import Database


@dataclass(frozen=True, slots=True)
class ReasoningBudgetTicket:
    identity: UUID
    reservation_id: UUID
    run_id: str
    node_id: str
    episode_id: UUID
    started: float


class ReasoningBudgetJournal:
    """Reserve task dimensions, fence dispatch, observe, then settle exactly once."""

    def __init__(self, database: Database, policy: HostedPolicy) -> None:
        self.database = database
        self.policy = policy
        self.budgets = BudgetLedger(database)

    @staticmethod
    def _identity(context: WorkspaceContext, run_id: str, node_id: str) -> UUID:
        return uuid5(NAMESPACE_URL, f"potbot:reasoning:{context.workspace_id}:{run_id}:{node_id}")

    def prepare(
        self, context: WorkspaceContext, *, run_id: str, node_id: str,
        lease_token: UUID, episode_id: UUID, provider: str, model: str,
        protocol: str | None, prompt_hash: str, system: str, prompt: str,
        max_output_tokens: int, timeout_seconds: float,
    ) -> ReasoningBudgetTicket:
        if not isinstance(episode_id, UUID) or not isinstance(lease_token, UUID):
            raise InvalidInputError("Reasoning budget requires task and live run identities")
        try:
            run_uuid, node_uuid = UUID(hex=run_id), UUID(node_id)
        except (TypeError, ValueError):
            raise InvalidInputError("Invalid reasoning run/node identity") from None
        if not isinstance(prompt_hash, str) or len(prompt_hash) != 64 or any(
            c not in "0123456789abcdef" for c in prompt_hash
        ):
            raise InvalidInputError("Reasoning prompt requires immutable SHA256 identity")
        request = ProviderRequest(
            ProviderSelection(provider, model, protocol), prompt=prompt, system=system,
            max_output_tokens=max_output_tokens, timeout_seconds=timeout_seconds,
        )
        self.policy.authorize(request)
        rule = self.policy.rule(provider, model, "reasoning")
        # UTF-8 bytes are the conservative admission proxy already used by the
        # hosted gateway. Cache discounts are not assumed.
        input_ceiling = len((prompt + system).encode("utf-8"))
        maximum_cost = rule.charge(input_ceiling, max_output_tokens)
        if maximum_cost > self.policy.max_cost_microunits:
            raise InvalidInputError("Inference exceeds the operator's cost ceiling")
        amounts = {
            "calls": 1,
            "tokens": input_ceiling + max_output_tokens,
            "inference_ms": int(timeout_seconds * 1000),
            "money_microunits": maximum_cost,
        }
        identity = self._identity(context, run_id, node_id)
        key = f"reasoning:{run_id}:{node_id}"
        # Reserve and journal within a single transaction. Any pre-dispatch
        # validation or receipt failure rolls the resource reservation back.
        with self.database.transaction(context) as conn:
            run = conn.execute(
                "SELECT state,lease_token,lease_until FROM reasoning_runtime_runs "
                "WHERE workspace_id=%s AND id=%s FOR UPDATE",
                (context.workspace_id, run_uuid),
            ).fetchone()
            if run is None or run["state"] != "running" or run["lease_token"] != lease_token:
                raise StateMismatchError("Reasoning run lease changed before budget admission")
            if not conn.execute(
                "SELECT 1 FROM reasoning_runtime_runs WHERE workspace_id=%s AND id=%s "
                "AND lease_until > clock_timestamp()",
                (context.workspace_id, run_uuid),
            ).fetchone():
                raise StateMismatchError("Reasoning lease expired before budget admission")
            reserved = self.budgets.reserve_in_transaction(
                conn, context, episode_id, amounts, key
            )
            reservation_id = UUID(str(reserved["id"]))
            task = conn.execute(
                "SELECT status,active_contract_id FROM episodes "
                "WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, episode_id),
            ).fetchone()
            if task is None or task["status"] != "running" or task["active_contract_id"] is None:
                raise StateMismatchError("Task revision is not running")
            conn.execute(
                "INSERT INTO reasoning_budget_invocations "
                "(workspace_id,id,run_id,node_id,episode_id,contract_revision_id,"
                "reservation_id,provider,model,prompt_hash,status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'reserved')",
                (context.workspace_id, identity, run_uuid, node_uuid, episode_id,
                 task["active_contract_id"], reservation_id, provider, model, prompt_hash),
            )
        return ReasoningBudgetTicket(identity, reservation_id, run_id, node_id,
                                     episode_id, time.monotonic())

    def dispatch(self, context: WorkspaceContext, ticket: ReasoningBudgetTicket,
                 lease_token: UUID) -> None:
        """Commit the possible-dispatch marker and lease/task check atomically."""
        with self.database.transaction(context) as conn:
            row = conn.execute(
                "UPDATE reasoning_budget_invocations i SET status='dispatched',"
                "updated_at=clock_timestamp() WHERE i.workspace_id=%s AND i.id=%s "
                "AND i.status='reserved' AND EXISTS ("
                "SELECT 1 FROM reasoning_runtime_runs r WHERE r.workspace_id=i.workspace_id "
                "AND r.id=i.run_id AND r.state='running' AND r.lease_token=%s "
                "AND r.lease_until>clock_timestamp()) AND EXISTS ("
                "SELECT 1 FROM episodes e WHERE e.workspace_id=i.workspace_id "
                "AND e.id=i.episode_id AND e.status='running' "
                "AND e.active_contract_id=i.contract_revision_id) RETURNING i.id",
                (context.workspace_id, ticket.identity, lease_token),
            ).fetchone()
            if row is None:
                raise StateMismatchError("Reasoning authorization changed before dispatch")

    def observe(self, context: WorkspaceContext, ticket: ReasoningBudgetTicket,
                usage: dict[str, int], *, elapsed_ms: int, rate_provider: str,
                rate_model: str) -> None:
        """Journal the terminal provider observation before settling the budget."""
        from psycopg.types.json import Jsonb

        if any(type(usage.get(k)) is not int or usage[k] < 0 for k in (
            "prompt_tokens", "completion_tokens", "total_tokens"
        )) or usage["total_tokens"] <= 0:
            raise InvalidInputError("Provider usage missing; hold uncertain reservation")
        if usage["prompt_tokens"] + usage["completion_tokens"] > usage["total_tokens"]:
            raise InvalidInputError("Provider usage totals are inconsistent")
        if type(elapsed_ms) is not int or elapsed_ms < 0:
            raise InvalidInputError("Invalid provider execution time")
        actual = {
            "calls": 1, "tokens": usage["total_tokens"], "inference_ms": elapsed_ms,
            "money_microunits": self.policy.rule(rate_provider, rate_model, "reasoning").charge(
                usage["prompt_tokens"], usage["completion_tokens"],
                total_tokens=usage["total_tokens"],
            ),
        }
        with self.database.transaction(context) as conn:
            row = conn.execute(
                "UPDATE reasoning_budget_invocations SET status='observed',actual=%s,"
                "updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s "
                "AND status='dispatched' RETURNING id",
                (Jsonb(actual), context.workspace_id, ticket.identity),
            ).fetchone()
            if row is None:
                raise StateMismatchError("Reasoning provider observation is not admissible")
        self.settle_observed(context, ticket.identity)

    def settle_observed(self, context: WorkspaceContext, identity: UUID) -> None:
        """Safe after an observed-but-unsettled crash; never repeats inference."""
        with self.database.transaction(context) as conn:
            row = conn.execute(
                "SELECT status,actual,reservation_id FROM reasoning_budget_invocations "
                "WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, identity),
            ).fetchone()
        if row is None:
            raise ScopeDeniedError("Reasoning invocation unavailable")
        if row["status"] == "settled":
            return
        if row["status"] != "observed" or row["actual"] is None:
            raise StateMismatchError("Reasoning observation requires manual reconciliation")
        # Idempotent: a second process may settle after the first observed the
        # row. The independent BudgetLedger transaction is authoritative.
        self.budgets.settle(context, row["reservation_id"], row["actual"],
                            "conservative_estimate")
        with self.database.transaction(context) as conn:
            conn.execute(
                "UPDATE reasoning_budget_invocations SET status='settled',"
                "updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s "
                "AND status='observed'",
                (context.workspace_id, identity),
            )

    def uncertain(self, context: WorkspaceContext, ticket: ReasoningBudgetTicket) -> None:
        """Only a confirmed non-dispatch can release a reserved allowance."""
        with self.database.transaction(context) as conn:
            row = conn.execute(
                "SELECT status FROM reasoning_budget_invocations "
                "WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, ticket.identity),
            ).fetchone()
        if row is None or row["status"] in {"settled", "released", "observed"}:
            return
        status = row["status"]
        if status == "reserved":
            self.budgets.release(context, ticket.reservation_id,
                                 "reasoning call confirmed not dispatched",
                                 confirmed_not_started=True)
            target = "released"
        else:
            self.budgets.mark_indeterminate(context, ticket.reservation_id,
                                            "reasoning provider outcome uncertain")
            target = "indeterminate"
        with self.database.transaction(context) as conn:
            conn.execute(
                "UPDATE reasoning_budget_invocations SET status=%s,updated_at=clock_timestamp() "
                "WHERE workspace_id=%s AND id=%s AND status=%s",
                (target, context.workspace_id, ticket.identity, status),
            )

    def list_for_run(self, context: WorkspaceContext, run_id: str) -> list[dict[str, Any]]:
        with self.database.transaction(context) as conn:
            rows = conn.execute(
                "SELECT id,node_id,episode_id,reservation_id,provider,model,prompt_hash,"
                "status,actual,created_at FROM reasoning_budget_invocations "
                "WHERE workspace_id=%s AND run_id=%s ORDER BY created_at,id",
                (context.workspace_id, UUID(hex=run_id)),
            ).fetchall()
            return [dict(row) for row in rows]
