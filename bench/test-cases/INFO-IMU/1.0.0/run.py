import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
from telemetry_runtime import main
main('INFO-IMU')
