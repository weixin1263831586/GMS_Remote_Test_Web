#!/usr/bin/env python3
"""Scan Android/GMS documentation for test-suite and certification updates."""

from __future__ import annotations

from dataclasses import dataclass, field

from lxml import html


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
class MainlinePackageRecord:
    source_key: str
    item_key: str
    year: str
    month: str
    month_label: str
    preload_version: str
    notes_url: str
    partner_zip_build_id: str
    ci_build_url: str
    partner_zip_label: str
    content_hash: str


@dataclass(frozen=True)
class BulletinPatchRecord:
    source_key: str
    item_key: str
    year: str
    patch_id: str
    patch_kind: str
    android_versions: str
    description: str
    bulletin_month: str
    deadline: str
    bulletin_url: str
    content_hash: str
    detail_text: str = ''
    detail_status: str = 'not_scanned'
    detail_fetched_at: str = ''
    patch_rows_json: str = ''


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
    mainline_packages: list[MainlinePackageRecord] = None  # type: ignore[assignment]
    bulletin_patches: list[BulletinPatchRecord] = None  # type: ignore[assignment]
    requirement_sections: list[RequirementSectionRecord] = None  # type: ignore[assignment]
    requirement_table_rows: list[RequirementTableRowRecord] = None  # type: ignore[assignment]
    requirement_version_tags: list[RequirementVersionTagRecord] = None  # type: ignore[assignment]
    bulletin_month_status: dict[str, str] = field(default_factory=dict)
    bulletin_month_diagnostics: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        for field_name in (
            'artifacts',
            'gms_packages',
            'mainline_packages',
            'bulletin_patches',
            'requirement_sections',
            'requirement_table_rows',
            'requirement_version_tags',
        ):
            if getattr(self, field_name) is None:
                object.__setattr__(self, field_name, [])
