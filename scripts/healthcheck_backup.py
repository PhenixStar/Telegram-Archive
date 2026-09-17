#!/usr/bin/env python3
"""Docker HEALTHCHECK for the backup container.

The scheduler rewrites a heartbeat file every 30s while it is healthy
(src/scheduler.py). A dead process, a wedged event loop, or a backup run that
has been stuck longer than BACKUP_STUCK_ALERT_HOURS all stop the updates.
Exit 0 = healthy, 1 = unhealthy.
"""

import os
import sys
import time

DEFAULT_HEARTBEAT_FILE = "/tmp/telegram-archive.heartbeat"
DEFAULT_MAX_AGE_SECONDS = 180  # several missed 30s beats plus slack


def main() -> int:
    path = os.getenv("HEARTBEAT_FILE", DEFAULT_HEARTBEAT_FILE)
    try:
        max_age = int(os.getenv("HEARTBEAT_MAX_AGE_SECONDS", str(DEFAULT_MAX_AGE_SECONDS)))
    except ValueError:
        max_age = DEFAULT_MAX_AGE_SECONDS
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return 1  # never written: the scheduler has not proven it is healthy
    return 0 if age < max_age else 1


if __name__ == "__main__":
    sys.exit(main())
