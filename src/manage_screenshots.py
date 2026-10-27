"""Operator command: move legacy public screenshots into private Cloudflare R2.

    python src/manage_screenshots.py upload-legacy [--dry-run]

Safe to re-run: only rows still pointing at /static/screenshots/ are touched.
Rows whose file is missing are cleared so the inspector does not show a broken image.
"""

import argparse
import os
from pathlib import Path
import re

from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor

import r2_storage

LEGACY_PREFIX = "/static/screenshots/"
LEGACY_NAME = re.compile(r"^snap_[A-Za-z0-9_.:-]+\.png$")
DEFAULT_FOLDER = Path(__file__).resolve().parents[1] / "static" / "screenshots"


def upload_legacy(dsn, folder=DEFAULT_FOLDER, dry_run=False, storage=r2_storage):
    folder = Path(folder).resolve()
    summary = {"uploaded": 0, "relinked": 0, "missing": 0, "deleted_files": 0}
    uploaded = {}
    with psycopg2.connect(dsn, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, screenshot_path FROM prospects WHERE screenshot_path LIKE %s ORDER BY id;",
                        (LEGACY_PREFIX + "%",))
            rows = cur.fetchall()
            for row in rows:
                name = row["screenshot_path"][len(LEGACY_PREFIX):]
                path = (folder / name).resolve()
                if not LEGACY_NAME.match(name) or path.parent != folder or not path.is_file():
                    summary["missing"] += 1
                    if not dry_run:
                        cur.execute("UPDATE prospects SET screenshot_path = NULL WHERE id = %s AND screenshot_path = %s;",
                                    (row["id"], row["screenshot_path"]))
                    continue
                if path not in uploaded:
                    uploaded[path] = None if dry_run else storage.upload_png(path.read_bytes())
                    summary["uploaded"] += 1
                summary["relinked"] += 1
                if not dry_run:
                    cur.execute("UPDATE prospects SET screenshot_path = %s WHERE id = %s AND screenshot_path = %s;",
                                (uploaded[path], row["id"], row["screenshot_path"]))
    if not dry_run:
        for path in uploaded:
            path.unlink(missing_ok=True)
            summary["deleted_files"] += 1
    return summary


def main():
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subs = parser.add_subparsers(dest="command", required=True)
    legacy = subs.add_parser("upload-legacy", help="Upload static/screenshots files to R2 and relink leads.")
    legacy.add_argument("--dry-run", action="store_true", help="Report what would change without writing.")
    args = parser.parse_args()
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        parser.error("DATABASE_URL is required.")
    if not args.dry_run and not r2_storage.configured():
        parser.error("Set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY and R2_BUCKET first.")
    summary = upload_legacy(dsn, dry_run=args.dry_run)
    prefix = "Dry run: would have " if args.dry_run else ""
    print(f"{prefix}uploaded {summary['uploaded']} file(s), relinked {summary['relinked']} lead(s), "
          f"cleared {summary['missing']} missing, deleted {summary['deleted_files']} local file(s).")


if __name__ == "__main__":
    main()
