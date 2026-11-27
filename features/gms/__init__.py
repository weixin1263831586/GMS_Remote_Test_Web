"""GMS updates, source scanning and official-reference lookup public surface."""

from .mainline_records import query_exemption_match as query_mainline_exemption_match
from .mainline_records import query_security_patches_by_cve as query_mainline_security_patches
from .recall import extract_reference_identifiers, recall_mainline_references
from .repository import init_db as init_gms_update_db
from .repository import initialize as initialize_gms_update
from .requirement_lookup import policy_lookup_target


__all__ = [
    'extract_reference_identifiers',
    'init_gms_update_db',
    'initialize_gms_update',
    'policy_lookup_target',
    'query_mainline_exemption_match',
    'query_mainline_security_patches',
    'recall_mainline_references',
]
