"""Streamlit Community Cloud entrypoint for the read-only demo.

Community Cloud installs the requirements.txt that sits next to the entrypoint first, so this folder carries a slim
one (demo_app/requirements.txt) while the repository root keeps the full pipeline list. This file turns DEMO_MODE on
(no secret needed), puts the repository root on the import path and runs the real dashboard, app.py.

Locally:  streamlit run demo_app/streamlit_app.py
"""
import os
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["DEMO_MODE"] = "true"  # must be set before `config` is imported
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

runpy.run_path(str(ROOT / "app.py"), run_name="__main__")
