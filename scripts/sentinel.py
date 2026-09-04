#!/usr/bin/env python3
"""Sentinel CLI — anomaly & schema-drift watchdog (module M5, PDF task 5.5).

Runs the three read-only checks in ``sql/sentinel/`` against a driver (local
DuckDB by default, Databricks in production — see ``agent.db.get_driver``),
turns their rows into :class:`agent.sentinel_core.Finding` objects, and
writes a DRAFT report an analyst must approve before it goes anywhere.

Design philosophy (see docs/sentinel_design.md): STATISTICS DETECT, the LLM
only NARRATES the findings that already exist, and a human approves before
distribution — ``--notify`` never sends anything, it only prints the
message that would be sent and to whom, standing in for that approval step.

Usage
-----
    python scripts/sentinel.py [--as-of YYYY-MM-DD] [--driver duckdb|databricks]
                                [--format md|json] [--notify]
                                [--registry PATH]

Exit codes (Job-friendly): 0 clean, 1 warning(s), 2 critical.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent import sentinel_core as sc  # noqa: E402
from agent.db import get_driver  # noqa: E402
from agent.llm import get_llm  # noqa: E402

REPORTS_DIR = REPO_ROOT / "reports" / "sentinel"

#: Where --notify says the DRAFT would be posted, and to whom — a stand-in
#: address, since this script never actually calls Slack (see build_notify_
#: message's docstring for the human-checkpoint reasoning).
NOTIFY_CHANNEL = "#analytics-alerts"
NOTIFY_RECIPIENTS = "@data-team"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sentinel: anomaly & schema-drift watchdog (read-only)."
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help=(
            "Day to score, YYYY-MM-DD. Default: the latest MATURE event day "
            "in the data (max observed date minus a "
            f"{sc.EVENT_MATURITY_BUFFER_DAYS}-day maturity buffer — see "
            "agent.sentinel_core.default_as_of / EVENT_MATURITY_BUFFER_DAYS "
            "for why the raw max date itself is not scored by default)."
        ),
    )
    parser.add_argument(
        "--driver",
        default=None,
        help="Driver name ('duckdb' or 'databricks'); default: $AGENT_DB, else duckdb.",
    )
    parser.add_argument(
        "--format",
        choices=["md", "json"],
        default="md",
        help="Console output format (the on-disk report is always Markdown).",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help=(
            "Print the exact message that WOULD be sent, and to whom — "
            "never actually sends anything. See build_notify_message()."
        ),
    )
    parser.add_argument(
        "--registry",
        default=str(sc.DEFAULT_REGISTRY_PATH),
        help="Path to the expected-state registry JSON.",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

DRAFT_HEADER = "**DRAFT — pending analyst approval (human checkpoint)**"


def _findings_section(findings: list[sc.Finding], severity: str) -> list[str]:
    rows = [f for f in findings if f.severity == severity]
    if not rows:
        return [f"### {severity.capitalize()} (0)", "", "None.", ""]
    lines = [f"### {severity.capitalize()} ({len(rows)})", ""]
    for f in rows:
        lines.append(f"- `{f.check}` **{f.subject}** — {f.message}")
    lines.append("")
    return lines


def build_markdown_report(run: sc.SentinelRun, narration: str, driver_name: str) -> str:
    lines: list[str] = [
        f"# Sentinel Report — {run.as_of}",
        "",
        DRAFT_HEADER,
        "",
        "## Executive Summary",
        "",
        narration,
        "",
        f"## Findings ({len(run.findings)})",
        "",
    ]
    for severity in ("critical", "warning", "info"):
        lines += _findings_section(run.findings, severity)

    lines += [
        "## Schema Snapshot",
        "",
        f"- Columns inventoried: {len(run.columns_df)} across "
        f"{run.columns_df['table_name'].nunique() if not run.columns_df.empty else 0} table(s).",
        f"- Distinct event_name values observed: "
        f"{sorted(run.event_names_df['event_name'].unique().tolist())}",
        f"- Distinct app_version values observed: "
        f"{sorted(run.app_versions_df['app_version'].unique().tolist())}",
        "",
        "## Run Metadata",
        "",
        f"- as_of: {run.as_of}",
        f"- driver: {driver_name}",
        f"- exit_code: {run.exit_code}",
        f"- registry generated_at: {run.registry.get('generated_at', 'unknown')}",
        f"- thresholds: {json.dumps(run.registry['thresholds'])}",
        f"- generated_at (this run, UTC): {dt.datetime.now(dt.timezone.utc).isoformat()}",
        "",
    ]
    return "\n".join(lines)


def build_json_report(run: sc.SentinelRun, narration: str, driver_name: str) -> dict[str, Any]:
    return {
        "as_of": run.as_of,
        "draft": True,
        "human_checkpoint": "pending analyst approval",
        "driver": driver_name,
        "exit_code": run.exit_code,
        "narration": narration,
        "findings": [f.to_dict() for f in run.findings],
        "schema_snapshot": {
            "event_names": sorted(run.event_names_df["event_name"].unique().tolist()),
            "app_versions": sorted(run.app_versions_df["app_version"].unique().tolist()),
            "column_count": int(len(run.columns_df)),
        },
        "thresholds": run.registry["thresholds"],
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def build_notify_message(run: sc.SentinelRun, report_path: Path) -> str:
    """The exact text --notify prints instead of sending.

    Human checkpoint by design: the sentinel's job is to detect and draft,
    never to broadcast. An analyst reviews `report_path` and decides whether
    (and how) to actually notify anyone — this function only shows what
    that notification WOULD look like, so the approval step has something
    concrete to review, without ever calling a real Slack webhook.
    """
    worst = sc.worst_severity(run.findings) or "info"
    counts = {s: sum(1 for f in run.findings if f.severity == s) for s in sc.SEVERITIES}
    slack_text = (
        f"[Sentinel] {run.as_of} — {counts['critical']} critical, "
        f"{counts['warning']} warning, {counts['info']} info finding(s) "
        f"(worst: {worst}). Draft report: {report_path}"
    )
    return (
        "[NOTIFY — NOT SENT]\n"
        f"Would post to Slack channel {NOTIFY_CHANNEL} (recipients: {NOTIFY_RECIPIENTS}):\n"
        f'  "{slack_text}"\n'
        "\n"
        "# Why this only prints instead of sending: the sentinel intentionally\n"
        "# never auto-distributes its own findings (see docs/sentinel_design.md,\n"
        "# 'Human checkpoint'). An analyst must open the DRAFT report above,\n"
        "# confirm the findings aren't a false positive, and send the real\n"
        "# notification themselves — this is the approval gate that keeps a\n"
        "# statistical false alarm from ever reaching a stakeholder unreviewed."
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    driver_name = (args.driver or "").strip().lower() or None
    driver = get_driver(driver_name)
    resolved_driver_name = driver.__class__.__name__

    registry = sc.load_registry(Path(args.registry))
    as_of = args.as_of or sc.default_as_of(driver)

    run = sc.run_checks(driver, as_of, registry)

    llm = get_llm()
    narration = sc.narrate(run.findings, as_of, llm)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"sentinel_{as_of}.md"
    report_md = build_markdown_report(run, narration, resolved_driver_name)
    report_path.write_text(report_md, encoding="utf-8")

    if args.format == "json":
        print(json.dumps(build_json_report(run, narration, resolved_driver_name), indent=2))
    else:
        print(report_md)

    print(f"\n[sentinel] Report written to {report_path}")

    if args.notify:
        print()
        print(build_notify_message(run, report_path))

    return run.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
