#!/usr/bin/env python3
"""
Generate a Draft transformation-group bundle between two MDR data models.

Usage (from the repo root):
    uv run python scripts/generate-crosswalk-draft.py \
        --source district-openapi.json --target college-openapi.json \
        [--crosswalk educore-crosswalk.json] [--authored exported-group.json] [--notes rule-notes.json] \
        --name "District -> College" \
        [--version 0.1] [--publisher X] [--min-confidence 0.5] -o bundle.json [--report report.json]

``--source``/``--target`` accept either a bare MDR OpenAPI export or a data-model bundle whose
``dataModel.openapi`` holds one. ``--authored`` takes an exported MDR transformation group (or a
transformation-group bundle) whose LIF-authored rules seed the draft, re-pointed by UniqueName; it may
be repeated, and when two files carry a rule for the same target the first file listed wins.
The report summary is printed to stderr.
"""

import argparse
import json
import sys
from pathlib import Path

try:
    from lif.crosswalk_draft import generate_draft_bundle
except ImportError:  # plain `python` outside the uv environment
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "components"))
    from lif.crosswalk_draft import generate_draft_bundle


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _authored_rules(path: str) -> list[dict]:
    """Accept an exported transformation group or a transformation-group bundle."""
    document = _load(path)
    group = document.get("transformationGroup")
    if not isinstance(group, dict):
        group = document
    return list(group.get("Transformations") or [])


def _dump(data: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a Draft transformation-group bundle between two MDR models.")
    parser.add_argument("--source", required=True, help="Source OpenAPI export or data-model bundle (JSON)")
    parser.add_argument("--target", required=True, help="Target OpenAPI export or data-model bundle (JSON)")
    parser.add_argument("--crosswalk", help="Optional EDUcore crosswalk JSON (properties + values)")
    parser.add_argument("--name", required=True, help="Transformation group name")
    parser.add_argument("--version", default="0.1", help="Transformation group version (default: 0.1)")
    parser.add_argument("--publisher", default="crosswalk_draft", help="Manifest publisher")
    parser.add_argument("--min-confidence", type=float, default=0.5, help="Drop crosswalk rows below this")
    parser.add_argument(
        "--authored",
        action="append",
        default=[],
        help="Exported MDR transformation group (or bundle) whose rules seed the draft; repeatable, the first file "
        "listed wins when two rules target the same attribute",
    )
    parser.add_argument("--authored-label", default="lif-authored", help="Alignment tag for seeded rules")
    parser.add_argument(
        "--notes",
        help="JSON overlay of transformation notes keyed by target path (rule Name): a string is the rule Notes, "
        "an object may carry Notes, SourceAttributeNotes (by source UniqueName) and TargetAttributeNotes",
    )
    parser.add_argument("-o", "--output", required=True, help="Where to write the bundle JSON")
    parser.add_argument("--report", help="Optional path to write the full report JSON")
    args = parser.parse_args()

    bundle, report = generate_draft_bundle(
        _load(args.source),
        _load(args.target),
        group_name=args.name,
        group_version=args.version,
        publisher=args.publisher,
        crosswalk=_load(args.crosswalk) if args.crosswalk else None,
        min_confidence=args.min_confidence,
        authored=[rule for path in args.authored for rule in _authored_rules(path)] if args.authored else None,
        authored_label=args.authored_label,
        rule_notes={k: v for k, v in _load(args.notes).items() if not k.startswith("_")} if args.notes else None,
    )
    _dump(bundle, args.output)
    if args.report:
        _dump(report, args.report)

    print(
        f"identity={report['identityMatches']} crosswalk={report['crosswalkMatches']} "
        f"authored={report['authoredMatches']} authoredSkipped={len(report['authoredSkipped'])} "
        f"valueLookups={report['valueLookupsApplied']} unmatchedSource={len(report['unmatchedSource'])} "
        f"unmatchedTarget={len(report['unmatchedTarget'])} belowConfidence={len(report['belowConfidence'])} "
        f"notesApplied={report['notesApplied']} notesUnmatched={len(report['notesUnmatched'])}",
        file=sys.stderr,
    )
    print(f"wrote {args.output} ({bundle['manifest']['checksum']})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
