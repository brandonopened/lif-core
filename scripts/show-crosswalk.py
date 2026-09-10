#!/usr/bin/env python3
"""Print an MDR transformation group as a readable source -> target crosswalk table.

Usage:
    python scripts/show-crosswalk.py <group_id> [--mdr-url http://localhost:8012] [--api-key changeme1]
    python scripts/show-crosswalk.py --list  [--mdr-url ...] [--api-key ...]

Reads GET /transformation_groups/{id}/export (the same portable file the MDR UI downloads) and
shows one row per transformation: the source attribute path, the target attribute path, and the
Alignment note (``identity`` / ``educore:...`` for generated drafts, blank for hand-authored rows).
Nothing here touches learner data; a transformation group is schema-level metadata only.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _get(url: str, api_key: str) -> dict:
    request = urllib.request.Request(url, headers={"X-API-Key": api_key})
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - local demo URL
        return json.load(response)


def _path(entity_id_path: str | None) -> str:
    """'17:Person,17:Person.Name,17:~Person.Name.firstName' -> 'Person.Name.firstName'."""
    if not entity_id_path:
        return "?"
    last = entity_id_path.split(",")[-1]
    return last.split(":", 1)[-1].lstrip("~")


def list_groups(mdr_url: str, api_key: str) -> None:
    data = _get(f"{mdr_url}/transformation_groups/?pagination=false&size=200", api_key)
    rows = data.get("data", data)
    print(f"{'id':>4}  {'version':<8} {'source':>6} -> {'target':<6}  name")
    for group in rows:
        print(
            f"{group['Id']:>4}  {str(group.get('GroupVersion') or ''):<8} {group.get('SourceDataModelId'):>6} -> "
            f"{group.get('TargetDataModelId'):<6}  {group.get('Name') or '(unnamed)'}"
        )


def show_group(group_id: int, mdr_url: str, api_key: str) -> int:
    detail = _get(f"{mdr_url}/transformation_groups/{group_id}?pagination=false&size=1", api_key).get("data", {})
    try:
        export = _get(f"{mdr_url}/transformation_groups/{group_id}/export", api_key)
    except urllib.error.HTTPError as exc:
        if exc.code == 400:
            print(f"group {group_id} ({detail.get('Name') or '(unnamed)'}) has no exportable JSONata transformations")
            return 1
        raise
    transformations = export.get("Transformations", [])
    print(f"{export.get('Name') or '(unnamed)'}  v{export.get('GroupVersion')}  ({len(transformations)} mappings)")
    print(
        f"{detail.get('SourceDataModelName') or export.get('SourceDataModelId')}  ->  "
        f"{detail.get('TargetDataModelName') or export.get('TargetDataModelId')}\n"
    )
    rows = []
    for transformation in transformations:
        sources = " + ".join(_path(a.get("EntityIdPath")) for a in transformation.get("SourceAttributes") or [])
        target = _path((transformation.get("TargetAttribute") or {}).get("EntityIdPath"))
        rows.append((sources or "?", target, transformation.get("Alignment") or ""))
    width = max((len(r[0]) for r in rows), default=6)
    print(f"{'SOURCE':<{width}}  ->  TARGET  [alignment]")
    for sources, target, alignment in sorted(rows, key=lambda r: r[1]):
        note = f"  [{alignment}]" if alignment else ""
        print(f"{sources:<{width}}  ->  {target}{note}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("group_id", nargs="?", type=int, help="transformation group id (see --list)")
    parser.add_argument("--list", action="store_true", help="list transformation groups instead")
    parser.add_argument("--mdr-url", default=os.environ.get("MDR_URL", "http://localhost:8012"))
    parser.add_argument("--api-key", default=os.environ.get("MDR_API_KEY", "changeme1"))
    args = parser.parse_args()
    # Group names can contain arrows and other non-cp1252 characters; keep Windows consoles happy.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace")
    mdr_url = args.mdr_url.rstrip("/")
    if args.list:
        list_groups(mdr_url, args.api_key)
        return 0
    if args.group_id is None:
        parser.error("give a group id or --list")
    return show_group(args.group_id, mdr_url, args.api_key)


if __name__ == "__main__":
    sys.exit(main())
