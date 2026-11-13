"""Start the server:  python -m app"""

from __future__ import annotations

import sys

import uvicorn

from app.config import get_settings


def main() -> int:
    settings = get_settings()
    missing = settings.missing()
    if missing:
        print(f"Missing settings: {', '.join(missing)}.")
        print("Create the .env file first:  python -m app.cli init")
        return 1
    # One process only: the copy engine keeps its state in memory.
    uvicorn.run(
        "app.main:create_app", factory=True, host=settings.host, port=settings.port,
        workers=1, log_level=settings.log_level.lower(), proxy_headers=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
