#!/usr/bin/env python3
"""Convenience entry point: cloned source + Docker + host Python 3.10+."""
from pathlib import Path
import runpy
runpy.run_path(str(Path(__file__).resolve().parents[1] / 'deploy/isolated-development/hub.py'), run_name='__main__')
