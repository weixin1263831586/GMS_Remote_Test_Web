"""Outbound targets authorized by the deployment operator, never by a query."""

import os

from foundation.outbound import resolve_outbound_target


def resolve_gerrit_target(url):
    return resolve_outbound_target(url, allowed_private_hosts={
        host.strip().lower().rstrip('.')
        for host in os.getenv('GMS_GERRIT_ALLOWED_HOSTS', '').split(',') if host.strip()
    })
