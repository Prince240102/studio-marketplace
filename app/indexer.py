from __future__ import annotations

import hashlib
import os
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from packaging.version import InvalidVersion, Version


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_zip_member(zf: zipfile.ZipFile, name: str) -> bytes | None:
    try:
        return zf.read(name)
    except KeyError:
        return None


def _pick_manifest_member(zf: zipfile.ZipFile) -> str | None:
    # difypkg is expected to contain manifest.yaml at root.
    for candidate in ("manifest.yaml", "manifest.yml"):
        if candidate in zf.namelist():
            return candidate
    return None


def _safe_str(v: Any) -> str:
    if v is None:
        return ""
    return str(v)


def _infer_category_from_path(pkg_path: Path) -> str | None:
    parts = [p.lower() for p in pkg_path.parts]
    if "triggers" in parts:
        return "trigger"
    if "tools" in parts:
        return "tool"
    if "models" in parts:
        return "model"
    if "datasources" in parts:
        return "datasource"
    if "agent-strategies" in parts:
        return "agent-strategy"
    if "extensions" in parts:
        return "extension"
    return None


@dataclass(frozen=True)
class VersionRecord:
    org: str
    name: str
    version: str
    unique_identifier: str
    plugin_id: str
    pkg_path: Path
    extracted_dir: Path | None
    checksum: str
    created_at: str
    category: str
    label: dict[str, str]
    description: dict[str, str]
    tags: list[str]
    icon_path: str
    icon_dark_path: str | None
    repo: str


@dataclass
class PluginRecord:
    plugin_id: str
    org: str
    name: str
    category: str
    latest: VersionRecord
    versions: list[VersionRecord]


class MarketplaceIndex:
    def __init__(self) -> None:
        self.built_at: float = 0.0
        self.data_root: Path | None = None
        self.by_unique_identifier: dict[str, VersionRecord] = {}
        self.by_plugin_id: dict[str, PluginRecord] = {}

    def load_from_db(self, db_path: str) -> bool:
        from .index_db import load_index

        loaded = load_index(db_path)
        if not loaded:
            return False
        by_unique, by_plugin, built_at, data_root = loaded
        self.built_at = built_at
        self.data_root = data_root
        self.by_unique_identifier = by_unique
        self.by_plugin_id = by_plugin
        return True

    def build(self, data_root: str) -> None:
        root = Path(data_root).resolve()
        if not root.exists():
            raise FileNotFoundError(f"MARKETPLACE_DATA_ROOT does not exist: {root}")

        by_unique: dict[str, VersionRecord] = {}
        by_plugin: dict[str, list[VersionRecord]] = {}

        pkg_paths = sorted(root.rglob("*.difypkg"), key=lambda p: p.stat().st_mtime)

        for pkg_path in pkg_paths:
            # Skip nested packages inside extracted directories.
            # Many extracted plugin folders include an embedded *.difypkg; we only
            # want the top-level package stored next to the extracted folder.
            parent = pkg_path.parent
            if (parent / "manifest.yaml").exists() or (
                parent / "manifest.yml"
            ).exists():
                continue
            rec = self._record_from_pkg(pkg_path)
            if rec is None:
                continue
            by_unique[rec.unique_identifier] = rec
            by_plugin.setdefault(rec.plugin_id, []).append(rec)

        plugins: dict[str, PluginRecord] = {}
        for plugin_id, versions in by_plugin.items():

            def sort_key(r: VersionRecord):
                try:
                    return (Version(r.version), r.pkg_path.stat().st_mtime)
                except InvalidVersion:
                    return (Version("0"), r.pkg_path.stat().st_mtime)

            versions_sorted = sorted(versions, key=sort_key, reverse=True)
            latest = versions_sorted[0]
            plugins[plugin_id] = PluginRecord(
                plugin_id=plugin_id,
                org=latest.org,
                name=latest.name,
                category=latest.category,
                latest=latest,
                versions=versions_sorted,
            )

        self.built_at = time.time()
        self.data_root = root
        self.by_unique_identifier = by_unique
        self.by_plugin_id = plugins

    def all_versions(self) -> list[VersionRecord]:
        return list(self.by_unique_identifier.values())

    def _record_from_pkg(self, pkg_path: Path) -> VersionRecord | None:
        try:
            with zipfile.ZipFile(pkg_path, "r") as zf:
                manifest_member = _pick_manifest_member(zf)
                if not manifest_member:
                    return None
                manifest_bytes = _read_zip_member(zf, manifest_member)
                if not manifest_bytes:
                    return None
        except zipfile.BadZipFile:
            return None

        try:
            manifest = yaml.safe_load(manifest_bytes) or {}
        except Exception:
            return None

        org = _safe_str(manifest.get("author"))
        name = _safe_str(manifest.get("name"))
        version = _safe_str(manifest.get("version"))
        if not org or not name or not version:
            return None

        plugin_id = f"{org}/{name}"
        unique_identifier = f"{plugin_id}:{version}"

        extracted_dir = pkg_path.parent / pkg_path.stem
        if not extracted_dir.exists() or not extracted_dir.is_dir():
            extracted_dir = None

        checksum = _sha256_file(pkg_path)

        created_at_raw = manifest.get("created_at")
        created_at = _safe_str(created_at_raw)
        if not created_at:
            created_at = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(pkg_path.stat().st_mtime)
            )

        category = _safe_str(manifest.get("category")).strip()
        if not category:
            # Some plugins (notably triggers) omit category in manifest.
            if "trigger" in manifest:
                category = "trigger"
            elif "tool" in manifest:
                category = "tool"
            elif "datasource" in manifest:
                category = "datasource"
            elif "model" in manifest:
                category = "model"
            elif "agent_strategy" in manifest or "agent-strategy" in manifest:
                category = "agent-strategy"
            else:
                category = _infer_category_from_path(pkg_path) or "extension"
        label = manifest.get("label") or {}
        if not isinstance(label, dict):
            label = {}
        description = manifest.get("description") or {}
        if not isinstance(description, dict):
            description = {}
        tags = manifest.get("tags") or []
        if not isinstance(tags, list):
            tags = []

        icon_path = _safe_str(manifest.get("icon"))
        icon_dark_path = manifest.get("icon_dark")
        icon_dark_path = _safe_str(icon_dark_path) if icon_dark_path else None
        repo = _safe_str(manifest.get("repo"))

        return VersionRecord(
            org=org,
            name=name,
            version=version,
            unique_identifier=unique_identifier,
            plugin_id=plugin_id,
            pkg_path=pkg_path,
            extracted_dir=extracted_dir,
            checksum=checksum,
            created_at=created_at,
            category=category,
            label=label,
            description=description,
            tags=[_safe_str(t) for t in tags if _safe_str(t)],
            icon_path=icon_path,
            icon_dark_path=icon_dark_path,
            repo=repo,
        )

    def get_pkg(self, unique_identifier: str) -> VersionRecord | None:
        return self.by_unique_identifier.get(unique_identifier)

    def get_plugin(self, plugin_id: str) -> PluginRecord | None:
        return self.by_plugin_id.get(plugin_id)


def load_manifest_from_extracted(extracted_dir: Path) -> dict[str, Any] | None:
    for name in ("manifest.yaml", "manifest.yml"):
        path = extracted_dir / name
        if path.exists():
            try:
                return yaml.safe_load(path.read_text("utf-8")) or {}
            except Exception:
                return None
    return None


def read_asset_bytes(version: VersionRecord, asset_rel_path: str) -> bytes | None:
    rel = asset_rel_path.lstrip("/")
    candidates = [rel]
    # Many difypkg manifests use "icon.svg" while the file is stored under "_assets/icon.svg".
    if "/" not in rel and not rel.startswith("_assets/"):
        candidates.append(f"_assets/{rel}")

    # Prefer extracted dir
    if version.extracted_dir:
        base = version.extracted_dir.resolve()
        for c in candidates:
            candidate = (base / c).resolve()
            try:
                candidate.relative_to(base)
            except Exception:
                continue
            if candidate.exists() and candidate.is_file():
                return candidate.read_bytes()

    # Fallback to zip member
    try:
        with zipfile.ZipFile(version.pkg_path, "r") as zf:
            for c in candidates:
                data = _read_zip_member(zf, c)
                if data is not None:
                    return data
    except Exception:
        return None
