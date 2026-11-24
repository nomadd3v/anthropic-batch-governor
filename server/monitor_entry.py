#!/usr/bin/env python3
"""launchd entry point for the batch-governor monitor.

Kept trivially small so the plist's ``ProgramArguments`` can reference a
single script rather than ``python -m server.monitor`` — some launchd
versions are easier to debug when the entry is a real file on disk with a
shebang.
"""

from __future__ import annotations

import asyncio

from server.monitor import main


if __name__ == "__main__":
    asyncio.run(main())
