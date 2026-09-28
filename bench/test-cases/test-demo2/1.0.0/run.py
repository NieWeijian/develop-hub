#!/usr/bin/env python3
from pathlib import Path
import re
import subprocess
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from demo_case_common import Case, arguments

LINE = re.compile(r"^\[(RGB|DEPTH)\] 5s: fps=([0-9]+(?:\.[0-9]+)?).*?recv=(\d+) drops_by_seq=(\d+)\(\+(\d+)\) total=(\d+)", re.M)

def main():
    data, out = arguments()
    case = Case(data, out)
    proc = None
    try:
        p = data["parameters"]
        duration = float(p.get("duration_sec", 12))
        minimum = float(p.get("min_fps", 1))
        if not 11 <= duration <= 120 or not 0 < minimum <= 30:
            raise ValueError("duration_sec must be 11..120 and min_fps must be 0..30")
        mode = case.call("get-device-mode", [data["tools"]["uvc_host_ctl"], "--device",
                                                    data["device"]["video"], "get-device-mode"])
        case.result["metrics"]["device_mode"] = mode
        if mode != "rgb+depth":
            return case.finish("blocked", "test-demo2 requires rgb+depth mode; actual " + mode)
        argv = [data["tools"]["uvc_host_stream"], "--device", data["device"]["video"]]
        log = out / "stream.log"
        start = time.monotonic()
        with log.open("wb") as output:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            try:
                proc.wait(timeout=duration)
                early = True
            except subprocess.TimeoutExpired:
                early = False
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    raise RuntimeError("Stream tool did not stop after SIGTERM")
        elapsed = time.monotonic() - start
        raw = log.read_text(errors="replace")
        case.result["steps"].append({"name": "capture", "argv": argv, "exit_code": proc.returncode,
                                      "elapsed_sec": round(elapsed, 3), "early_exit": early})
        case.save()
        if early or proc.returncode != 0:
            raise RuntimeError("Stream ended early or returned error: " + raw[-500:])
        measurements = {"RGB": [], "DEPTH": []}
        for label, fps, count, drops, interval_drops, total in LINE.findall(raw):
            measurements[label].append({"fps": float(fps), "frames": int(count),
                                        "drops_total": int(drops), "drops_interval": int(interval_drops),
                                        "total": int(total)})
        case.result["metrics"].update({"duration_sec": round(elapsed, 3), "streams": measurements})
        case.save()
        for label in ("RGB", "DEPTH"):
            windows = measurements[label]
            case.check(label + " measured windows", "at least 2", len(windows), len(windows) >= 2)
            average = sum(row["fps"] for row in windows) / len(windows)
            case.check(label + " average fps", minimum, round(average, 3), average >= minimum, "fps")
            case.check(label + " minimum window fps", minimum, min(row["fps"] for row in windows),
                       all(row["fps"] >= minimum for row in windows), "fps")
        return case.finish("passed")
    except AssertionError as exc:
        return case.finish("failed", str(exc))
    except Exception as exc:
        return case.finish("error", str(exc))
    finally:
        if proc and proc.poll() is None:
            proc.kill()
            proc.wait()

if __name__ == "__main__":
    raise SystemExit(main())

