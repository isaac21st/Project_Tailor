"""Shared by the browser tests: the extension's background worker, waited for patiently (a cold Chromium can take more
than 15 s to start it), and a clean shutdown when the start fails, so one slow start does not break every later test
("Playwright Sync API inside the asyncio loop")."""
from __future__ import annotations

import time


def service_worker(ctx, timeout_s: float = 40.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if ctx.service_workers:
            return ctx.service_workers[0]
        try:
            return ctx.wait_for_event("serviceworker", timeout=5000)
        except Exception:
            continue
    raise TimeoutError("the extension's background worker did not start")


def start_or_clean_up(cls, ctx, pw, cleanup):
    """The extension's worker, or everything shut down and the error raised."""
    try:
        return service_worker(ctx)
    except Exception:
        try:
            ctx.close()
        finally:
            pw.stop()
            cleanup()
        raise
