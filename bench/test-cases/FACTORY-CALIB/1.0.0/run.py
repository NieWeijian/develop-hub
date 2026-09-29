import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
from factory_runtime import main
if __name__=='__main__': main('FACTORY-CALIB')
