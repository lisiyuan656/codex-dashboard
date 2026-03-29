from __future__ import annotations

import argparse
import asyncio

from .config import load_config
from .service import DashboardAgent


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the codex-dashboard machine agent.")
    parser.add_argument("--once", action="store_true", help="Run one connection attempt and exit on disconnect.")
    args = parser.parse_args()

    config = load_config()
    agent = DashboardAgent(config)
    if args.once:
        asyncio.run(agent.run_once())
        return
    asyncio.run(agent.run_forever())
