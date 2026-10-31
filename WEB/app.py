#!/usr/bin/env python3
"""Main entry point for launching the Interactive BESS Web Dashboard.

Usage:
    python WEB/app.py
    python WEB/app.py --port 8080 --host 0.0.0.0
"""

import argparse
import logging
import os
from pathlib import Path
import sys
import webbrowser

# Ensure WEB folder and repo root are discoverable
CURRENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = CURRENT_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

try:
    from WEB.server import run_server
except ImportError:
    from server import run_server


def parse_args():
    parser = argparse.ArgumentParser(
        description="Launch the Interactive BESS Web Dashboard (Grid BESS & Solarnex BESS)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        '--host',
        type=str,
        default='127.0.0.1',
        help='Server host address (e.g. 127.0.0.1 or 0.0.0.0 for LAN access)',
    )
    parser.add_argument(
        '--port',
        type=int,
        default=8080,
        help='Server TCP port number',
    )
    parser.add_argument(
        '--tmp-dir',
        type=str,
        default=None,
        help='Custom directory for temporary run outputs (defaults to WEB/tmp)',
    )
    parser.add_argument(
        '--browser',
        action='store_true',
        help='Automatically launch default web browser on startup',
    )
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='Enable verbose debug logging',
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )

    if args.browser:
        target_url = f"http://{args.host if args.host != '0.0.0.0' else 'localhost'}:{args.port}/"
        try:
            webbrowser.open_new_tab(target_url)
        except Exception:
            pass

    run_server(host=args.host, port=args.port, tmp_dir=args.tmp_dir)


if __name__ == '__main__':
    main()

