import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
import argparse,json
from switch_runtime import SwitchCase
p=argparse.ArgumentParser();p.add_argument('--input',required=True);p.add_argument('--output-dir',required=True);a=p.parse_args()
raise SystemExit(SwitchCase('SWITCH-RESOLUTION',json.loads(Path(a.input).read_text()),Path(a.output_dir)).run())
