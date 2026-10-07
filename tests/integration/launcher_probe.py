"""Attach a process-id probe to the real app only in isolated launcher tests."""

import os

if os.environ.get("NOVELMIND_QA") != "launcher-isolated":
    raise RuntimeError("The process probe must only run under the isolated launcher test")

from backend.app.main import app


@app.get("/api/review/process")
def process_identity():
    """Expose only the test process identity, not environment or credentials."""
    return {"pid": os.getpid()}


# The real app ends in a GET static catch-all, so the test-only probe precedes it.
app.router.routes.insert(0, app.router.routes.pop())
