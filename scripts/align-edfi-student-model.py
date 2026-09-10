#!/usr/bin/env python3
"""Align an MDR Ed-Fi data model's Student identity elements with the Ed-Fi Data Standard (schema-exchange demo).

Usage (from the repo root):
    uv run python scripts/align-edfi-student-model.py --mdr-url http://localhost:8012 [--api-key changeme1]
        [--model-name "Northgate Ed-Fi v5"] [--model-version 1.0]

The MDR's seeded ``Ed-Fi v5`` model simplifies the Student: a single ``Student.Name`` string, a ``Student.UniqueId``,
and a ``StudentIdentificationCode`` entity that carries only the identification *system*. The Ed-Fi Data Standard
(checked against the EDUcore Ed-Fi graph) has ``FirstName`` / ``MiddleName`` / ``LastSurname`` and
``StudentUniqueId`` on Student, and an identification-code common of ``IdentificationCode`` +
``StudentIdentificationSystemDescriptor`` + ``AssigningOrganizationIdentificationCode`` (on
StudentEducationOrganizationAssociation in Ed-Fi; the seed keeps it as a standalone entity, which this script
leaves in place). Descriptor-valued elements keep the seed's convention of dropping the ``Descriptor`` suffix.

For each entity in ``ALIGNMENT`` the script creates the missing attributes through the public MDR API, one per
call, each linked to the entity in the same transaction (``CreateAttributeDTO.EntityId``), and retires the seed's
non-Ed-Fi attributes (soft delete via ``DELETE /attributes/{id}``, which also drops the entity association, profile
inclusions, and transformations that reference the attribute). It is idempotent: existing attributes are left
alone and already-retired ones are skipped, so it can be re-run after a database restore.

``POST /exchange/receive`` reuses an existing (Name, Version, ContributorOrganization) model without updating
its attributes, so in the demo the script runs against BOTH the publishing district MDR and the receiving college
MDR. Nothing here touches learner data; attributes are schema-level metadata only.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# entity UniqueName -> (attributes to add as (name, description), seed attribute names to retire)
ALIGNMENT: dict[str, tuple[tuple[tuple[str, str], ...], tuple[str, ...]]] = {
    "Student": (
        (
            (
                "FirstName",
                "Ed-Fi Student.FirstName: a name given to an individual at birth, baptism, or during another naming "
                "ceremony, or through legal change.",
            ),
            (
                "MiddleName",
                "Ed-Fi Student.MiddleName: a secondary name given to an individual at birth, baptism, or during another "
                "naming ceremony.",
            ),
            ("LastSurname", "Ed-Fi Student.LastSurname: the name borne in common by members of a family."),
            ("StudentUniqueId", "Ed-Fi Student.StudentUniqueId: a unique alphanumeric code assigned to a student."),
        ),
        ("Name", "UniqueId"),
    ),
    "StudentIdentificationCode": (
        (
            (
                "IdentificationCode",
                "Ed-Fi StudentIdentificationCode.IdentificationCode: a unique number or alphanumeric code assigned to a "
                "student by a school, school system, a state, or other agency or entity. Paired with "
                "StudentIdentificationSystem (the descriptor) and AssigningOrganizationIdentificationCode.",
            ),
            (
                "AssigningOrganizationIdentificationCode",
                "Ed-Fi StudentIdentificationCode.AssigningOrganizationIdentificationCode: the organization code or name "
                "assigning the StudentIdentificationCode.",
            ),
        ),
        (),
    ),
}

RETIRED_NOTE = "Ed-Fi has no such element on Student; retired by scripts/align-edfi-student-model.py"


def _request(method: str, url: str, api_key: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method, headers={"X-API-Key": api_key, "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - local demo URL
        return json.load(response)


def _rows(payload: dict | list) -> list:
    return payload.get("data", []) if isinstance(payload, dict) else payload


def find_model_id(mdr_url: str, api_key: str, name: str, version: str) -> int:
    query = urllib.parse.urlencode({"name": name, "pagination": "false"})
    rows = [m for m in _rows(_request("GET", f"{mdr_url}/datamodels/?{query}", api_key)) if m.get("Name") == name]
    rows = [m for m in rows if str(m.get("DataModelVersion")) == version] or rows
    if not rows:
        raise SystemExit(f"data model '{name}' {version} not found on {mdr_url}")
    return int(rows[0]["Id"])


def entity_ids(mdr_url: str, api_key: str, model_id: int) -> dict[str, int]:
    rows = _rows(_request("GET", f"{mdr_url}/entities/by_data_model_id/{model_id}?page=1&size=500", api_key))
    return {e["UniqueName"]: int(e["Id"]) for e in rows}


def existing_attributes(mdr_url: str, api_key: str, entity_id: int) -> dict[str, int]:
    """UniqueName -> attribute id for the entity's active attributes."""
    rows = _rows(_request("GET", f"{mdr_url}/attributes/by_entity_id/{entity_id}?page=1&size=500", api_key))
    return {(a.get("UniqueName") or a.get("Name")): int(a["Id"]) for a in rows}


def align_entity(mdr_url: str, api_key: str, model_id: int, entity_name: str, entity_id: int) -> tuple[int, int]:
    additions, retirements = ALIGNMENT[entity_name]
    present = existing_attributes(mdr_url, api_key, entity_id)
    created = retired = 0
    for name, description in additions:
        unique_name = f"{entity_name}.{name}"
        if unique_name in present:
            print(f"  = {unique_name} already exists")
            continue
        attribute = _request(
            "POST",
            f"{mdr_url}/attributes/",
            api_key,
            {
                "Name": name,
                "UniqueName": unique_name,
                "DataType": "string",
                "DataModelId": model_id,
                "EntityId": entity_id,
                "Description": description,
                "Notes": "Added by scripts/align-edfi-student-model.py (schema-exchange demo, real Ed-Fi Student elements).",
            },
        )
        created += 1
        print(f"  + {unique_name} (attribute id {attribute.get('Id')})")
    for name in retirements:
        unique_name = f"{entity_name}.{name}"
        if unique_name not in present:
            continue
        _request("DELETE", f"{mdr_url}/attributes/{present[unique_name]}", api_key)
        retired += 1
        print(f"  - {unique_name} retired (attribute id {present[unique_name]}); {RETIRED_NOTE}")
    return created, retired


def align_model(mdr_url: str, api_key: str, model_name: str, model_version: str) -> None:
    model_id = find_model_id(mdr_url, api_key, model_name, model_version)
    ids = entity_ids(mdr_url, api_key, model_id)
    totals = [0, 0]
    for entity_name in ALIGNMENT:
        if entity_name not in ids:
            raise SystemExit(f"entity '{entity_name}' not found in data model {model_id} on {mdr_url}")
        print(f"{entity_name} (entity {ids[entity_name]}):")
        created, retired = align_entity(mdr_url, api_key, model_id, entity_name, ids[entity_name])
        totals[0] += created
        totals[1] += retired
    print(f"{mdr_url}: model '{model_name}' {model_version} (id {model_id}): {totals[0]} created, {totals[1]} retired")


def main() -> int:
    parser = argparse.ArgumentParser(description="Align an MDR Ed-Fi model's Student identity elements with Ed-Fi.")
    parser.add_argument("--mdr-url", required=True, help="MDR API base URL, e.g. http://localhost:8012")
    parser.add_argument("--api-key", default=os.environ.get("MDR_ADMIN_KEY", "changeme1"), help="MDR service API key")
    parser.add_argument(
        "--model-name", default="Northgate Ed-Fi v5", help="Data model name (default: Northgate Ed-Fi v5)"
    )
    parser.add_argument("--model-version", default="1.0", help="Data model version (default: 1.0)")
    args = parser.parse_args()
    try:
        align_model(args.mdr_url, args.api_key, args.model_name, args.model_version)
    except urllib.error.HTTPError as error:
        print(
            f"MDR returned {error.code} for {error.url}: {error.read().decode('utf-8', 'replace')[:400]}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
