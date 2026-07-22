"""SQLite schema for the personal knowledge store."""

from __future__ import annotations

import sqlite3


KNOWLEDGE_REQUIRED_TABLES = frozenset({
    "knowledge_spaces", "knowledge_nodes", "knowledge_docs", "knowledge_tags",
    "knowledge_doc_tags", "knowledge_attachments", "knowledge_links",
    "knowledge_fts", "knowledge_doc_versions",
})


def initialize_knowledge_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS knowledge_spaces (
            space_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            icon TEXT DEFAULT '',
            sort_order INTEGER DEFAULT 0,
            created_at TEXT DEFAULT '',
            updated_at TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS knowledge_nodes (
            node_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            space_id TEXT NOT NULL,
            parent_id TEXT DEFAULT '',
            type TEXT NOT NULL CHECK(type IN ('folder','doc')),
            title TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            archived INTEGER DEFAULT 0,
            created_at TEXT DEFAULT '',
            updated_at TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS knowledge_docs (
            doc_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            space_id TEXT NOT NULL,
            node_id TEXT NOT NULL UNIQUE,
            content_md TEXT DEFAULT '',
            raw_content TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            source TEXT DEFAULT 'manual',
            source_file TEXT DEFAULT '',
            favorite INTEGER DEFAULT 0,
            created_at TEXT DEFAULT '',
            updated_at TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS knowledge_tags (
            tag_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            UNIQUE(user_id, name)
        );
        CREATE TABLE IF NOT EXISTS knowledge_doc_tags (
            doc_id TEXT NOT NULL,
            tag_id TEXT NOT NULL,
            PRIMARY KEY(doc_id, tag_id)
        );
        CREATE TABLE IF NOT EXISTS knowledge_attachments (
            attachment_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            doc_id TEXT NOT NULL,
            original_name TEXT NOT NULL,
            path TEXT NOT NULL,
            mime TEXT DEFAULT '',
            size INTEGER DEFAULT 0,
            extracted_text TEXT DEFAULT '',
            created_at TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS knowledge_links (
            link_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            doc_id TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            title TEXT DEFAULT '',
            url TEXT DEFAULT '',
            created_at TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_knowledge_spaces_user
            ON knowledge_spaces(user_id, sort_order);
        CREATE INDEX IF NOT EXISTS idx_knowledge_nodes_tree
            ON knowledge_nodes(user_id, space_id, parent_id, sort_order);
        CREATE INDEX IF NOT EXISTS idx_knowledge_docs_user
            ON knowledge_docs(user_id, updated_at);
        CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
            doc_id UNINDEXED,
            user_id UNINDEXED,
            space_id UNINDEXED,
            title,
            content_md,
            raw_content,
            summary,
            tags,
            attachments,
            links
        );
        """
    )
    conn.commit()
