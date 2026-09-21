import gc
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Qt UI tests must not depend on a real display: on a desktop session the full suite opens
# hundreds of real windows and the X client eventually aborts mid-run. Real-GUI verification is a
# separate manual step (CLAUDE.md §10), so default to offscreen and let an explicit
# QT_QPA_PLATFORM in the environment win.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def _release_qt_widgets():
    """Close windows a test left open, so Qt resources do not accumulate across the suite.

    The suite builds a few hundred OperatorWindows. A test that forgets ``window.close()`` keeps
    its widgets (and their worker threads) alive for the rest of the run, and the process
    eventually aborts inside an unrelated test. Cleaning up here keeps failures local to the test
    that caused them.
    """
    yield
    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:  # headless environments without the UI extra
        return
    app = QApplication.instance()
    if app is None:
        return
    # hide() only: it releases the platform surface, which is the resource that runs out.
    # close() runs OperatorWindow's full shutdown and deleteLater() destroys objects the
    # finished test still references — both crash the run, which is worse than the leak.
    for widget in tuple(app.topLevelWidgets()):
        try:
            # A window built with real settings starts a live Hailo health thread that probes the
            # device through a subprocess every minute. Tests that never close their window leave
            # it running for the rest of the session; a few hundred of them exhaust the process.
            stop = getattr(widget, "_stop_hailo_health_monitor", None)
            if stop is not None:
                stop()
            widget.hide()
        except RuntimeError:  # already destroyed by the test itself
            continue
    # Windows form reference cycles (window → worker list → bound slot → window), so they are
    # only freed by the cyclic collector. Without this they pile up for the whole session.
    gc.collect()
