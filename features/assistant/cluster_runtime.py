"""Shared Cluster lifecycle values used by the conversation Agent."""

ACTIVE_CLUSTER_JOB_STATUSES = frozenset({
    "created", "queued", "leasing", "assigned", "dispatching", "running",
    "stopping", "collecting", "worker_lost",
})
