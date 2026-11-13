"""Command-line helpers.

    python -m app.cli init            create the .env file (asks for the admin password)
    python -m app.cli hash-password   print a password hash for ADMIN_PASSWORD_HASH
    python -m app.cli gen-key         print a new ENCRYPTION_KEY
"""

from __future__ import annotations

import argparse
import getpass
import secrets
import sys
from pathlib import Path

from app.config import REPO_DIR
from app.core.security import CredentialCipher, encode_hash_for_env, hash_password

ENV_TEMPLATE = """# Created by `python -m app.cli init`. Keep this file private.
ADMIN_USERNAME={username}
ADMIN_PASSWORD_HASH={password_hash}
SESSION_SECRET={session_secret}
ENCRYPTION_KEY={encryption_key}

HOST=127.0.0.1
PORT=8000
SEED_DEMO_DATA=true
SIM_RANDOM_WALK=true
RECONCILE_INTERVAL_S=3.0
LOG_LEVEL=INFO
"""


def _read_secret(prompt: str) -> str:
    # Git Bash (mintty) on Windows has no real console, where getpass would hang.
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    return input(prompt.rstrip(": ") + " (visible while typing): ")


def _ask_password() -> str:
    while True:
        first = _read_secret("Admin password (min. 8 characters): ")
        if len(first) < 8:
            print("Too short, try again.")
            continue
        if _read_secret("Repeat password: ") != first:
            print("Passwords do not match, try again.")
            continue
        return first


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.env_file)
    if path.exists() and not args.force:
        print(f"{path} already exists. Use --force to overwrite it.")
        return 1
    username = args.username or input("Admin username [admin]: ").strip() or "admin"
    password = args.password or _ask_password()
    content = ENV_TEMPLATE.format(
        username=username,
        password_hash=encode_hash_for_env(hash_password(password)),
        session_secret=secrets.token_urlsafe(48),
        encryption_key=CredentialCipher.generate_key(),
    )
    path.write_text(content, encoding="utf-8")
    print(f"Created {path.resolve()}")
    print("Keep ENCRYPTION_KEY safe: without it, saved broker credentials cannot be decrypted.")
    return 0


def cmd_hash(args: argparse.Namespace) -> int:
    password = args.password or _ask_password()
    print(encode_hash_for_env(hash_password(password)))
    return 0


def cmd_gen_key(_: argparse.Namespace) -> int:
    print(CredentialCipher.generate_key())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create the .env file")
    p_init.add_argument("--env-file", default=str(REPO_DIR / ".env"))
    p_init.add_argument("--username")
    p_init.add_argument("--password", help="non-interactive (avoid: ends up in shell history)")
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=cmd_init)

    p_hash = sub.add_parser("hash-password", help="print a hash for ADMIN_PASSWORD_HASH")
    p_hash.add_argument("--password")
    p_hash.set_defaults(func=cmd_hash)

    p_key = sub.add_parser("gen-key", help="print a new ENCRYPTION_KEY")
    p_key.set_defaults(func=cmd_gen_key)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
