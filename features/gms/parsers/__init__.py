"""Source parser registry and shared public entry points."""

from .critical import bulletin_content_hash, bulletin_month_identity, parse_gms_bulletins
from .downloads import parse_cts_downloads, parse_gms_downloads, parse_gts_downloads, parse_vts_downloads
from .mainline import mainline_entries, parse_mainline_package, parse_mainline_release_notes
from .requirements import parse_gms_requirements


__all__ = [
    'PARSERS',
    'bulletin_content_hash',
    'bulletin_month_identity',
    'mainline_entries',
    'parse_mainline_package',
]

PARSERS = {
    'cts_downloads': parse_cts_downloads,
    'vts_downloads': parse_vts_downloads,
    'gts_downloads': parse_gts_downloads,
    'gms_downloads': parse_gms_downloads,
    'gms_requirements': parse_gms_requirements,
    'mainline_release_notes': parse_mainline_release_notes,
    'gms_bulletins': parse_gms_bulletins,
}
