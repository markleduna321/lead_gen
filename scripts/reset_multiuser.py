"""One-time upgrade to per-user data isolation.

Startup migrations are deliberately additive and never touch existing rows, so
the destructive half of the multi-user upgrade lives here and must be run
explicitly, once:

    python scripts/reset_multiuser.py --wipe

What it does:
  1. Deletes every prospect, job lead, and custom hiring service (--wipe), or
     assigns them to an existing account (--assign-to EMAIL).
  2. Promotes prospects.user_id, job_leads.user_id and hiring_services.user_id
     to NOT NULL so ownerless rows can never be created again.

Back up first:  pg_dump "$DATABASE_URL" -f backup.sql
"""

import argparse
import os
import sys

import psycopg2
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "src"))
load_dotenv(os.path.join(BASE_DIR, ".env"))

DB_DSN = os.getenv("DATABASE_URL")
OWNED_TABLES = ("prospects", "job_leads", "hiring_services")


def counts(cur) -> dict:
    totals = {}
    for table in OWNED_TABLES:
        cur.execute(f"SELECT COUNT(*) FROM {table};")
        totals[table] = cur.fetchone()[0]
    return totals


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--wipe", action="store_true",
                       help="Delete all existing leads and start clean.")
    group.add_argument("--assign-to", metavar="EMAIL",
                       help="Give all existing leads to this registered account.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    args = parser.parse_args()

    if not DB_DSN:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 1

    with psycopg2.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('users');")
            if cur.fetchone()[0] is None:
                print("The users table does not exist yet. Start the app once so "
                      "startup migrations can create it, then re-run this script.",
                      file=sys.stderr)
                return 1

            before = counts(cur)
            print("Current rows:", ", ".join(f"{k}={v}" for k, v in before.items()))

            owner_id = None
            if args.assign_to:
                cur.execute("SELECT id FROM users WHERE email_key = %s;",
                            (args.assign_to.strip().casefold(),))
                row = cur.fetchone()
                if row is None:
                    print(f"No account found for {args.assign_to}. Register it first.",
                          file=sys.stderr)
                    return 1
                owner_id = row[0]
                action = f"assign {sum(before.values())} row(s) to {args.assign_to}"
            else:
                action = f"permanently DELETE {sum(before.values())} row(s)"

            if not args.yes:
                print(f"\nThis will {action} and make user_id NOT NULL.")
                if input("Type 'yes' to continue: ").strip().lower() != "yes":
                    print("Aborted. Nothing was changed.")
                    return 1

            if owner_id is None:
                for table in OWNED_TABLES:
                    cur.execute(f"DELETE FROM {table};")
            else:
                for table in OWNED_TABLES:
                    cur.execute(
                        f"UPDATE {table} SET user_id = %s WHERE user_id IS NULL;",
                        (owner_id,),
                    )

            for table in OWNED_TABLES:
                cur.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id IS NULL;")
                orphans = cur.fetchone()[0]
                if orphans:
                    print(f"{table} still has {orphans} ownerless row(s); aborting.",
                          file=sys.stderr)
                    conn.rollback()
                    return 1
                cur.execute(f"ALTER TABLE {table} ALTER COLUMN user_id SET NOT NULL;")

            after = counts(cur)
        conn.commit()

    print("Rows after:", ", ".join(f"{k}={v}" for k, v in after.items()))
    print("user_id is now NOT NULL on prospects, job_leads and hiring_services.")
    print("Done. Register an account at /register to begin.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
