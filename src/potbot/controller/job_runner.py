"""One leased hosted-generation job at a time, with durable streamed progress."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from functools import partial
from typing import Any

from potbot.controller.jobs import JobService
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import DomainError, RevisionConflictError
from potbot.domain.jobs import JobLease
from potbot.models.hosted_gateway import HostedAdmissionError, HostedInferenceGateway


class HostedJobRunner:
    def __init__(self, jobs: JobService, gateway: HostedInferenceGateway) -> None:
        self.jobs, self.gateway = jobs, gateway

    def _finish(
        self, context: WorkspaceContext, lease: JobLease, state: str, **kwargs: Any
    ) -> dict[str, Any]:
        try:
            return self.jobs.finish(context, lease, state, **kwargs)
        except RevisionConflictError:
            return self.jobs.inspect(context, lease.job_id)

    def run_once(self, context: WorkspaceContext, worker_id: str) -> dict[str, Any] | None:
        lease = self.jobs.claim(context, worker_id, lease_seconds=45)
        if lease is None:
            return None
        stopped, abort = threading.Event(), threading.Event()

        def heartbeat() -> None:
            renewal = time.monotonic() + 10
            while not stopped.wait(1):
                try:
                    if self.jobs.cancelled(context, lease):
                        abort.set()
                    if time.monotonic() >= renewal:
                        self.jobs.renew(context, lease, lease_seconds=45)
                        renewal = time.monotonic() + 10
                except Exception:
                    # Losing the lease cannot turn into permission to continue.
                    abort.set()
                    return

        monitor = threading.Thread(target=heartbeat, name="potbot-job-heartbeat", daemon=True)
        monitor.start()
        try:
            return self._run(context, lease, abort)
        except RevisionConflictError:
            # A lost/expired generation is already terminal or owned elsewhere.
            return self.jobs.inspect(context, lease.job_id)
        except DomainError as error:
            current = self.jobs.inspect(context, lease.job_id)
            state = "indeterminate" if current["dispatched_at"] is not None else "failed"
            if (
                error.code.value == "cancelled"
                and current["dispatched_at"] is None
                and current["cancellation_requested"]
            ):
                state = "cancelled"
            return self._finish(context, lease, state, error_code=error.code.value)
        except Exception:
            current = self.jobs.inspect(context, lease.job_id)
            state = "indeterminate" if current["dispatched_at"] is not None else "failed"
            return self._finish(context, lease, state, error_code="internal_error")
        finally:
            stopped.set()
            monitor.join(timeout=1)

    def _run(
        self, context: WorkspaceContext, lease: JobLease, abort: threading.Event
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        totals: dict[str, int] = {}
        completed = 0
        state, error_code = "succeeded", None
        accounting = "conservative_estimate"
        ordinal = -1
        generation_in_progress = False
        flush: Callable[[], None] | None = None
        try:
            for ordinal, selection in enumerate(lease.selected_models):
                flush = None
                if abort.is_set() or self.jobs.cancelled(context, lease):
                    if self.jobs.inspect(context, lease.job_id)["cancellation_requested"]:
                        state, error_code = "cancelled", "cancelled"
                    else:
                        state, error_code = "indeterminate", "indeterminate"
                    break
                self.jobs.progress(
                    context, lease, "generating", ordinal, len(lease.selected_models)
                )
                buffered = ""
                emitted_at = time.monotonic()

                def emit(
                    text: str, *, final: bool = False, selected: dict[str, str] = selection
                ) -> None:
                    nonlocal buffered, emitted_at
                    buffered += text
                    if (
                        not final
                        and len(buffered.encode()) < 1024
                        and time.monotonic() - emitted_at < 0.1
                    ):
                        return
                    while buffered:
                        # UTF-8 characters fit the queue's 64 KiB delta bound.
                        chunk, buffered = buffered[:8192], buffered[8192:]
                        self.jobs.output(
                            context,
                            lease,
                            chunk,
                            provider=selected["provider"],
                            model=selected["model"],
                            role=selected["role"],
                        )
                    emitted_at = time.monotonic()

                flush = partial(emit, "", final=True)
                generation_in_progress = True
                result = self.gateway.generate(
                    context, lease, ordinal, on_delta=emit, cancelled=abort.is_set
                )
                generation_in_progress = False
                # Retain known accounting before publishing the final output
                # buffer or progress; either database write can itself fail.
                results.append(result)
                if result.get("accounted_amounts"):
                    for dimension, amount in result["accounted_amounts"].items():
                        totals[dimension] = totals.get(dimension, 0) + amount
                if result["status"] == "completed" and result["finish_reason"] == "stop":
                    completed += 1
                if result["accounting"] == "pending_reconciliation":
                    accounting = "pending_reconciliation"
                flush()
                flush = None
                if result["status"] == "indeterminate":
                    state, error_code = "indeterminate", "indeterminate"
                    break
                if result["status"] == "rejected":
                    state, error_code = "failed", result.get("error_code") or "model_unavailable"
                    if (
                        error_code == "cancelled"
                        and self.jobs.inspect(context, lease.job_id)["cancellation_requested"]
                    ):
                        state = "cancelled"
                    break
                if result["finish_reason"] != "stop":
                    state, error_code = (
                        "failed",
                        "provider_refused"
                        if result["finish_reason"] == "refusal"
                        else "model_unavailable",
                    )
                    break
                self.jobs.progress(
                    context, lease, "model_completed", completed, len(lease.selected_models)
                )
        except HostedAdmissionError as error:
            # This ordinal was never admitted. Earlier successful calls retain
            # their receipts, model count and accounted usage.
            state, error_code = "failed", error.code.value
        except Exception as error:
            state = "indeterminate"
            error_code = error.code.value if isinstance(error, DomainError) else "internal_error"
            if generation_in_progress:
                # Persistence failures during generation cannot establish that
                # the remote request was unsent or that usage was settled.
                accounting = "pending_reconciliation"
            if flush is not None:
                try:
                    flush()
                except Exception:
                    pass
            if ordinal >= 0:
                try:
                    invocation = self.gateway.inspect(
                        context, self.gateway.operation_id(context, lease, ordinal)
                    )
                except Exception:
                    pass
                else:
                    if invocation["id"] not in {value["invocation_id"] for value in results}:
                        results.append({"invocation_id": invocation["id"]})
        metadata: dict[str, Any] = {
            "invocation_ids": [value["invocation_id"] for value in results],
            "completed_models": completed,
            "accounting": accounting,
            "usage": totals,
        }
        return self._finish(context, lease, state, result=metadata, error_code=error_code)
