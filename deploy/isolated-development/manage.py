#!/usr/bin/env python3
"""Compatibility aliases for the versioned deployment controller."""
import os
from pathlib import Path
import sys
arguments = sys.argv[1:] or ['status']
aliases = {'build': 'prepare', 'test': 'prepare', 'restart': 'deploy', 'start': 'deploy'}
arguments[0] = aliases.get(arguments[0], arguments[0])
if '--root' not in arguments:
    arguments += ['--root', str(Path.home() / 'develop-hub-dev')]
os.execv(sys.executable, [sys.executable, str(Path(__file__).with_name('hub.py')), *arguments])
