"""Development data seeder — creates default admin user and system configs.

USAGE:
    cd backend
    python scripts/seed_dev_data.py

IDEMPOTENT: safe to run repeatedly. Existing records are skipped.

Default account:
    username: admin
    password: admin123456
    role:     admin
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running from the backend/ directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.core.security import hash_password
from app.db.session import SyncSessionLocal
from app.models.user import User
from app.models.config import SystemConfig
from app.utils.ids import generate_public_id


def seed_users(session) -> dict:
    """Insert default admin user if not already present."""
    defaults = {
        "username": "admin",
        "password": "admin123456",
        "display_name": "管理员",
        "role": "admin",
        "public_id": "user_testagent_admin",
    }

    existing = session.execute(
        select(User).where(User.username == defaults["username"])
    ).scalar_one_or_none()

    if existing:
        return {"action": "skipped", "public_id": existing.public_id, "username": existing.username}

    user = User(
        public_id=defaults["public_id"],
        username=defaults["username"],
        password_hash=hash_password(defaults["password"]),
        display_name=defaults["display_name"],
        role=defaults["role"],
        status="active",
    )
    session.add(user)
    session.flush()
    return {
        "action": "created",
        "public_id": user.public_id,
        "username": user.username,
    }


SEED_CONFIGS = [
    ("upload.max_file_size_mb", "50", "int", "Single upload max file size (MB)", 1),
    ("upload.allowed_extensions", '[\".docx\",\".txt\",\".md\"]', "json", "Allowed upload file extensions", 1),
    ("upload.max_files_per_conversation", "10", "int", "Max files per conversation", 1),
    ("storage.local_base_path", "data", "string", "Local storage root relative path", 0),
    ("agent.default_timeout_seconds", "300", "int", "Agent default timeout (seconds)", 1),
    ("knowledge.default_top_k", "5", "int", "Knowledge base default retrieval count", 1),
]


def seed_system_configs(session) -> dict:
    """Insert default system_configs rows (skip if key already exists)."""
    created = 0
    skipped = 0
    for key, value, vtype, desc, editable in SEED_CONFIGS:
        exists = session.execute(
            select(SystemConfig).where(SystemConfig.config_key == key)
        ).scalar_one_or_none()
        if exists:
            skipped += 1
            continue
        session.add(SystemConfig(
            config_key=key,
            config_value=value,
            value_type=vtype,
            description=desc,
            editable=bool(editable),
        ))
        created += 1
    return {"created": created, "skipped": skipped}


def main() -> None:
    print("=" * 62)
    print("  TestAgent — Development Data Seeder")
    print("=" * 62)

    with SyncSessionLocal() as session:
        try:
            user_result = seed_users(session)
            cfg_result = seed_system_configs(session)
            session.commit()

            print(f"  User  [{user_result['action']}]: {user_result['username']}")
            if user_result["action"] == "created":
                print(f"        public_id: {user_result['public_id']}")
                print(f"        password:  admin123456  (default)")
            print(f"  Configs: {cfg_result['created']} created, {cfg_result['skipped']} skipped")
            print("=" * 62)
            print("  Seed complete.")
            print()
            print("  Default login credentials:")
            print(f"    username: admin")
            print(f"    password: admin123456")
            print()
            print("  The password hash is stored in the database.")
            print("  It is NEVER printed to the console.")
            print("=" * 62)
        except Exception as exc:
            session.rollback()
            print(f"  ERROR: {exc}")
            sys.exit(1)


if __name__ == "__main__":
    main()
