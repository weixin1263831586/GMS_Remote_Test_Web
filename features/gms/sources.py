"""The only scan source registry and task-to-source mapping."""
from .models import SourceConfig


SOURCES: tuple[SourceConfig, ...] = (
    SourceConfig(
        key='cts_downloads',
        name='CTS Downloads',
        url='https://source.android.com/docs/compatibility/cts/downloads',
        category='test_suite',
        parser='cts_downloads',
    ),
    SourceConfig(
        key='vts_downloads',
        name='VTS, GSIs, and GKIs',
        url='https://docs.partner.android.com/gms/testing/vts',
        category='test_suite',
        parser='vts_downloads',
        auth_required=True,
    ),
    SourceConfig(
        key='gts_downloads',
        name='GTS Downloads',
        url='https://docs.partner.android.com/gms/testing/gts',
        category='test_suite',
        parser='gts_downloads',
        auth_required=True,
    ),
    SourceConfig(
        key='gms_downloads',
        name='GMS Downloads',
        url='https://docs.partner.android.com/gms/building/integrating/gms-download',
        category='gms_package',
        parser='gms_downloads',
        auth_required=True,
    ),
    SourceConfig(
        key='gms_requirements',
        name='GMS Certification Requirements',
        url='https://docs.partner.android.com/gms/policies/domains/reqs',
        category='certification_requirement',
        parser='gms_requirements',
        auth_required=True,
    ),
    SourceConfig(
        key='mainline_preload',
        name='Mainline PRELOAD Release Notes',
        url='https://docs.partner.android.com/mainline/release/release-notes?authuser=2',
        category='mainline_package',
        parser='mainline_release_notes',
        auth_required=True,
    ),
    SourceConfig(
        key='gms_bulletins',
        name='Critical Fix Supplementary Bulletins',
        url='https://docs.partner.android.com/partners/releases/cf',
        category='gms_bulletin',
        parser='gms_bulletins',
        auth_required=True,
    ),
)


SOURCE_KEYS = tuple(s.key for s in SOURCES)
MAINLINE = 'mainline_preload'
TASK_SOURCES = {
    'requirements': ('gms_requirements',),
    'packages': ('gms_downloads',),
    'mainline': (MAINLINE,),
    'artifacts': ('cts_downloads', 'vts_downloads', 'gts_downloads', 'gms_downloads'),
    'bulletins': ('gms_bulletins',),
    'exemptions': (MAINLINE,),
    'security_patches': (MAINLINE,),
}


def select_sources(keys=None):
    wanted = set(keys or SOURCE_KEYS)
    if not wanted <= set(SOURCE_KEYS):
        raise ValueError('unknown scan source')
    return [s for s in SOURCES if s.key in wanted]


def sources_for_tasks(tasks):
    if not tasks or set(tasks) - TASK_SOURCES.keys():
        raise ValueError('invalid scan task selection')
    wanted = {source for task in tasks for source in TASK_SOURCES[task]}
    return [key for key in SOURCE_KEYS if key in wanted]
