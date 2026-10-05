from __future__ import annotations

import os

import uvicorn

from hoh_control_plane.api import create_app


def main() -> None:
    host = os.getenv("HOH_HOST", "127.0.0.1")
    port = int(os.getenv("HOH_PORT", "8765"))
    # Opt-in only: seeding by default put a demo project into whatever database
    # the process was pointed at, including the real one.
    seed = os.getenv("HOH_SEED_DEMO", "0").lower() in {"1", "true", "yes"}
    uvicorn.run(create_app(seed_demo=seed), host=host, port=port)


if __name__ == "__main__":
    main()
