#!/usr/bin/env python3
"""
Run once per PythonAnywhere scheduled task (recommended: every 5 minutes).

Example PA task command:
  python /home/USERNAME/ship2003/cron/run_simulation_tick.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simulation.engine import run_simulation_tick

if __name__ == '__main__':
    print(run_simulation_tick())
