import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Qt UI tests must not depend on a real display: on a desktop session the full suite opens
# hundreds of real windows and the X client eventually aborts mid-run. Real-GUI verification is a
# separate manual step (CLAUDE.md §10), so default to offscreen and let an explicit
# QT_QPA_PLATFORM in the environment win.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
