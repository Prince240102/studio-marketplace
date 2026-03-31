# Private Dify Marketplace (Self-Hosted)

This service implements enough of the Dify Marketplace API to support:

- Dify backend plugin install/upgrade from marketplace (`MARKETPLACE_API_URL`)
- Dify web marketplace browsing (`NEXT_PUBLIC_MARKETPLACE_API_PREFIX`)

## Data layout

The indexer scans `MARKETPLACE_DATA_ROOT` recursively for `*.difypkg`.

For each `*.difypkg`, it reads `manifest.yaml` (or `manifest.yml`) from inside the archive.
If an extracted directory exists next to the package (same filename without `.difypkg`),
the service will prefer that directory for `_assets/` reads.

Example:

```
/data/
  my-plugin.difypkg
  my-plugin/
    manifest.yaml
    _assets/
      icon.svg
      icon-dark.svg
```

If you keep multiple versions, store multiple packages (and optional extracted dirs)
in the tree. The plugin id is derived from `manifest.author` + `manifest.name`.

## Environment

- `MARKETPLACE_DATA_ROOT` (default: `/data`)
- `MARKETPLACE_INDEX_DB_PATH` (optional): path to a SQLite index file
- `MARKETPLACE_VALIDATE_INDEX_DB` (default: `true`): if `true`, compare DB meta with filesystem stats and rebuild if different
- `MARKETPLACE_PORT` (default: `3001`)
- `MARKETPLACE_REINDEX_INTERVAL_SECONDS` (default: `30`)
- `MARKETPLACE_ADMIN_TOKEN` (optional): protects `POST /api/v1/admin/reindex`

### Startup sync hook

If you want the marketplace container to sync plugin files on startup (e.g. `git pull` + unzip),
set:

- `MARKETPLACE_SYNC_CMD` (default: empty)
- `MARKETPLACE_SYNC_TIMEOUT_SECONDS` (default: 600)

The command runs via `/bin/sh -lc` before the index is loaded.

Note: the default image only includes a minimal OS + Python; if your sync command
needs `git`, `git-lfs`, or `unzip`, run that sync outside the container (recommended)
or extend the Dockerfile to install those tools.

### SQLite index (git-committable)

If you set `MARKETPLACE_INDEX_DB_PATH`, the service will:

- Load plugin metadata from this SQLite file on startup (fast)
- Update it on reindex (admin endpoint or periodic reindex)

For a git-friendly workflow, generate the DB file on your CI/post-pull runner
and commit it to your marketplace repo.

You can generate it with:

```
python3 scripts/reindex_to_db.py --data-root <ASSETS_ROOT> --db marketplace_index.sqlite
```

## Quick test

```
docker build -t private-marketplace .
docker run --rm -p 3001:3001 -e MARKETPLACE_DATA_ROOT=/data -v /path/to/data:/data:ro private-marketplace
```
