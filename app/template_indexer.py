from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class TemplateRecord:
    template_id: str
    name: str
    mode: str
    description: str
    icon: str
    version: str
    yaml_path: Path
    mtime: int
    size: int


def _safe_str(v: Any) -> str:
    if v is None:
        return ""
    return str(v)


def scan_templates(data_root: str) -> list[TemplateRecord]:
    root = Path(data_root).resolve() / "templates"
    if not root.exists() or not root.is_dir():
        return []

    out: list[TemplateRecord] = []
    for template_yaml in sorted(root.rglob("template.y*ml")):
        template_dir = template_yaml.parent
        template_id = template_dir.name
        try:
            st = template_yaml.stat()
        except FileNotFoundError:
            continue

        try:
            obj = yaml.safe_load(template_yaml.read_text("utf-8")) or {}
        except Exception:
            obj = {}

        app = obj.get("app") or {}
        if not isinstance(app, dict):
            app = {}

        name = _safe_str(app.get("name")) or template_id
        mode = _safe_str(app.get("mode"))
        description = _safe_str(app.get("description"))
        icon = _safe_str(app.get("icon"))

        version = ""
        version_file = template_dir / "version"
        if version_file.exists():
            try:
                version = version_file.read_text("utf-8").strip()
            except Exception:
                version = ""
        if not version:
            version = _safe_str(obj.get("version"))
        if not version:
            version = time.strftime("%Y.%m.%d", time.gmtime(int(st.st_mtime)))

        out.append(
            TemplateRecord(
                template_id=template_id,
                name=name,
                mode=mode,
                description=description,
                icon=icon,
                version=version,
                yaml_path=template_yaml,
                mtime=int(st.st_mtime),
                size=int(st.st_size),
            )
        )

    return out


def relpath_under_data_root(data_root: str, path: Path) -> str:
    return os.path.relpath(str(path), str(Path(data_root).resolve()))
