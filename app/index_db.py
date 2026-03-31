from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from .indexer import PluginRecord, VersionRecord
from .template_indexer import TemplateRecord


SCHEMA_VERSION = 4


def compute_fs_stats(data_root: str) -> dict[str, int]:
    """Compute cheap stats for difypkg inventory under data_root."""
    root = Path(data_root).resolve()
    count = 0
    max_mtime = 0
    total_size = 0
    for pkg_path in root.rglob("*.difypkg"):
        parent = pkg_path.parent
        # Skip nested packages inside extracted directories
        if (parent / "manifest.yaml").exists() or (parent / "manifest.yml").exists():
            continue
        try:
            st = pkg_path.stat()
        except FileNotFoundError:
            continue
        count += 1
        mt = int(st.st_mtime)
        if mt > max_mtime:
            max_mtime = mt
        total_size += int(st.st_size)
    return {
        "difypkg_count": count,
        "difypkg_max_mtime": max_mtime,
        "difypkg_total_size": total_size,
    }


def read_meta(db_path: str) -> dict[str, str] | None:
    if not db_path or not os.path.exists(db_path):
        return None
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT key, value FROM meta").fetchall()
        return {str(r[0]): str(r[1]) for r in rows}
    finally:
        conn.close()


def db_matches_fs(db_path: str, data_root: str) -> bool:
    meta = read_meta(db_path)
    if not meta:
        return False
    try:
        if int(meta.get("schema_version", "0")) != SCHEMA_VERSION:
            return False
    except ValueError:
        return False

    fs = compute_fs_stats(data_root)
    for k, v in fs.items():
        try:
            if int(meta.get(k, "-1")) != int(v):
                return False
        except ValueError:
            return False
    return True


def _connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS versions (
          unique_identifier TEXT PRIMARY KEY,
          plugin_id TEXT NOT NULL,
          org TEXT NOT NULL,
          name TEXT NOT NULL,
          version TEXT NOT NULL,
          category TEXT NOT NULL,
          checksum TEXT NOT NULL,
          created_at TEXT NOT NULL,
          pkg_path TEXT NOT NULL,
          extracted_dir TEXT,
          label_json TEXT NOT NULL,
          description_json TEXT NOT NULL,
          tags_json TEXT NOT NULL,
          icon_path TEXT NOT NULL,
          icon_dark_path TEXT,
          repo TEXT NOT NULL,
          mtime INTEGER NOT NULL,
          size INTEGER NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS plugins (
          plugin_id TEXT PRIMARY KEY,
          org TEXT NOT NULL,
          name TEXT NOT NULL,
          category TEXT NOT NULL,
          latest_unique_identifier TEXT NOT NULL,
          latest_version TEXT NOT NULL,
          label_json TEXT NOT NULL,
          description_json TEXT NOT NULL,
          tags_json TEXT NOT NULL,
          icon_path TEXT NOT NULL,
          icon_dark_path TEXT,
          repo TEXT NOT NULL,
          search_text TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS templates (
          template_id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          mode TEXT NOT NULL,
          description TEXT NOT NULL,
          icon TEXT NOT NULL,
          version TEXT NOT NULL,
          yaml_path TEXT NOT NULL,
          mtime INTEGER NOT NULL,
          size INTEGER NOT NULL,
          search_text TEXT NOT NULL
        )
        """
    )

    conn.commit()

    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
        ("schema_version", str(SCHEMA_VERSION)),
    )
    conn.commit()


def write_index(
    db_path: str,
    data_root: str,
    versions: list[VersionRecord],
    templates: list[TemplateRecord] | None = None,
) -> None:
    conn = _connect(db_path)
    init_db(conn)
    now = int(time.time())

    conn.execute("DELETE FROM versions")
    conn.execute("DELETE FROM plugins")
    conn.execute("DELETE FROM templates")
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
        ("data_root", str(Path(data_root).resolve())),
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
        ("built_at", str(now)),
    )

    fs = compute_fs_stats(data_root)
    for k, v in fs.items():
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            (k, str(int(v))),
        )

    rows = []
    root = Path(data_root).resolve()
    for v in versions:
        st = v.pkg_path.stat()
        pkg_rel = os.path.relpath(str(v.pkg_path), str(root))
        extracted_rel = None
        if v.extracted_dir:
            extracted_rel = os.path.relpath(str(v.extracted_dir), str(root))
        rows.append(
            (
                v.unique_identifier,
                v.plugin_id,
                v.org,
                v.name,
                v.version,
                v.category,
                v.checksum,
                v.created_at,
                pkg_rel,
                extracted_rel,
                json.dumps(v.label, ensure_ascii=True, sort_keys=True),
                json.dumps(v.description, ensure_ascii=True, sort_keys=True),
                json.dumps(v.tags, ensure_ascii=True),
                v.icon_path,
                v.icon_dark_path,
                v.repo,
                int(st.st_mtime),
                int(st.st_size),
            )
        )

    conn.executemany(
        """
        INSERT INTO versions(
          unique_identifier, plugin_id, org, name, version, category, checksum, created_at,
          pkg_path, extracted_dir, label_json, description_json, tags_json,
          icon_path, icon_dark_path, repo, mtime, size
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()

    # Build plugin-level rows (latest per plugin_id by mtime, then version string).
    by_plugin: dict[str, VersionRecord] = {}
    for v in versions:
        current = by_plugin.get(v.plugin_id)
        if not current:
            by_plugin[v.plugin_id] = v
            continue
        try:
            cm = int(current.pkg_path.stat().st_mtime)
        except Exception:
            cm = 0
        try:
            nm = int(v.pkg_path.stat().st_mtime)
        except Exception:
            nm = 0
        if (nm, v.version) > (cm, current.version):
            by_plugin[v.plugin_id] = v

    plugin_rows = []
    for plugin_id, v in by_plugin.items():
        label_json = json.dumps(v.label, ensure_ascii=True, sort_keys=True)
        desc_json = json.dumps(v.description, ensure_ascii=True, sort_keys=True)
        tags_json = json.dumps(v.tags, ensure_ascii=True)
        search_text = " ".join(
            [
                plugin_id,
                v.org,
                v.name,
                label_json,
                desc_json,
                tags_json,
            ]
        ).lower()
        plugin_rows.append(
            (
                plugin_id,
                v.org,
                v.name,
                v.category,
                v.unique_identifier,
                v.version,
                label_json,
                desc_json,
                tags_json,
                v.icon_path,
                v.icon_dark_path,
                v.repo,
                search_text,
            )
        )

    conn.executemany(
        """
        INSERT INTO plugins(
          plugin_id, org, name, category,
          latest_unique_identifier, latest_version,
          label_json, description_json, tags_json,
          icon_path, icon_dark_path, repo, search_text
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        plugin_rows,
    )

    # Write templates.
    if templates:
        for t in templates:
            yaml_rel = os.path.relpath(str(t.yaml_path), str(root))
            search_text = " ".join(
                [t.template_id, t.name, t.mode, t.description]
            ).lower()
            conn.execute(
                """
                INSERT INTO templates(
                  template_id, name, mode, description, icon,
                  version, yaml_path, mtime, size, search_text
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    t.template_id,
                    t.name,
                    t.mode,
                    t.description,
                    t.icon,
                    t.version,
                    yaml_rel,
                    t.mtime,
                    t.size,
                    search_text,
                ),
            )

    conn.commit()
    conn.close()


def _like_escape(s: str) -> str:
    return s.replace("%", "\\%").replace("_", "\\_")


def query_plugins(
    db_path: str,
    *,
    query: str,
    category: str,
    tags: list[str],
    exclude: set[str],
    page: int,
    page_size: int,
) -> tuple[list[sqlite3.Row], int]:
    conn = _connect(db_path)
    try:
        where = []
        params: list[object] = []
        if category:
            where.append("category = ?")
            params.append(category)
        if query:
            where.append("search_text LIKE ? ESCAPE '\\'")
            params.append(f"%{_like_escape(query.lower())}%")
        if exclude:
            placeholders = ",".join(["?"] * len(exclude))
            where.append(f"plugin_id NOT IN ({placeholders})")
            params.extend(list(exclude))
        if tags:
            # tags_json is a JSON array string; do a simple contains match.
            # Any-match semantics.
            tag_w = []
            for t in tags:
                tag_w.append("tags_json LIKE ?")
                params.append(f'%"{_like_escape(str(t))}"%')
            where.append("(" + " OR ".join(tag_w) + ")")

        where_sql = (" WHERE " + " AND ".join(where)) if where else ""

        total = conn.execute(
            f"SELECT COUNT(1) AS c FROM plugins{where_sql}", params
        ).fetchone()[0]

        offset = max(0, (page - 1) * page_size)
        rows = conn.execute(
            f"SELECT * FROM plugins{where_sql} ORDER BY category, plugin_id LIMIT ? OFFSET ?",
            [*params, page_size, offset],
        ).fetchall()
        return rows, int(total)
    finally:
        conn.close()


def get_plugin_row(db_path: str, plugin_id: str) -> sqlite3.Row | None:
    conn = _connect(db_path)
    try:
        return conn.execute(
            "SELECT * FROM plugins WHERE plugin_id = ?",
            (plugin_id,),
        ).fetchone()
    finally:
        conn.close()


def get_version_row(db_path: str, unique_identifier: str) -> sqlite3.Row | None:
    conn = _connect(db_path)
    try:
        return conn.execute(
            "SELECT * FROM versions WHERE unique_identifier = ?",
            (unique_identifier,),
        ).fetchone()
    finally:
        conn.close()


def list_versions(
    db_path: str, plugin_id: str, page: int, page_size: int
) -> list[sqlite3.Row]:
    conn = _connect(db_path)
    try:
        offset = max(0, (page - 1) * page_size)
        return conn.execute(
            "SELECT * FROM versions WHERE plugin_id = ? ORDER BY mtime DESC, version DESC LIMIT ? OFFSET ?",
            (plugin_id, page_size, offset),
        ).fetchall()
    finally:
        conn.close()


def query_templates(
    db_path: str,
    *,
    query: str,
    mode: str,
    page: int,
    page_size: int,
) -> tuple[list[sqlite3.Row], int]:
    conn = _connect(db_path)
    try:
        where = []
        params: list[object] = []
        if mode:
            where.append("mode = ?")
            params.append(mode)
        if query:
            where.append("search_text LIKE ? ESCAPE '\\'")
            params.append(f"%{_like_escape(query.lower())}%")

        where_sql = (" WHERE " + " AND ".join(where)) if where else ""

        total = conn.execute(
            f"SELECT COUNT(1) AS c FROM templates{where_sql}", params
        ).fetchone()[0]

        offset = max(0, (page - 1) * page_size)
        rows = conn.execute(
            f"SELECT * FROM templates{where_sql} ORDER BY name LIMIT ? OFFSET ?",
            [*params, page_size, offset],
        ).fetchall()
        return rows, int(total)
    finally:
        conn.close()


def get_template_row(db_path: str, template_id: str) -> sqlite3.Row | None:
    conn = _connect(db_path)
    try:
        return conn.execute(
            "SELECT * FROM templates WHERE template_id = ?",
            (template_id,),
        ).fetchone()
    finally:
        conn.close()


def resolve_template_path(db_path: str, template_id: str) -> Path | None:
    row = get_template_row(db_path, template_id)
    if not row:
        return None
    meta = read_meta(db_path)
    if not meta:
        return None
    root = Path(str(meta.get("data_root", "")))
    return root / str(row["yaml_path"])


def load_index(
    db_path: str,
) -> tuple[dict[str, VersionRecord], dict[str, PluginRecord], float, Path] | None:
    if not db_path:
        return None
    if not os.path.exists(db_path):
        return None

    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if not row or int(row[0]) != SCHEMA_VERSION:
            return None

        root_row = conn.execute(
            "SELECT value FROM meta WHERE key='data_root'"
        ).fetchone()
        built_row = conn.execute(
            "SELECT value FROM meta WHERE key='built_at'"
        ).fetchone()
        if not root_row or not built_row:
            return None
        data_root = Path(str(root_row[0]))
        built_at = float(int(built_row[0]))

        by_unique: dict[str, VersionRecord] = {}
        by_plugin_versions: dict[str, list[VersionRecord]] = {}

        for r in conn.execute("SELECT * FROM versions"):
            label = json.loads(r["label_json"]) if r["label_json"] else {}
            desc = json.loads(r["description_json"]) if r["description_json"] else {}
            tags = json.loads(r["tags_json"]) if r["tags_json"] else []

            rec = VersionRecord(
                org=r["org"],
                name=r["name"],
                version=r["version"],
                unique_identifier=r["unique_identifier"],
                plugin_id=r["plugin_id"],
                pkg_path=Path(r["pkg_path"]),
                extracted_dir=Path(r["extracted_dir"]) if r["extracted_dir"] else None,
                checksum=r["checksum"],
                created_at=r["created_at"],
                category=r["category"],
                label=label,
                description=desc,
                tags=tags,
                icon_path=r["icon_path"],
                icon_dark_path=r["icon_dark_path"],
                repo=r["repo"],
            )

            by_unique[rec.unique_identifier] = rec
            by_plugin_versions.setdefault(rec.plugin_id, []).append(rec)

        by_plugin: dict[str, PluginRecord] = {}
        for plugin_id, versions in by_plugin_versions.items():
            versions_sorted = sorted(
                versions,
                key=lambda v: (
                    v.version,
                    v.pkg_path.stat().st_mtime if v.pkg_path.exists() else 0,
                ),
                reverse=True,
            )
            latest = versions_sorted[0]
            by_plugin[plugin_id] = PluginRecord(
                plugin_id=plugin_id,
                org=latest.org,
                name=latest.name,
                category=latest.category,
                latest=latest,
                versions=versions_sorted,
            )

        return by_unique, by_plugin, built_at, data_root
    finally:
        conn.close()
