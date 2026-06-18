#!/usr/bin/env python3
"""Scan Android/GMS documentation for test-suite and certification updates."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lxml import html


DEFAULT_DB_PATH = Path('data/gms_update_monitor.sqlite3')
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SourceConfig:
    key: str
    name: str
    url: str
    category: str
    parser: str
    auth_required: bool = False


@dataclass(frozen=True)
class FetchedDocument:
    source: SourceConfig
    doc: html.HtmlElement
    title: str
    content_hash: str
    status_code: int
    final_url: str


@dataclass(frozen=True)
class LinkInfo:
    text: str
    url: str


@dataclass(frozen=True)
class ArtifactRecord:
    source_key: str
    item_key: str
    suite_type: str
    android_version: str
    release_name: str
    artifact_kind: str
    arch: str
    file_name: str
    download_url: str
    release_notes_url: str
    user_guide_url: str
    ci_build_id: str
    target_platform: str
    description: str
    section_path: str
    content_hash: str


@dataclass(frozen=True)
class GmsPackageRecord:
    source_key: str
    item_key: str
    section: str
    android_version: str
    release_notes_url: str
    file_name: str
    download_url: str
    required_from: str
    partner_gerrit_tag: str
    partner_gerrit_url: str
    description: str
    content_hash: str


@dataclass(frozen=True)
class RequirementSectionRecord:
    source_key: str
    section_key: str
    level: int
    number: str
    title: str
    path: str
    text_excerpt: str
    table_count: int
    link_count: int
    content_hash: str


@dataclass(frozen=True)
class RequirementTableRowRecord:
    source_key: str
    row_key: str
    section_key: str
    section_title: str
    table_index: int
    row_index: int
    headers_json: str
    values_json: str
    row_text: str
    content_hash: str


@dataclass(frozen=True)
class RequirementVersionTagRecord:
    source_key: str
    tag_key: str
    android_version: str
    change_kind: str
    section_key: str
    section_title: str
    requirement_ids: str
    text_excerpt: str
    content_hash: str


@dataclass
class ParsedSource:
    artifacts: list[ArtifactRecord] = None  # type: ignore[assignment]
    gms_packages: list[GmsPackageRecord] = None  # type: ignore[assignment]
    requirement_sections: list[RequirementSectionRecord] = None  # type: ignore[assignment]
    requirement_table_rows: list[RequirementTableRowRecord] = None  # type: ignore[assignment]
    requirement_version_tags: list[RequirementVersionTagRecord] = None  # type: ignore[assignment]

    def __post_init__(self):
        for field_name in ('artifacts', 'gms_packages', 'requirement_sections', 'requirement_table_rows', 'requirement_version_tags'):
            if getattr(self, field_name) is None:
                object.__setattr__(self, field_name, [])


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
)

