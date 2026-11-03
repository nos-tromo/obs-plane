"""Make the audit/ modules importable as top-level modules, as in the container."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
