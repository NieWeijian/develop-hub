#!/usr/bin/env python3
import json
from pathlib import Path
import re
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from demo_case_common import Case, arguments

def main():
    data, out = arguments()
    case = Case(data, out)
    try:
        actual = case.call("get-sn", [data["tools"]["uvc_host_ctl"], "--device",
                                      data["device"]["video"], "get-sn"])
        actual = actual.strip()
        expected = data["parameters"].get("expected_sn")
        valid = bool(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", actual))
        case.result["metrics"]["sn"] = actual
        case.result["metrics"]["sn_length"] = len(actual)
        case.check("SN nonempty ASCII", "1-64 ASCII letters/digits/_/-", actual, valid)
        if expected:
            case.check("SN identity", expected, actual, actual == expected)
        return case.finish("passed")
    except AssertionError as exc:
        return case.finish("failed", str(exc))
    except Exception as exc:
        return case.finish("error", str(exc))

if __name__ == "__main__":
    raise SystemExit(main())

