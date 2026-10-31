from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import RuntimeConfig
from .storage import ensure_directories, require_storage


def main() -> int:
    parser = argparse.ArgumentParser(prog="reco-field")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--data-dir", type=Path)

    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--bind")
    serve_parser.add_argument("--port", type=int)

    args = parser.parse_args()
    runtime = RuntimeConfig.from_env()
    if getattr(args, "data_dir", None):
        runtime = RuntimeConfig(
            **{
                **runtime.__dict__,
                "data_dir": args.data_dir.expanduser().resolve(),
            }
        )
    require_storage(runtime.data_dir, runtime.storage_mount, runtime.storage_uuid)
    ensure_directories(runtime.data_dir)
    if args.command == "init":
        print("Reco Field data initialized. Open the web interface to control the device.")
        return 0

    import uvicorn

    from .app import create_app

    if args.bind:
        runtime = RuntimeConfig(**{**runtime.__dict__, "bind": args.bind})
    if args.port:
        runtime = RuntimeConfig(**{**runtime.__dict__, "port": args.port})
    app = create_app(runtime)
    uvicorn.run(app, host=runtime.bind, port=runtime.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
