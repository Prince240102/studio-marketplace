from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates

from .indexer import MarketplaceIndex, read_asset_bytes
from .markdown_render import render_markdown
from .settings import settings


app = FastAPI(title="Private Dify Marketplace", version="0.1.0")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

if settings.cors_allow_origins:
    origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

index = MarketplaceIndex()
index_lock = threading.Lock()

db_state = {
    "usable": False,
    "checked_at": 0.0,
}


def _db_path() -> str:
    return settings.index_db_path


def _db_check(force: bool = False) -> bool:
    """Return True if sqlite index DB is usable for queries."""
    if not settings.index_db_path:
        return False
    if not os.path.exists(settings.index_db_path):
        return False

    now = time.time()
    if (
        not force
        and db_state["checked_at"]
        and now - float(db_state["checked_at"]) < 30
    ):
        return bool(db_state["usable"])

    if not settings.validate_index_db:
        db_state["usable"] = True
        db_state["checked_at"] = now
        return True

    try:
        from .index_db import db_matches_fs

        ok = db_matches_fs(settings.index_db_path, settings.data_root)
    except Exception:
        ok = False

    db_state["usable"] = bool(ok)
    db_state["checked_at"] = now
    return bool(ok)


def _plugin_row_to_card(r: Any) -> dict[str, Any]:
    label = json.loads(r["label_json"]) if r["label_json"] else {}
    desc = json.loads(r["description_json"]) if r["description_json"] else {}
    tags = json.loads(r["tags_json"]) if r["tags_json"] else []
    return {
        "type": "plugin",
        "org": r["org"],
        "name": r["name"],
        "plugin_id": r["plugin_id"],
        "version": r["latest_version"],
        "latest_version": r["latest_version"],
        "latest_package_identifier": r["latest_unique_identifier"],
        "icon": f"/api/v1/plugins/{r['org']}/{r['name']}/icon",
        "verified": False,
        "label": label,
        "brief": desc,
        "description": desc,
        "introduction": "",
        "repository": r["repo"],
        "category": r["category"],
        "install_count": 0,
        "endpoint": {"settings": []},
        "tags": [{"name": t} for t in tags],
        "badges": [],
        "verification": {"authorized_category": "community"},
        "from": "marketplace",
    }


def _version_row_to_record(r: Any):
    from .indexer import VersionRecord

    def resolve_path(p: str) -> Path:
        if not p:
            return Path(settings.data_root)
        pp = Path(str(p))
        if pp.is_absolute() and pp.exists():
            return pp
        return Path(settings.data_root) / pp

    label = json.loads(r["label_json"]) if r["label_json"] else {}
    desc = json.loads(r["description_json"]) if r["description_json"] else {}
    tags = json.loads(r["tags_json"]) if r["tags_json"] else []
    return VersionRecord(
        org=r["org"],
        name=r["name"],
        version=r["version"],
        unique_identifier=r["unique_identifier"],
        plugin_id=r["plugin_id"],
        pkg_path=resolve_path(str(r["pkg_path"])),
        extracted_dir=(
            resolve_path(str(r["extracted_dir"])) if r["extracted_dir"] else None
        ),
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


def _ensure_index() -> None:
    with index_lock:
        if index.data_root is None:
            if _db_check():
                return
            if settings.index_db_path:
                try:
                    from .index_db import db_matches_fs

                    if (not settings.validate_index_db) or db_matches_fs(
                        settings.index_db_path, settings.data_root
                    ):
                        if index.load_from_db(settings.index_db_path):
                            return
                except Exception:
                    pass
            index.build(settings.data_root)
            if settings.index_db_path:
                try:
                    from .index_db import write_index
                    from .template_indexer import scan_templates

                    templates = scan_templates(settings.data_root)
                    write_index(
                        settings.index_db_path,
                        settings.data_root,
                        index.all_versions(),
                        templates,
                    )
                except Exception:
                    # DB is optional; continue with in-memory index.
                    pass


def _ensure_index_fresh() -> None:
    from .template_indexer import scan_templates

    with index_lock:
        if index.data_root is None and _db_check():
            return
        if index.data_root is None:
            if settings.index_db_path:
                try:
                    from .index_db import db_matches_fs

                    if (not settings.validate_index_db) or db_matches_fs(
                        settings.index_db_path, settings.data_root
                    ):
                        if index.load_from_db(settings.index_db_path):
                            return
                except Exception:
                    pass
            index.build(settings.data_root)
            templates = scan_templates(settings.data_root)
            if settings.index_db_path:
                try:
                    from .index_db import write_index

                    write_index(
                        settings.index_db_path,
                        settings.data_root,
                        index.all_versions(),
                        templates,
                    )
                except Exception:
                    pass
            return


def _rebuild_index() -> dict[str, int]:
    """Full rebuild of in-memory index and SQLite DB. Returns counts."""
    from .template_indexer import scan_templates

    with index_lock:
        index.build(settings.data_root)
        templates = scan_templates(settings.data_root)
        if settings.index_db_path:
            try:
                from .index_db import write_index

                write_index(
                    settings.index_db_path,
                    settings.data_root,
                    index.all_versions(),
                    templates,
                )
                _db_check(force=True)
            except Exception:
                pass
        return {
            "plugins": len(index.by_plugin_id),
            "versions": len(index.by_unique_identifier),
            "templates": len(templates),
        }


@app.on_event("startup")
def _startup_sync_and_prime_index() -> None:
    # Optional sync hook (git pull, unzip difypkg, generate extracted dirs, etc.)
    if settings.sync_cmd:
        try:
            subprocess.run(
                ["/bin/sh", "-lc", settings.sync_cmd],
                check=True,
                timeout=settings.sync_timeout_seconds,
            )
        except Exception:
            # Don't crash the server if sync fails; indexing may still work.
            pass

    # Prime index once so first request is fast.
    try:
        _ensure_index_fresh()
    except Exception:
        pass


def _rebuild_index() -> dict[str, int]:
    """Full rebuild of in-memory index and SQLite DB. Returns counts."""
    from .template_indexer import scan_templates

    with index_lock:
        index.build(settings.data_root)
        templates = scan_templates(settings.data_root)
        if settings.index_db_path:
            try:
                from .index_db import write_index

                write_index(
                    settings.index_db_path,
                    settings.data_root,
                    index.all_versions(),
                    templates,
                )
                _db_check(force=True)
            except Exception:
                pass
        return {
            "plugins": len(index.by_plugin_id),
            "versions": len(index.by_unique_identifier),
            "templates": len(templates),
        }


@app.on_event("startup")
def _startup_sync_and_prime_index() -> None:
    if settings.sync_cmd:
        try:
            subprocess.run(
                ["/bin/sh", "-lc", settings.sync_cmd],
                check=True,
                timeout=settings.sync_timeout_seconds,
            )
        except Exception:
            pass

    try:
        _ensure_index_fresh()
    except Exception:
        pass


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/admin/reindex")
def admin_reindex(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    if settings.admin_token and x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="Unauthorized")
    counts = _rebuild_index()
    return {"status": "ok", **counts}


_update_state = {
    "running": False,
    "success": None,
    "finished_at": 0.0,
}
_update_queue = None
_update_lock = threading.Lock()


def _emit(line: str) -> None:
    """Push a line into the SSE queue."""
    global _update_queue
    with _update_lock:
        if _update_queue is not None:
            _update_queue.put_nowait(line)


@app.post("/api/v1/admin/update")
def admin_update(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    if settings.admin_token and x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if _update_state["running"]:
        raise HTTPException(status_code=409, detail="Update already in progress")

    global _update_queue
    _update_queue = asyncio.Queue()
    _update_state["running"] = True
    _update_state["success"] = None

    def _run_update() -> None:
        script = (
            Path(__file__).resolve().parent.parent / "scripts" / "sync_from_github.sh"
        )
        if not script.exists():
            _emit(f"Script not found: {script}")
            _update_state["success"] = False
            _update_state["running"] = False
            _update_state["finished_at"] = time.time()
            with _update_lock:
                if _update_queue is not None:
                    _update_queue.put_nowait("__DONE__")
            return

        try:
            proc = subprocess.Popen(
                ["/bin/bash", str(script)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=str(Path(__file__).resolve().parent.parent),
            )
            for line in proc.stdout:
                line = line.rstrip("\n")
                _emit(line)
            proc.wait()

            if proc.returncode != 0:
                _update_state["success"] = False
            else:
                _emit("")
                _emit("🔄 Reindexing...")
                counts = _rebuild_index()
                _emit(
                    f"✅ Index rebuilt: {counts['plugins']} plugins, "
                    f"{counts['versions']} versions, {counts['templates']} templates"
                )
                _update_state["success"] = True
        except Exception as e:
            _emit(str(e))
            _update_state["success"] = False
        finally:
            _update_state["running"] = False
            _update_state["finished_at"] = time.time()
            with _update_lock:
                if _update_queue is not None:
                    _update_queue.put_nowait("__DONE__")

    threading.Thread(target=_run_update, daemon=True).start()
    return {"status": "started"}


@app.get("/api/v1/admin/update/stream")
def admin_update_stream(
    token: str = Query(default=""),
) -> StreamingResponse:
    if settings.admin_token and token != settings.admin_token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    async def event_stream():
        global _update_queue
        while True:
            if _update_queue is None:
                yield 'data: {"status": "idle"}\n\n'
                await asyncio.sleep(1)
                continue
            try:
                line = await asyncio.wait_for(_update_queue.get(), timeout=1.0)
                if line == "__DONE__":
                    success = _update_state.get("success")
                    done_msg = json.dumps({"done": True, "success": success})
                    yield f"data: {done_msg}\n\n"
                    _update_queue = None
                    break
                line_msg = json.dumps({"line": line})
                yield f"data: {line_msg}\n\n"
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/v1/plugins/download")
def download_by_unique_identifier(unique_identifier: str) -> Response:
    if _db_check():
        from .index_db import get_version_row

        r = get_version_row(_db_path(), unique_identifier)
        if not r:
            raise HTTPException(status_code=404, detail="Plugin not found")
        pkg_path = _version_row_to_record(r).pkg_path
        return FileResponse(
            path=str(pkg_path),
            media_type="application/octet-stream",
            filename=Path(str(pkg_path)).name,
        )

    _ensure_index_fresh()
    rec = index.get_pkg(unique_identifier)
    if not rec:
        raise HTTPException(status_code=404, detail="Plugin not found")
    return FileResponse(
        path=str(rec.pkg_path),
        media_type="application/octet-stream",
        filename=rec.pkg_path.name,
    )


@app.get("/api/v1/plugins/{org}/{name}/{version}/download")
def download_by_parts(org: str, name: str, version: str) -> Response:
    return download_by_unique_identifier(f"{org}/{name}:{version}")


@app.post("/api/v1/plugins/batch")
def batch_plugins(plugin_ids: dict = Body(...)) -> dict[str, Any]:
    if _db_check():
        from .index_db import get_plugin_row

        ids = plugin_ids.get("plugin_ids") or []
        result = []
        for plugin_id in ids:
            r = get_plugin_row(_db_path(), str(plugin_id))
            if not r:
                continue
            result.append(
                {
                    "name": r["name"],
                    "org": r["org"],
                    "plugin_id": r["plugin_id"],
                    "icon": f"/api/v1/plugins/{r['org']}/{r['name']}/icon",
                    "label": json.loads(r["label_json"]) if r["label_json"] else {},
                    "brief": json.loads(r["description_json"])
                    if r["description_json"]
                    else {},
                    "resource": {"memory": 0},
                    "endpoint": None,
                    "model": None,
                    "tool": None,
                    "latest_version": r["latest_version"],
                    "latest_package_identifier": r["latest_unique_identifier"],
                    "status": "active",
                    "deprecated_reason": "",
                    "alternative_plugin_id": "",
                }
            )
        return {"data": {"plugins": result}}

    _ensure_index_fresh()
    ids = plugin_ids.get("plugin_ids") or []
    result = []
    for plugin_id in ids:
        pr = index.get_plugin(str(plugin_id))
        if not pr:
            continue
        latest = pr.latest
        result.append(
            {
                "name": pr.name,
                "org": pr.org,
                "plugin_id": pr.plugin_id,
                "icon": f"/api/v1/plugins/{pr.org}/{pr.name}/icon",
                "label": latest.label,
                "brief": latest.description,
                "resource": {"memory": 0},
                "endpoint": None,
                "model": None,
                "tool": None,
                "latest_version": latest.version,
                "latest_package_identifier": latest.unique_identifier,
                "status": "active",
                "deprecated_reason": "",
                "alternative_plugin_id": "",
            }
        )
    return {"data": {"plugins": result}}


@app.post("/api/v1/stats/plugins/install_count")
def record_install_event(_: dict = Body(...)) -> dict[str, Any]:
    # no-op for private deployments
    return {"status": "ok"}


def _to_plugin_card(pr, latest) -> dict[str, Any]:
    return {
        "type": "plugin",
        "org": pr.org,
        "name": pr.name,
        "plugin_id": pr.plugin_id,
        "version": latest.version,
        "latest_version": latest.version,
        "latest_package_identifier": latest.unique_identifier,
        "icon": f"/api/v1/plugins/{pr.org}/{pr.name}/icon",
        # IMPORTANT: do not include `icon_dark` as a relative URL.
        # The Dify web app uses `icon_dark` directly and will resolve it against
        # the Dify origin (nginx), not the marketplace origin, causing 404s.
        # Falling back to `icon` is fine; the marketplace icon endpoint can also
        # render the right icon based on theme via query.
        "verified": False,
        "label": latest.label,
        "brief": latest.description,
        "description": latest.description,
        "introduction": "",
        "repository": latest.repo,
        "category": pr.category,
        "install_count": 0,
        "endpoint": {"settings": []},
        "tags": [{"name": t} for t in latest.tags],
        "badges": [],
        "verification": {"authorized_category": "community"},
        "from": "marketplace",
    }


def _filter_plugins(
    query: str,
    category: str,
    tags: list[str],
    exclude: set[str],
) -> list:
    q = query.strip().lower()
    filtered = []
    for pr in index.by_plugin_id.values():
        if pr.plugin_id in exclude:
            continue
        if category and pr.category != category:
            continue
        latest = pr.latest
        if tags:
            wanted = set([str(t) for t in tags if str(t)])
            if not wanted.intersection(set(latest.tags)):
                continue
        if q:
            hay = " ".join(
                [
                    pr.plugin_id,
                    pr.name,
                    pr.org,
                    " ".join(latest.tags),
                    " ".join([str(v) for v in latest.label.values()]),
                    " ".join([str(v) for v in latest.description.values()]),
                ]
            ).lower()
            if q not in hay:
                continue
        filtered.append(pr)
    return filtered


@app.post("/api/v1/plugins/identifier/batch")
def plugins_by_identifier(payload: dict = Body(...)) -> dict[str, Any]:
    identifiers = payload.get("unique_identifiers") or []
    plugins = []

    if _db_check():
        from .index_db import get_plugin_row

        for uid in identifiers:
            plugin_id = str(uid).split(":", 1)[0]
            r = get_plugin_row(_db_path(), plugin_id)
            if not r:
                continue
            plugins.append(_plugin_row_to_card(r))
        return {"data": {"plugins": plugins, "total": len(plugins)}}

    _ensure_index_fresh()
    for uid in identifiers:
        rec = index.get_pkg(str(uid))
        if not rec:
            continue
        pr = index.get_plugin(rec.plugin_id)
        if not pr:
            continue
        plugins.append(_to_plugin_card(pr, rec))
    return {"data": {"plugins": plugins, "total": len(plugins)}}


@app.post("/api/v1/plugins/versions/batch")
def plugins_versions_batch(payload: dict = Body(...)) -> dict[str, Any]:
    tuples = payload.get("plugin_tuples") or []
    out = []

    if _db_check():
        from .index_db import get_plugin_row, get_version_row

        for item in tuples:
            org = str(item.get("org") or "")
            name = str(item.get("name") or "")
            version = str(item.get("version") or "")
            if not org or not name or not version:
                continue
            uid = f"{org}/{name}:{version}"
            vr = get_version_row(_db_path(), uid)
            pr = get_plugin_row(_db_path(), f"{org}/{name}")
            if not vr or not pr:
                continue
            out.append(
                {
                    "plugin": _plugin_row_to_card(pr),
                    "version": {
                        "plugin_name": name,
                        "plugin_org": org,
                        "unique_identifier": uid,
                    },
                }
            )
        return {"data": {"list": out}}

    _ensure_index_fresh()
    for item in tuples:
        org = str(item.get("org") or "")
        name = str(item.get("name") or "")
        version = str(item.get("version") or "")
        if not org or not name or not version:
            continue
        uid = f"{org}/{name}:{version}"
        rec = index.get_pkg(uid)
        pr = index.get_plugin(f"{org}/{name}")
        if not rec or not pr:
            continue
        out.append(
            {
                "plugin": _to_plugin_card(pr, pr.latest),
                "version": {
                    "plugin_name": name,
                    "plugin_org": org,
                    "unique_identifier": uid,
                },
            }
        )
    return {"data": {"list": out}}


def _search_advanced_impl(payload: dict) -> tuple[list[dict[str, Any]], int]:
    query = str(payload.get("query") or "").strip()
    page = int(payload.get("page") or 1)
    page_size = int(payload.get("page_size") or 40)
    category = str(payload.get("category") or "").strip()
    tags = payload.get("tags") or []
    exclude = set(payload.get("exclude") or [])

    if _db_check():
        from .index_db import query_plugins

        rows, total = query_plugins(
            _db_path(),
            query=query,
            category=category,
            tags=list(tags) if isinstance(tags, list) else [],
            exclude=exclude,
            page=page,
            page_size=page_size,
        )
        plugins = [_plugin_row_to_card(r) for r in rows]
        return plugins, total

    _ensure_index_fresh()
    filtered = _filter_plugins(
        query=query, category=category, tags=tags, exclude=exclude
    )
    total = len(filtered)
    start = (page - 1) * page_size
    end = start + page_size
    page_items = filtered[start:end]
    plugins = [_to_plugin_card(pr, pr.latest) for pr in page_items]
    return plugins, total


@app.post("/api/v1/plugins/search/advanced")
def search_plugins_advanced(payload: dict = Body(...)) -> dict[str, Any]:
    plugins, total = _search_advanced_impl(payload)
    # IMPORTANT: don't include `bundles: []` here. The Dify web UI uses
    # `res.data.bundles || res.data.plugins`, and `[]` is truthy.
    return {"data": {"plugins": plugins, "total": total}}


@app.post("/api/v1/bundles/search/advanced")
def search_bundles_advanced(payload: dict = Body(...)) -> dict[str, Any]:
    # Bundles are not implemented yet; keep shape compatible.
    _ensure_index_fresh()
    return {"data": {"bundles": [], "total": 0}}


@app.get("/")
def home(
    request: Request,
    q: str = Query(default=""),
    tags: str = Query(default=""),
    category: str = Query(default=""),
    language: str = Query(default="en-US"),
    theme: str = Query(default="system"),
    source: str = Query(default=""),
) -> HTMLResponse:
    _ensure_index_fresh()
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    category_map = {
        "agent": "agent-strategy",
        "agent-strategy": "agent-strategy",
        "datasource": "datasource",
        "trigger": "trigger",
        "tool": "tool",
        "model": "model",
        "extension": "extension",
        "": "",
    }
    cat = category_map.get(category, category)

    if _db_check():
        from .index_db import query_plugins

        rows, _total = query_plugins(
            _db_path(),
            query=q,
            category=cat,
            tags=tag_list,
            exclude=set(),
            page=1,
            page_size=50,
        )
        cards = [_plugin_row_to_card(r) for r in rows]
        total_indexed = _total
    else:
        filtered = _filter_plugins(query=q, category=cat, tags=tag_list, exclude=set())
        filtered = filtered[:50]
        cards = [_to_plugin_card(pr, pr.latest) for pr in filtered]
        total_indexed = len(index.by_plugin_id)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "plugins": cards,
            "q": q,
            "tags": tags,
            "category": cat,
            "language": language,
            "theme": theme,
            "source": source,
            "total": total_indexed,
            "admin_token": settings.admin_token or "",
        },
    )


templates_env = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@app.get("/templates")
def templates_page(
    request: Request,
    q: str = Query(default=""),
    mode: str = Query(default=""),
    language: str = Query(default="en-US"),
    theme: str = Query(default="system"),
) -> HTMLResponse:
    page = 1
    page_size = 50
    templates_list = []
    total = 0

    if _db_check():
        from .index_db import query_templates

        rows, total = query_templates(
            _db_path(),
            query=q,
            mode=mode,
            page=page,
            page_size=page_size,
        )
        templates_list = [
            {
                "template_id": r["template_id"],
                "name": r["name"],
                "mode": r["mode"],
                "description": r["description"],
                "icon": r["icon"],
                "version": r["version"],
            }
            for r in rows
        ]
    else:
        from .template_indexer import scan_templates

        all_templates = scan_templates(settings.data_root)
        if q:
            q_lower = q.lower()
            all_templates = [
                t
                for t in all_templates
                if q_lower
                in " ".join([t.template_id, t.name, t.mode, t.description]).lower()
            ]
        if mode:
            all_templates = [t for t in all_templates if t.mode == mode]
        total = len(all_templates)
        templates_list = [
            {
                "template_id": t.template_id,
                "name": t.name,
                "mode": t.mode,
                "description": t.description,
                "icon": t.icon,
                "version": t.version,
            }
            for t in all_templates[:page_size]
        ]

    return templates_env.TemplateResponse(
        "templates.html",
        {
            "request": request,
            "templates": templates_list,
            "q": q,
            "mode": mode,
            "language": language,
            "theme": theme,
            "total": total,
        },
    )


@app.get("/templates/{template_id}")
def template_detail_page(request: Request, template_id: str) -> HTMLResponse:
    yaml_path: Path | None = None
    template_data: dict[str, Any] = {}

    if _db_check():
        from .index_db import get_template_row, resolve_template_path

        row = get_template_row(_db_path(), template_id)
        if row:
            yaml_path = resolve_template_path(_db_path(), template_id)
            template_data = {
                "template_id": row["template_id"],
                "name": row["name"],
                "mode": row["mode"],
                "description": row["description"],
                "icon": row["icon"],
                "version": row["version"],
            }
    else:
        from .template_indexer import scan_templates

        all_t = scan_templates(settings.data_root)
        for t in all_t:
            if t.template_id == template_id:
                yaml_path = t.yaml_path
                template_data = {
                    "template_id": t.template_id,
                    "name": t.name,
                    "mode": t.mode,
                    "description": t.description,
                    "icon": t.icon,
                    "version": t.version,
                }
                break

    if not template_data:
        raise HTTPException(status_code=404, detail="Template not found")

    yaml_content = ""
    if yaml_path and yaml_path.exists():
        try:
            yaml_content = yaml_path.read_text("utf-8")
        except Exception:
            yaml_content = ""

    return templates_env.TemplateResponse(
        "template_detail.html",
        {
            "request": request,
            "template": template_data,
            "yaml_content": yaml_content,
        },
    )


@app.get("/api/v1/templates")
def list_templates_api(
    query: str = Query(default=""),
    mode: str = Query(default=""),
    page: int = Query(default=1),
    page_size: int = Query(default=40),
) -> dict[str, Any]:
    if _db_check():
        from .index_db import query_templates

        rows, total = query_templates(
            _db_path(), query=query, mode=mode, page=page, page_size=page_size
        )
        templates_out = [
            {
                "template_id": r["template_id"],
                "name": r["name"],
                "mode": r["mode"],
                "description": r["description"],
                "icon": r["icon"],
                "version": r["version"],
                "download_url": f"/api/v1/templates/{r['template_id']}/download",
            }
            for r in rows
        ]
        return {"data": {"templates": templates_out, "total": total}}

    from .template_indexer import scan_templates

    all_t = scan_templates(settings.data_root)
    if query:
        q_lower = query.lower()
        all_t = [
            t
            for t in all_t
            if q_lower
            in " ".join([t.template_id, t.name, t.mode, t.description]).lower()
        ]
    if mode:
        all_t = [t for t in all_t if t.mode == mode]
    total = len(all_t)
    start = (page - 1) * page_size
    templates_out = [
        {
            "template_id": t.template_id,
            "name": t.name,
            "mode": t.mode,
            "description": t.description,
            "icon": t.icon,
            "version": t.version,
            "download_url": f"/api/v1/templates/{t.template_id}/download",
        }
        for t in all_t[start : start + page_size]
    ]
    return {"data": {"templates": templates_out, "total": total}}


@app.get("/api/v1/templates/{template_id}")
def template_info_api(template_id: str) -> dict[str, Any]:
    if _db_check():
        from .index_db import get_template_row

        r = get_template_row(_db_path(), template_id)
        if not r:
            raise HTTPException(status_code=404, detail="Template not found")
        return {
            "data": {
                "template": {
                    "template_id": r["template_id"],
                    "name": r["name"],
                    "mode": r["mode"],
                    "description": r["description"],
                    "icon": r["icon"],
                    "version": r["version"],
                    "download_url": f"/api/v1/templates/{r['template_id']}/download",
                }
            }
        }

    from .template_indexer import scan_templates

    all_t = scan_templates(settings.data_root)
    for t in all_t:
        if t.template_id == template_id:
            return {
                "data": {
                    "template": {
                        "template_id": t.template_id,
                        "name": t.name,
                        "mode": t.mode,
                        "description": t.description,
                        "icon": t.icon,
                        "version": t.version,
                        "download_url": f"/api/v1/templates/{t.template_id}/download",
                    }
                }
            }
    raise HTTPException(status_code=404, detail="Template not found")


@app.get("/api/v1/templates/{template_id}/icon")
def template_icon(template_id: str) -> Response:
    yaml_path: Path | None = None
    icon_name: str = ""

    if _db_check():
        from .index_db import get_template_row

        row = get_template_row(_db_path(), template_id)
        if row:
            icon_name = str(row["icon"])
    else:
        from .template_indexer import scan_templates

        all_t = scan_templates(settings.data_root)
        for t in all_t:
            if t.template_id == template_id:
                icon_name = t.icon
                break

    if not icon_name:
        raise HTTPException(status_code=404, detail="Icon not found")

    if icon_name.startswith("_assets/") or "/" in icon_name:
        icon_path = (
            Path(settings.data_root)
            / "templates"
            / template_id
            / "_assets"
            / icon_name.lstrip("_assets/")
        )
        if not icon_path.exists():
            icon_path = (
                Path(settings.data_root)
                / "templates"
                / template_id
                / "_assets"
                / icon_name
            )
    else:
        icon_path = Path(settings.data_root) / "templates" / template_id / icon_name
        if not icon_path.exists():
            icon_path = (
                Path(settings.data_root)
                / "templates"
                / template_id
                / "_assets"
                / icon_name
            )

    if not icon_path.exists():
        raise HTTPException(status_code=404, detail="Icon not found")

    media_type, _ = mimetypes.guess_type(str(icon_path))
    return FileResponse(
        path=str(icon_path),
        media_type=media_type or "image/svg+xml",
    )


@app.get("/api/v1/templates/{template_id}/download")
def template_download(template_id: str) -> Response:
    yaml_path: Path | None = None

    if _db_check():
        from .index_db import resolve_template_path

        yaml_path = resolve_template_path(_db_path(), template_id)
    else:
        from .template_indexer import scan_templates

        all_t = scan_templates(settings.data_root)
        for t in all_t:
            if t.template_id == template_id:
                yaml_path = t.yaml_path
                break

    if not yaml_path or not yaml_path.exists():
        raise HTTPException(status_code=404, detail="Template not found")
    return FileResponse(
        path=str(yaml_path),
        media_type="application/x-yaml",
        filename="template.yaml",
    )


@app.post("/api/v1/templates/search/advanced")
def search_templates_advanced(payload: dict = Body(...)) -> dict[str, Any]:
    return list_templates_api(
        query=str(payload.get("query") or ""),
        mode=str(payload.get("mode") or ""),
        page=int(payload.get("page") or 1),
        page_size=int(payload.get("page_size") or 40),
    )


@app.get("/api/v1/collections")
def collections() -> dict[str, Any]:
    return {
        "data": {
            "collections": [
                {
                    "name": "__recommended-plugins-tools",
                    "label": {"en-US": "Recommended Tools"},
                    "description": {"en-US": "Recommended tools"},
                    "rule": "",
                    "created_at": "",
                    "updated_at": "",
                    "searchable": False,
                    "search_params": {
                        "query": "",
                        "sort_by": "install_count",
                        "sort_order": "DESC",
                    },
                },
                {
                    "name": "__recommended-plugins-triggers",
                    "label": {"en-US": "Recommended Triggers"},
                    "description": {"en-US": "Recommended triggers"},
                    "rule": "",
                    "created_at": "",
                    "updated_at": "",
                    "searchable": False,
                    "search_params": {
                        "query": "",
                        "sort_by": "install_count",
                        "sort_order": "DESC",
                    },
                },
            ]
        }
    }


@app.post("/api/v1/collections/{collection_id}/plugins")
def collection_plugins(
    collection_id: str, payload: dict = Body(default=None)
) -> dict[str, Any]:
    payload = payload or {}
    limit = int(payload.get("limit") or 15)
    category = ""
    if collection_id.endswith("tools"):
        category = "tool"
    elif collection_id.endswith("triggers"):
        category = "trigger"

    if _db_check():
        from .index_db import query_plugins

        rows, _total = query_plugins(
            _db_path(),
            query="",
            category=category,
            tags=[],
            exclude=set(),
            page=1,
            page_size=limit,
        )
        items = [_plugin_row_to_card(r) for r in rows]
        return {"data": {"plugins": items}}

    _ensure_index_fresh()

    items = []
    for pr in index.by_plugin_id.values():
        if category and pr.category != category:
            continue
        items.append(_to_plugin_card(pr, pr.latest))
        if len(items) >= limit:
            break
    return {"data": {"plugins": items}}


@app.get("/api/v1/plugins/{plugin_id:path}/versions")
def plugin_versions(
    plugin_id: str, page: int = 1, page_size: int = 100
) -> dict[str, Any]:
    if _db_check():
        from .index_db import list_versions

        rows = list_versions(_db_path(), plugin_id, page, page_size)
        out = []
        for r in rows:
            out.append(
                {
                    "plugin_org": r["org"],
                    "plugin_name": r["name"],
                    "version": r["version"],
                    "file_name": Path(str(r["pkg_path"])).name,
                    "checksum": r["checksum"],
                    "created_at": r["created_at"],
                    "unique_identifier": r["unique_identifier"],
                }
            )
        return {"data": {"versions": out}}

    _ensure_index_fresh()
    pr = index.get_plugin(plugin_id)
    if not pr:
        raise HTTPException(status_code=404, detail="Plugin not found")
    versions = pr.versions
    start = (page - 1) * page_size
    end = start + page_size
    out = []
    for v in versions[start:end]:
        out.append(
            {
                "plugin_org": v.org,
                "plugin_name": v.name,
                "version": v.version,
                "file_name": v.pkg_path.name,
                "checksum": v.checksum,
                "created_at": v.created_at,
                "unique_identifier": v.unique_identifier,
            }
        )
    return {"data": {"versions": out}}


@app.get("/api/v1/plugins/{plugin_id:path}/icon")
def plugin_icon(plugin_id: str, theme: str | None = None) -> Response:
    if _db_check():
        from .index_db import get_plugin_row, get_version_row

        pr = get_plugin_row(_db_path(), plugin_id)
        if not pr:
            raise HTTPException(status_code=404, detail="Plugin not found")
        vr = get_version_row(_db_path(), pr["latest_unique_identifier"])
        if not vr:
            raise HTTPException(status_code=404, detail="Plugin not found")
        v = _version_row_to_record(vr)
        icon_rel = (
            v.icon_dark_path if theme == "dark" and v.icon_dark_path else v.icon_path
        )
        if not icon_rel:
            raise HTTPException(status_code=404, detail="Icon not found")
        data = read_asset_bytes(v, icon_rel)
        if not data:
            raise HTTPException(status_code=404, detail="Icon not found")
        media_type, _ = mimetypes.guess_type(icon_rel)
        return Response(content=data, media_type=media_type or "image/svg+xml")

    _ensure_index_fresh()
    pr = index.get_plugin(plugin_id)
    if not pr:
        raise HTTPException(status_code=404, detail="Plugin not found")
    v = pr.latest
    icon_rel = v.icon_dark_path if theme == "dark" and v.icon_dark_path else v.icon_path
    if not icon_rel:
        raise HTTPException(status_code=404, detail="Icon not found")
    data = read_asset_bytes(v, icon_rel)
    if not data:
        raise HTTPException(status_code=404, detail="Icon not found")
    media_type, _ = mimetypes.guess_type(icon_rel)
    return Response(content=data, media_type=media_type or "image/svg+xml")


@app.get("/api/v1/plugins/{plugin_id:path}/_assets/{asset_path:path}")
def plugin_asset(plugin_id: str, asset_path: str) -> Response:
    if _db_check():
        from .index_db import get_plugin_row, get_version_row

        pr = get_plugin_row(_db_path(), plugin_id)
        if not pr:
            raise HTTPException(status_code=404, detail="Plugin not found")
        vr = get_version_row(_db_path(), pr["latest_unique_identifier"])
        if not vr:
            raise HTTPException(status_code=404, detail="Plugin not found")
        v = _version_row_to_record(vr)
        rel = f"_assets/{asset_path}".lstrip("/")
        data = read_asset_bytes(v, rel)
        if not data:
            raise HTTPException(status_code=404, detail="Asset not found")
        media_type, _ = mimetypes.guess_type(asset_path)
        return Response(
            content=data, media_type=media_type or "application/octet-stream"
        )

    _ensure_index_fresh()
    pr = index.get_plugin(plugin_id)
    if not pr:
        raise HTTPException(status_code=404, detail="Plugin not found")
    v = pr.latest
    rel = f"_assets/{asset_path}".lstrip("/")
    data = read_asset_bytes(v, rel)
    if not data:
        raise HTTPException(status_code=404, detail="Asset not found")
    media_type, _ = mimetypes.guess_type(asset_path)
    return Response(content=data, media_type=media_type or "application/octet-stream")


@app.get("/api/v1/plugins/{plugin_id:path}")
def plugin_info(plugin_id: str) -> dict[str, Any]:
    if _db_check():
        from .index_db import get_plugin_row

        pr = get_plugin_row(_db_path(), plugin_id)
        if not pr:
            raise HTTPException(status_code=404, detail="Plugin not found")
        return {
            "data": {
                "plugin": {
                    "category": pr["category"],
                    "latest_package_identifier": pr["latest_unique_identifier"],
                    "latest_version": pr["latest_version"],
                },
                "version": {"version": pr["latest_version"]},
            }
        }

    _ensure_index_fresh()
    pr = index.get_plugin(plugin_id)
    if not pr:
        raise HTTPException(status_code=404, detail="Plugin not found")
    latest = pr.latest
    return {
        "data": {
            "plugin": {
                "category": pr.category,
                "latest_package_identifier": latest.unique_identifier,
                "latest_version": latest.version,
            },
            "version": {"version": latest.version},
        }
    }


@app.get("/plugins/{org}/{name}")
def plugin_detail_page(request: Request, org: str, name: str) -> HTMLResponse:
    plugin_id = f"{org}/{name}"

    if _db_check():
        from .index_db import get_plugin_row, get_version_row, list_versions

        pr = get_plugin_row(_db_path(), plugin_id)
        if not pr:
            raise HTTPException(status_code=404, detail="Plugin not found")
        vr = get_version_row(_db_path(), pr["latest_unique_identifier"])
        if not vr:
            raise HTTPException(status_code=404, detail="Plugin not found")
        latest = _version_row_to_record(vr)

        versions_rows = list_versions(_db_path(), plugin_id, page=1, page_size=200)
        versions = []
        for r in versions_rows:
            versions.append(
                {
                    "org": r["org"],
                    "name": r["name"],
                    "version": r["version"],
                    "unique_identifier": r["unique_identifier"],
                }
            )

        readme_html = ""
        if latest.extracted_dir:
            readme_path = latest.extracted_dir / "README.md"
            if readme_path.exists():
                try:
                    readme_html = render_markdown(
                        readme_path.read_text("utf-8"), org=org, name=name
                    )
                except Exception:
                    readme_html = ""

        # Minimal object compatible with templates.
        plugin_obj = {
            "plugin_id": plugin_id,
            "org": org,
            "name": name,
            "versions": versions,
        }

        return templates.TemplateResponse(
            "plugin.html",
            {
                "request": request,
                "plugin": plugin_obj,
                "latest": latest,
                "readme_html": readme_html,
            },
        )

    _ensure_index_fresh()
    pr = index.get_plugin(plugin_id)
    if not pr:
        raise HTTPException(status_code=404, detail="Plugin not found")

    readme_html = ""
    if pr.latest.extracted_dir:
        readme_path = pr.latest.extracted_dir / "README.md"
        if readme_path.exists():
            try:
                readme_html = render_markdown(
                    readme_path.read_text("utf-8"), org=org, name=name
                )
            except Exception:
                readme_html = ""

    return templates.TemplateResponse(
        "plugin.html",
        {
            "request": request,
            "plugin": pr,
            "latest": pr.latest,
            "readme_html": readme_html,
        },
    )


@app.get("/plugin/{org}/{name}")
def plugin_detail_page_alias(request: Request, org: str, name: str) -> HTMLResponse:
    return plugin_detail_page(request=request, org=org, name=name)


@app.exception_handler(Exception)
def unhandled_error(_request: Request, exc: Exception) -> JSONResponse:
    # Keep responses predictable for the Dify web client.
    return JSONResponse(status_code=500, content={"detail": str(exc)})


def main() -> None:
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
