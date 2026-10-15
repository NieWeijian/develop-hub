#!/usr/bin/env python3
"""Compatibility entry point; implementation is versioned in hub.py."""
from pathlib import Path
import runpy
runpy.run_path(str(Path(__file__).with_name('hub.py')), run_name='__main__')
