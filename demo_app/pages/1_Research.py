"""The Research page of the demo: runs the real pages/1_Research.py in demo mode (see ../streamlit_app.py)."""
import os
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
os.environ["DEMO_MODE"] = "true"  # must be set before `config` is imported
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

runpy.run_path(str(ROOT / "pages" / "1_Research.py"), run_name="__main__")
