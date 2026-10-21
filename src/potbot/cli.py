"""Small, dependency-free entry point with explicit capability boundaries."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from potbot import __version__


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch commands without importing adapters for unrelated operations."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "fixture":
        from potbot.evaluation.repository_fixture import main as fixture_main

        return fixture_main(args[1:])
    if args and args[0] == "db":
        from potbot.persistence.harness import main as database_main

        return database_main(args[1:])
    if args and args[0] == "admin":
        from potbot.persistence.admin_cli import main as admin_main

        return admin_main(args[1:])
    if args and args[0] == "task":
        from potbot.api.task_cli import main as task_main

        return task_main(args[1:])
    if args and args[0] == "worker":
        from potbot.persistence.worker_cli import main as worker_main

        return worker_main(args[1:])
    if args and args[0] == "embedding":
        from potbot.memory.embedding_cli import main as embedding_main

        return embedding_main(args[1:])
    if args and args[0] == "syn":
        from potbot.semantics.cli import main as syn_main

        return syn_main(args[1:])
    if args and args[0] == "execution":
        from potbot.tools.execution_cli import main as execution_main

        return execution_main(args[1:])
    if args and args[0] == "credentials":
        from potbot.credentials import main as credentials_main

        return credentials_main(args[1:])
    if args and args[0] == "dependencies":
        from potbot.dependencies import main as dependencies_main

        return dependencies_main(args[1:])
    if args and args[0] == "patch":
        from potbot.tools.patch_cli import main as patch_main

        return patch_main(args[1:])
    if args and args[0] == "models":
        from potbot.models.cli import main as models_main

        return models_main(args[1:])

    parser = argparse.ArgumentParser(
        prog="potbot",
        description="Foundation for an evidence-driven, multi-model coding agent.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("info", help="show implemented capabilities as JSON")
    commands.add_parser("fixture", help="create, check, repair, and clean up real Git fixtures")
    commands.add_parser("db", help="manage isolated PostgreSQL integration-test schemas")
    commands.add_parser("admin", help="migrate and provision application workspaces")
    commands.add_parser("task", help="submit, inspect, revise, and control durable tasks")
    commands.add_parser("worker", help="run recoverable fixture outbox jobs")
    commands.add_parser("embedding", help="set up, index, and query local Qwen embeddings")
    commands.add_parser("syn", help="import, resolve and search typed local SYN snapshots")
    commands.add_parser("execution", help="capture repositories and run qualified isolated tools")
    commands.add_parser("patch", help="run, resume and export the scripted verified patch workflow")
    commands.add_parser(
        "models", help="inspect model profiles, envelopes and pinned host readiness"
    )
    commands.add_parser(
        "dependencies", help="inspect and download verified local model dependencies"
    )
    commands.add_parser(
        "credentials", help="manage PostgreSQL connection URLs in the native credential vault"
    )
    parsed = parser.parse_args(args)
    if parsed.command == "info":
        print(
            json.dumps(
                {
                    "package": "potbot",
                    "version": __version__,
                    "implementation_phase": 5,
                    "implementation_phase_status": "foundation",
                    "capabilities": [
                        "versioned_domain_contracts",
                        "real_git_fixtures",
                        "postgresql_vector_test_harness",
                        "reuse_discovery_policy",
                        "scoped_durable_tasks",
                        "immutable_evidence_and_artifacts",
                        "leased_outbox_workers",
                        "task_budget_ledger",
                        "model_deployment_and_invocation_records",
                        "local_standard_vector_embeddings",
                        "scoped_syn_references_and_exact_type_inventory",
                        "named_embedding_spaces",
                        "immutable_repository_snapshots",
                        "fenced_candidate_workspaces",
                        "durable_execution_and_reconciliation",
                        "qualified_linux_process_isolation",
                        "versioned_semantic_registry_and_validation",
                        "semantic_task_and_context_pins",
                        "hard_supervised_local_embeddings",
                        "restartable_scripted_patch_pipeline",
                        "immutable_implementation_handoffs",
                        "baseline_and_exact_state_verification",
                        "retained_patch_verification_manifests",
                        "resumable_verified_dependency_downloads",
                        "native_credential_store_and_first_startup",
                        "uv_bootstrapped_platform_launchers",
                        "bounded_model_proposals_and_predicates",
                        "recipient_specific_context_rendering",
                        "durable_offline_inference_gateway",
                        "separate_bounded_response_repairs",
                        "model_coding_result_admission",
                        "default_model_profiles_and_offline_preflight",
                    ],
                    "model_inference_available": False,
                    "model_downloads_required": False,
                    "embedding_model_download_required_for_local_encoding": True,
                    "live_coding_or_reasoning_inference_available": False,
                    "scripted_patch_adapter_available": True,
                },
                sort_keys=True,
            )
        )
    else:
        parser.print_help()
    return 0
