"""
Root-level conftest.py.
Ensures the project root is on sys.path so `from src.x import y` works in tests.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
