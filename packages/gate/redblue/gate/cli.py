"""``redblue-gate`` command line (also reachable as ``redblue gate``)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from redblue.gate import report as rep
from redblue.gate.blue import BlueAgent
from redblue.gate.config import ConfigError, load_config
from redblue.gate.gate import run_gate
from redblue.gate.llm import LLM
from redblue.gate.schemas import Severity


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="redblue-gate",
        description=(
            "Adversarial security gate. Scans your code and a preview of your app that it "
            "launches itself. It never accepts a target URL."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--path", default=".", help="Repository to scan")
        sp.add_argument("--config", default=None, help="Config file (default .redblue.yml)")
        sp.add_argument("--base-ref", default=None, help="Diff base, e.g. origin/main")
        sp.add_argument("--fail-on", default=None, help="Severity threshold override")
        sp.add_argument("--no-ai", action="store_true", help="Skip all model calls")
        sp.add_argument("--only", default=None, help="Comma-separated scanner names")

    scan = sub.add_parser("scan", help="Scan and decide")
    common(scan)
    scan.add_argument("--format", choices=["md", "json", "sarif"], default="md")
    scan.add_argument("--output", default=None)

    fix = sub.add_parser("fix", help="Scan, then propose verified fixes")
    common(fix)
    fix.add_argument("--output-dir", default=".redblue/fixes")
    fix.add_argument(
        "--open-prs",
        action="store_true",
        help="Open PRs (GitHub Actions only). PRs are never merged automatically.",
    )

    ci = sub.add_parser("ci", help="CI mode: scan, write summary + SARIF, set exit code")
    common(ci)
    ci.add_argument("--sarif", default="redblue.sarif")
    ci.add_argument("--open-fix-prs", default="true")
    return p


def is_fork_event() -> bool:
    """True for pull requests from forks (they get no secrets, so AI steps are skipped)."""
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path or not Path(path).exists():
        return False
    try:
        event = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return False
    pr = event.get("pull_request") or {}
    head = (pr.get("head") or {}).get("repo") or {}
    base = (pr.get("base") or {}).get("repo") or {}
    return bool(head.get("fork")) or (bool(head) and head.get("full_name") != base.get("full_name"))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo = Path(args.path).resolve()
    try:
        config = load_config(args.config, repo)
    except ConfigError as exc:
        print(f"redblue-gate: {exc}", file=sys.stderr)
        return 2
    threshold = Severity.parse(args.fail_on) if args.fail_on else None
    only = args.only.split(",") if args.only else None
    use_ai = not args.no_ai
    if args.cmd == "ci" and is_fork_event():
        use_ai = False
        print("Pull request from a fork: AI steps skipped (no secrets available).")
    fix = args.cmd in ("fix",) or (
        args.cmd == "ci" and use_ai and args.open_fix_prs.lower() == "true"
    )
    base_ref = args.base_ref or (
        f"origin/{os.environ['GITHUB_BASE_REF']}"
        if args.cmd == "ci" and os.environ.get("GITHUB_BASE_REF")
        else None
    )
    report, decision = run_gate(
        config, repo, base_ref=base_ref, fix=fix, use_ai=use_ai, only=only, threshold=threshold
    )

    if args.cmd == "scan":
        out = {
            "md": lambda: rep.markdown(report, decision),
            "json": lambda: rep.as_json(report, decision),
            "sarif": lambda: json.dumps(rep.sarif(report), indent=2),
        }[args.format]()
        if args.output:
            Path(args.output).write_text(out, encoding="utf-8")
        else:
            print(out)
    elif args.cmd == "fix":
        blue = BlueAgent(config, repo, LLM.from_env(config.spend_limit_usd, use_ai))
        out_dir = Path(args.output_dir)
        for p in report.fixes:
            dest = blue.write(p, out_dir)
            state = "verified" if p.verified else "NOT verified"
            print(f"[{state}] {p.summary} -> {dest}")
            if args.open_prs and p.verified:
                url = blue.open_pr(p, os.environ.get("GITHUB_BASE_REF") or "main")
                if url:
                    print(f"  opened {url}")
        if not report.fixes:
            print("No automatic fixes available for the open findings.")
        print(decision.summary)
    else:  # ci
        md = rep.markdown(report, decision)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write(md)
        Path(args.sarif).write_text(json.dumps(rep.sarif(report), indent=2), encoding="utf-8")
        Path("redblue-report.md").write_text(md, encoding="utf-8")
        if fix and report.fixes:
            blue = BlueAgent(config, repo, LLM.from_env(config.spend_limit_usd, use_ai))
            for p in report.fixes:
                if p.verified:
                    blue.open_pr(p, os.environ.get("GITHUB_BASE_REF") or "main")
            Path("redblue-report.md").write_text(rep.markdown(report, decision), encoding="utf-8")
        print(md)
    return 0 if decision.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
