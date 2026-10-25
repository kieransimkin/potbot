"use client";

import { useMemo, useState } from "react";
import { filterJobs } from "@/lib/activity";
import type { JobViewFilter } from "@/lib/activity";
import { useWorkspace } from "./WorkspaceContext";
import { DockControls } from "./Panels";

export function ActivityPanel() {
  const { demo, activity, activityStatus, refreshActivity, openPanel, transfers, transfersStatus } = useWorkspace();
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState<JobViewFilter>("all");
  const visible = useMemo(() => filterJobs(activity?.jobs ?? [], query, status), [activity, query, status]);
  return <section className="activity-pane panel-scroll" aria-label="Workspace activity">
    <header className="activity-toolbar">
      <span className="eyebrow">WORKSPACE ACTIVITY</span>
      <h2>Running work</h2>
      <p role="status">{demo ? "Demo mode · no backend events" : activityStatus}</p>
      <button type="button" onClick={() => refreshActivity()} disabled={demo}>Refresh</button>
    </header>
    <label className="activity-query" htmlFor="job-search">Find a job
      <input id="job-search" type="search" value={query} maxLength={120}
        onChange={event => setQuery(event.target.value)} placeholder="Type, stage or job ID" /></label>
    <div className="activity-filter" role="group" aria-label="Job state">
      {(["all", "active", "finished"] as const).map(value => <button type="button" key={value}
        aria-pressed={value === status} onClick={() => setStatus(value)}>{value}</button>)}
    </div>
    <ul className="activity-jobs" aria-label="Workspace jobs">
      {visible.map(job => <li key={job.id} className="activity-job">
        <strong>{job.type}</strong><span className={`activity-job-state ${job.state}`}>{job.state}</span>
        <small>{job.id} · {job.stage ?? "No stage reported"}</small>
        {job.completed !== null && job.total !== null && <div>
          <progress value={job.completed} max={Math.max(1, job.total)}
            aria-label={`Job progress: ${job.completed} of ${job.total}`} />
          <small>{job.completed} / {job.total} steps (units reported by backend)</small>
        </div>}
        {job.state === "indeterminate" && <small>Outcome requires reconciliation; no automatic retry implied.</small>}
      </li>)}
    </ul>
    {!visible.length && <p className="muted-note">{activity ? "No matching jobs in the most recent 50." : "No job feed is connected."}</p>}
    {activity && <section className="activity-history" aria-label="Recent job events">
      <h3>Recent events</h3>
      {activity.gap && <p role="alert">Event cursor gap detected. Earlier activity may be incomplete.</p>}
      <ol>{activity.events.slice(-30).reverse().map(event => <li key={event.sequence}>
        <span>#{event.sequence} · {event.type}</span><small>{event.jobId.slice(0, 8)}</small>
      </li>)}</ol>
      {activity.hasMore && <small>More events available; updating shortly.</small>}
    </section>}
    <div className="activity-unavailable"><strong>Model transfers and installations</strong>
      <p>{demo ? "Preview · no transfer jobs" : transfersStatus} · {transfers.length} artifact transfer receipts.
        Inference jobs remain distinct from file downloads.</p>
      <button type="button" onClick={() => openPanel("downloads")}>Verified byte progress ↗</button>
      <button type="button" onClick={() => openPanel("installations")}>Model installations ↗</button>
    </div>
    <DockControls id="activity" />
  </section>;
}
