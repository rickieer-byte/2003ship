"""
Local dev helper: continuous simulation loop.
On PythonAnywhere, use cron/run_simulation_tick.py as a scheduled task instead.
"""
import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simulation.engine import run_simulation_tick

if __name__ == '__main__':
    print("Telemetry simulation active (10s intervals). Ctrl+C to stop.")
    print("For PythonAnywhere, schedule: python cron/run_simulation_tick.py")
    while True:
        try:
            result = run_simulation_tick()
            print(result)
        except Exception as exc:
            print(f"Simulation error: {exc}")
        time.sleep(10)
