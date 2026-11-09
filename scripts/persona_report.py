"""Turns a persona-acceptance JUnit file into a release report: revisions, how it ran, and each journey's result.

    python scripts/persona_report.py <junit.xml> <report.json>

The report holds no secret values, tokens or data payloads: only test names, outcomes and revisions. It states that
these journeys ran against the in-process API with a simulated orchestrator, so a reader never mistakes them for a
live installation check.
"""
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def revision() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "services", "apps", "terraform"], cwd=ROOT,
                               capture_output=True, text=True, check=True).stdout.strip()
        return sha + ("+local changes" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def provider_version() -> str:
    """The latest provider release tag reachable from this commit (releases are tagged provider-vX.Y.Z)."""
    try:
        tag = subprocess.run(["git", "describe", "--tags", "--abbrev=0", "--match", "provider-v*"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.strip()
        return tag.removeprefix("provider-v")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main(junit: str, out: str) -> int:
    journeys = []
    for case in ET.parse(junit).getroot().iter("testcase"):
        outcome = "failed" if case.find("failure") is not None or case.find("error") is not None else \
            "skipped" if case.find("skipped") is not None else "passed"
        journeys.append({"journey": case.get("name"), "module": case.get("classname"), "outcome": outcome,
                         "seconds": round(float(case.get("time") or 0), 2)})
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "control_plane_revision": revision(),
        "provider_version": provider_version(),
        "mode": "simulated: in-process API, test identities, simulated orchestrator; not a live installation",
        "summary": {o: sum(j["outcome"] == o for j in journeys) for o in ("passed", "failed", "skipped")},
        "journeys": journeys,
    }
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(report, indent=2) + "\n")
    print(f"Persona acceptance: {report['summary']} → {out}")
    return 1 if report["summary"]["failed"] or not journeys else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
