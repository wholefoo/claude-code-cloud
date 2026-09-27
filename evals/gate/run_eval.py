"""Gate eval: recall on known-vulnerable snippets, false-positive rate on known-safe ones,
and (optionally) fix verification rate on the vulnerable demo app.

    python evals/gate/run_eval.py [--no-ai] [--fixes] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

from redblue.gate.config import parse_config
from redblue.gate.gate import run_gate
from redblue.gate.schemas import Severity

HERE = Path(__file__).parent
DEMO = HERE.parents[1] / "examples" / "vulnerable-demo"


def _scan_case(case: dict, use_ai: bool) -> list:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / case["file"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(case["code"])
        cfg = parse_config({"scanners": {"pip_audit": False, "dast": False, "zap": False,
                                         "nuclei": False, "semgrep": False}})
        report, _ = run_gate(cfg, root, use_ai=use_ai)
        return report.open_findings()


def evaluate(use_ai: bool = False, fixes: bool = False) -> dict:
    vulnerable = yaml.safe_load((HERE / "cases" / "vulnerable.yaml").read_text())
    safe = yaml.safe_load((HERE / "cases" / "safe.yaml").read_text())
    rows, hits, expected_total = [], 0, 0
    for case in vulnerable:
        found = {f.rule_id for f in _scan_case(case, use_ai)}
        ok = set(case["expect"]) <= found
        hits += sum(1 for r in case["expect"] if r in found)
        expected_total += len(case["expect"])
        rows.append({"case": case["name"], "kind": "vulnerable", "pass": ok,
                     "found": sorted(found)})
    fp_cases = 0
    for case in safe:
        noisy = [f.rule_id for f in _scan_case(case, use_ai) if f.severity >= Severity.medium]
        fp_cases += bool(noisy)
        rows.append({"case": case["name"], "kind": "safe", "pass": not noisy, "found": noisy})
    result = {
        "recall": round(hits / expected_total, 3),
        "false_positive_rate": round(fp_cases / len(safe), 3),
        "cases": rows,
    }
    if fixes:
        with tempfile.TemporaryDirectory() as tmp:
            demo = Path(tmp) / "demo"
            shutil.copytree(DEMO, demo)
            report, _ = run_gate(parse_config({"app": "app:app",
                                               "scanners": {"pip_audit": False}}),
                                 demo, use_ai=use_ai, fix=True)
        verified = sum(p.verified for p in report.fixes)
        result["fixes_proposed"] = len(report.fixes)
        result["fix_verification_rate"] = round(verified / max(1, len(report.fixes)), 3)
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--fixes", action="store_true")
    ap.add_argument("--json", default=None)
    ap.add_argument("--max-fp-rate", type=float, default=None)
    ap.add_argument("--min-recall", type=float, default=None)
    args = ap.parse_args()
    res = evaluate(use_ai=not args.no_ai, fixes=args.fixes)
    for row in res["cases"]:
        mark = "PASS" if row["pass"] else "FAIL"
        print(f"{mark:4}  {row['kind']:10}  {row['case']:28}  {', '.join(row['found'])}")
    print(f"\nrecall={res['recall']}  false_positive_rate={res['false_positive_rate']}"
          + (f"  fix_verification_rate={res['fix_verification_rate']}" if args.fixes else ""))
    if args.json:
        Path(args.json).write_text(json.dumps(res, indent=2))
    failed = (args.max_fp_rate is not None and res["false_positive_rate"] > args.max_fp_rate) or \
             (args.min_recall is not None and res["recall"] < args.min_recall)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
