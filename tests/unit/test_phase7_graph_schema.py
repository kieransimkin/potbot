"""Version-18 graph links are appended, not substituted for historical migrations."""

from potbot.persistence.schema import application_migrations


def test_graph_link_migration_is_append_only_scoped_and_history_retained():
    migrations = application_migrations()
    assert tuple(m.version for m in migrations) == tuple(range(1, 19))
    assert migrations[15].name == "phase7_schema"
    assert migrations[16].name == "phase7_worker_schema"
    latest = migrations[17]
    assert (latest.version, latest.name) == (18, "phase7_graph_schema")
    assert latest.workspace_tables == latest.append_only_tables == ("research_solve_links",)
    sql = "\n".join(latest.statements)
    for clause in (
        "UNIQUE(workspace_id,solve_run_id,research_run_id)",
        "UNIQUE(workspace_id,solve_run_id,node_id)",
        "FOREIGN KEY(workspace_id,episode_id,research_run_id)",
        "FOREIGN KEY(workspace_id,episode_id,solve_run_id)",
        "FOREIGN KEY(workspace_id,solve_run_id,parent_node_id)",
        "FOREIGN KEY(workspace_id,evidence_event_id)",
        "synny://type/potbot-research-solve-link",
        "'verified_immutable_repository_source_only'",
        "content->>'patch_verified'='false'",
        "content->>'need_resolved'='false'",
    ):
        assert clause in sql
