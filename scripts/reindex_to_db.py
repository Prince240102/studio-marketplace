#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.index_db import write_index  # noqa: E402
from app.indexer import MarketplaceIndex  # noqa: E402
from app.template_indexer import scan_templates  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Build marketplace SQLite index DB")
    parser.add_argument(
        "--data-root", required=True, help="Root directory that contains *.difypkg"
    )
    parser.add_argument("--db", required=True, help="SQLite db path to write")
    args = parser.parse_args()

    data_root = str(Path(args.data_root).resolve())
    db_path = str(Path(args.db).resolve())

    idx = MarketplaceIndex()
    idx.build(data_root)
    templates = scan_templates(data_root)
    write_index(db_path, data_root, idx.all_versions(), templates)

    print(
        f"Wrote {len(idx.by_unique_identifier)} versions and {len(templates)} templates into {db_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
