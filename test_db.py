"""Explicit, read-only database connectivity check; inert during test discovery."""
import argparse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Read the configured Supabase database")
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live to run the database connectivity check")
    from bot.config import SUPER_ADMIN_ID
    from bot.db.users import get_user
    user = get_user(SUPER_ADMIN_ID)
    print("Database reachable; superadmin record", "found" if user else "not found")


if __name__ == "__main__":
    main()
