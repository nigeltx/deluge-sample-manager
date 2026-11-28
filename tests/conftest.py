"""Shared test setup."""
import pytest


@pytest.fixture(autouse=True)
def _no_first_run_guide(monkeypatch):
    """The first-run guide is a modal window: in tests it is closed at once, as if the user pressed Cancel
    (test_first_run_guide replaces it with its own)."""
    from dsm.ui import first_run
    monkeypatch.setattr(first_run.FirstRunGuide, "exec", lambda self: 0)
