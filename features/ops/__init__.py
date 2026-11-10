"""Ops foundation: alert ledger, global kill-switch and disk hygiene.

The single persistence point for unattended-operation bookkeeping. Other
features consume these capabilities only through foundation ports
(``foundation.ops_port`` / ``foundation.automation_port``) — the composition
root wires them via :func:`register_ops_ports` at startup, which keeps the
feature graph acyclic (ADR 0030).
"""

from features.ops.service import (
    advancement_paused,
    get_ops_store,
    record_incident,
    set_advancement_paused,
)


def register_ops_ports() -> None:
    """Wire ops capabilities into the foundation ports (composition root).

    Called once from ``bootstrap.dependencies``; importing this package alone
    does not wire the ports, so unconfigured deployments keep their documented
    fail-open behaviour (no incidents, no sweeps, advancement never paused).
    """
    from foundation.automation_port import configure_advancement_gate
    from foundation.ops_port import (
        configure_alert_sweep,
        configure_disk_hygiene,
        configure_incident_recorder,
    )

    from .disk_hygiene import run_cleanup
    from .sweep import run_ops_sweep

    configure_incident_recorder(record_incident)
    configure_advancement_gate(advancement_paused)
    configure_alert_sweep(run_ops_sweep)
    configure_disk_hygiene(run_cleanup)


__all__ = [
    "advancement_paused",
    "get_ops_store",
    "record_incident",
    "register_ops_ports",
    "set_advancement_paused",
]
