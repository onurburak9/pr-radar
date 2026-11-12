"""Entry point: python -m pr_radar [--config path] [--once]"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, load_config
from .github import GitHubClient
from .poller import Poller
from .server import serve
from .store import Store


def main() -> None:
    ap = argparse.ArgumentParser(prog="pr-radar")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    ap.add_argument("--once", action="store_true", help="poll once, print the snapshot summary, exit")
    ap.add_argument("--port", type=int, help="override config port")
    ap.add_argument("--log-file", type=Path, help="rotating log file (default: stderr)")
    args = ap.parse_args()

    fmt = "%(asctime)s %(levelname)s %(message)s"
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(args.log_file, maxBytes=1_000_000, backupCount=3)
    else:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(fmt))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    cfg = load_config(args.config)
    poller = Poller(cfg, Store(cfg.db_path), GitHubClient(cfg.github_login))

    if args.once:
        poller.poll_once()
        snap = poller.snapshot()
        print(json.dumps({"counts": snap["counts"], "health": snap["health"]}, indent=2))
        return

    port = args.port or cfg.port
    httpd = serve(poller, port)
    logging.info("pr-radar listening on http://localhost:%d", port)

    def shutdown(*_):
        poller.stop()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    poller.run_forever()
    httpd.shutdown()


if __name__ == "__main__":
    main()
