"""Tests for the crosswalk_draft brick (schema-exchange demo).

Fixtures are hand-written in the exact MDR OpenAPI export shape (see
test/bases/lif/mdr_restapi/data_model_example_datasource_full_openapi_schema.json): entity nodes carry
``properties`` plus ``Id``/``UniqueName``/``DataModelId``/``Array`` metadata, attribute nodes carry
``DataType``/``UniqueName``/``DataModelId`` and an optional ``enum``.
"""

import copy
import itertools
import json
import re
from pathlib import Path

import pytest

from lif.crosswalk_draft import (
    apply_value_map_to_expression,
    build_lookup_term,
    compute_checksum,
    data_model_block,
    extract_attributes,
    generate_draft_bundle,
    resolve_path,
    resolve_source_path,
)

_IDS = itertools.count(100)
ENTITY_ID_PATH_RE = re.compile(r"^\d+:[^,]+(,\d+:[^,]+)*,\d+:~[^,]+$")
REFERENCE_FILE = (
    Path(__file__).resolve().parents[4] / "reference_data" / "transformations" / "Ed-Fi-v5_StateU-LIF__v1.0.json"
)


def attribute(name: str, unique_name: str, data_model_id: int = 1, enum: list[str] | None = None) -> dict:
    node = {
        "Id": next(_IDS),
        "Name": name,
        "UniqueName": unique_name,
        "DataModelId": data_model_id,
        "DataType": "string",
        "ValueSetId": 7 if enum else None,
        "Required": "No",
        "Array": "No",
    }
    if enum:
        node["enum"] = enum
    return node


def entity(name: str, unique_name: str, properties: dict, array: bool = True, data_model_id: int = 1) -> dict:
    return {
        "type": "object",
        "required": [],
        "Id": next(_IDS),
        "Name": name,
        "UniqueName": unique_name,
        "DataModelId": data_model_id,
        "Array": "Yes" if array else "No",
        "properties": properties,
    }


def openapi(model_name: str, schemas: dict, version: str = "1.0") -> dict:
    return {
        "openapi": "3.0.0",
        "info": {
            "title": f"Machine-Readable Schema for {model_name}",
            "version": version,
            "description": "OpenAPI Spec",
        },
        "paths": {},
        "components": {"schemas": schemas},
    }


def lif_person(extra: dict | None = None) -> dict:
    """A mini LIF ``Person`` root: nested arrays, a non-array nested entity, an enum attribute."""
    return entity(
        "Person",
        "Person",
        {
            "identifier": attribute("identifier", "Person.identifier"),
            "Name": entity(
                "Name",
                "Person.Name",
                {
                    "firstName": attribute("firstName", "Person.Name.firstName"),
                    "lastName": attribute("lastName", "Person.Name.lastName"),
                },
            ),
            "SexAndGender": entity(
                "SexAndGender",
                "Person.SexAndGender",
                {"sex": attribute("sex", "Person.SexAndGender.sex", enum=["Female", "Male", "Not Selected"])},
            ),
            "Contact": entity(
                "Contact",
                "Common.Contact",
                {
                    "Email": entity(
                        "Email",
                        "Common.Contact.Email",
                        {"emailAddress": attribute("emailAddress", "Common.Contact.Email.emailAddress", 17)},
                    )
                },
            ),
            "Birth": entity(
                "Birth", "Person.Birth", {"birthDate": attribute("birthDate", "Person.Birth.birthDate")}, False
            ),
            **(extra or {}),
        },
    )


@pytest.fixture
def district_openapi() -> dict:
    """Source: LIF-derived district model with one district-only attribute."""
    return openapi(
        "District LIF", {"Person": lif_person({"districtOnly": attribute("districtOnly", "Person.districtOnly", 20)})}
    )


@pytest.fixture
def college_openapi() -> dict:
    """Target: LIF-derived college model with one college-only attribute."""
    person = lif_person()
    person["properties"]["Name"]["properties"]["middleName"] = attribute("middleName", "Person.Name.middleName", 21)
    return openapi("College LIF", {"Person": person}, version="2.0")


@pytest.fixture
def edfi_openapi() -> dict:
    """Source in Ed-Fi style: descriptor-named enum attribute, and a Course root (as in the reference file)."""
    return openapi(
        "Ed-Fi v5",
        {
            "Student": entity(
                "Student",
                "Student",
                {"SexDescriptor": attribute("SexDescriptor", "Student.SexDescriptor", 6, enum=["Female", "Male"])},
                data_model_id=6,
            ),
            "Course": entity(
                "Course", "Course", {"CourseTitle": attribute("CourseTitle", "Course.CourseTitle", 6)}, data_model_id=6
            ),
        },
    )


@pytest.fixture
def lif_with_course_openapi() -> dict:
    person = lif_person(
        {
            "CourseLearningExperience": entity(
                "CourseLearningExperience",
                "Person.CourseLearningExperience",
                {"Course": entity("Course", "Course", {"name": attribute("name", "Course.name")})},
            )
        }
    )
    return openapi("StateU LIF", {"Person": person})


def by_name(bundle: dict) -> dict[str, dict]:
    return {t["Name"]: t for t in bundle["transformationGroup"]["Transformations"]}


class TestIdentityMatching:
    def test_top_level_attribute(self, district_openapi, college_openapi):
        bundle, report = generate_draft_bundle(district_openapi, college_openapi, group_name="D->C")
        t = by_name(bundle)["Person.identifier"]
        assert t["Expression"] == '{ "Person": Person. { "identifier": identifier } }'
        assert t["SourceAttributes"] == [{"EntityIdPath": "1:Person,1:~Person.identifier"}]
        assert t["TargetAttribute"] == {"EntityIdPath": "1:Person,1:~Person.identifier"}
        assert t["Alignment"] == "identity"
        assert t["ExpressionLanguage"] == "JSONata"
        assert report["identityMatches"] == 6

    def test_nested_array_levels_and_foreign_data_model_prefix(self, district_openapi, college_openapi):
        t = by_name(generate_draft_bundle(district_openapi, college_openapi, group_name="D->C")[0])[
            "Person.Contact.Email.emailAddress"
        ]
        # Mirrors reference_data: 1:Person,1:Common.Contact,1:Common.Contact.Email,17:~Common.Contact.Email.emailAddress
        assert (
            t["TargetAttribute"]["EntityIdPath"]
            == "1:Person,1:Common.Contact,1:Common.Contact.Email,17:~Common.Contact.Email.emailAddress"
        )
        assert (
            t["Expression"]
            == '{ "Person": Person. { "Contact": [{ "Email": [{ "emailAddress": Contact.Email.emailAddress }] }] } }'
        )

    def test_non_array_nested_entity_is_wrapped_as_object(self, district_openapi, college_openapi):
        t = by_name(generate_draft_bundle(district_openapi, college_openapi, group_name="D->C")[0])[
            "Person.Birth.birthDate"
        ]
        assert t["Expression"] == '{ "Person": Person. { "Birth": { "birthDate": Birth.birthDate } } }'

    def test_unmatched_reported_on_both_sides(self, district_openapi, college_openapi):
        bundle, report = generate_draft_bundle(district_openapi, college_openapi, group_name="D->C")
        assert report["unmatchedSource"] == ["Person.districtOnly"]
        assert report["unmatchedTarget"] == ["Person.Name.middleName"]
        assert report["crosswalkMatches"] == 0
        assert report["valueLookupsApplied"] == 0
        assert report["belowConfidence"] == []
        assert "Person.Name.middleName" not in by_name(bundle)
        group = bundle["transformationGroup"]
        assert group["Name"] == "D->C"
        assert group["GroupVersion"] == "0.1"
        assert group["Description"] == "Draft generated by crosswalk_draft — review required"
        assert group["Notes"] == (
            "identity=6; crosswalk=0; authored=0; valueLookups=0; unmatchedSource=1; unmatchedTarget=1; belowConfidence=0"
        )

    def test_transformations_sorted_by_target_path(self, district_openapi, college_openapi):
        names = [
            t["Name"]
            for t in generate_draft_bundle(district_openapi, college_openapi, group_name="x")[0]["transformationGroup"][
                "Transformations"
            ]
        ]
        assert names == sorted(names)


class TestCrosswalkMatching:
    CROSSWALK = {
        "meta": {"source": "EDUcore", "sourceStandard": "EdFi", "targetStandard": "LIF", "hub": "CEDS 14.0.0.0"},
        "properties": [
            {
                "sourcePath": "Student.SexDescriptor",
                "targetPath": "Person.SexAndGender.sex",
                "confidence": 0.93,
                "matchType": "EXACT_MATCH",
                "cedsKey": "P000255",
                "cedsName": "Sex",
            },
            {
                "sourcePath": "Course.CourseTitle",
                "targetPath": "Person.CourseLearningExperience.Course.name",
                "confidence": 0.8,
                "matchType": "CLOSE_MATCH",
                "cedsKey": "P000144",
                "cedsName": "Course Title",
            },
            {
                "sourcePath": "Student.SexDescriptor",
                "targetPath": "Person.Name.firstName",
                "confidence": 0.2,
                "matchType": "CLOSE_MATCH",
                "cedsKey": "P000115",
                "cedsName": "First Name",
            },
        ],
        "values": [
            {
                "sourceOptionSet": "SexDescriptor",
                "sourceValue": "Male",
                "targetOptionSet": "Person.SexAndGender.sex",
                "targetValue": "Male",
                "confidence": 1.0,
                "cedsKey": "OV1",
                "cedsName": "Male",
            },
            {
                "sourceOptionSet": "SexDescriptor",
                "sourceValue": "Female",
                "targetOptionSet": "Person.SexAndGender.sex",
                "targetValue": "Female",
                "confidence": 1.0,
                "cedsKey": "OV2",
                "cedsName": "Female",
            },
            {
                "sourceOptionSet": "SexDescriptor",
                "sourceValue": "Unknown",
                "targetOptionSet": "Person.SexAndGender.sex",
                "targetValue": "Not Selected",
                "confidence": 0.3,
                "cedsKey": "OV3",
                "cedsName": "Not selected",
            },
        ],
    }

    def test_property_match_alignment_and_value_lookup(self, edfi_openapi, lif_with_course_openapi):
        bundle, report = generate_draft_bundle(
            edfi_openapi, lif_with_course_openapi, group_name="EdFi->LIF", crosswalk=self.CROSSWALK
        )
        t = by_name(bundle)["Person.SexAndGender.sex"]
        assert t["Alignment"] == "educore:EXACT_MATCH:0.93:P000255"
        assert t["SourceAttributes"] == [{"EntityIdPath": "6:Student,6:~Student.SexDescriptor"}]
        assert t["TargetAttribute"] == {"EntityIdPath": "1:Person,1:Person.SexAndGender,1:~Person.SexAndGender.sex"}
        # Value lookup wraps the source term; map keys sorted; below-confidence "Unknown" excluded.
        assert t["Expression"] == (
            '{ "Person": Student. { "SexAndGender": [{ "sex": $lookup({"Female": "Female", "Male": "Male"}, SexDescriptor) }] } }'
        )
        assert "value lookup: 2 values" in t["Notes"]
        assert report["crosswalkMatches"] == 2
        assert report["valueLookupsApplied"] == 1
        assert report["identityMatches"] == 0

    def test_below_confidence_rows_excluded_and_reported(self, edfi_openapi, lif_with_course_openapi):
        bundle, report = generate_draft_bundle(
            edfi_openapi, lif_with_course_openapi, group_name="EdFi->LIF", crosswalk=self.CROSSWALK
        )
        assert "Person.Name.firstName" not in by_name(bundle)
        assert report["belowConfidence"] == [
            {
                "sourcePath": "SexDescriptor=Unknown",
                "targetPath": "Person.SexAndGender.sex=Not Selected",
                "confidence": 0.3,
            },
            {"sourcePath": "Student.SexDescriptor", "targetPath": "Person.Name.firstName", "confidence": 0.2},
        ]

    def test_min_confidence_is_configurable(self, edfi_openapi, lif_with_course_openapi):
        bundle, report = generate_draft_bundle(
            edfi_openapi, lif_with_course_openapi, group_name="x", crosswalk=self.CROSSWALK, min_confidence=0.1
        )
        assert report["belowConfidence"] == []
        assert "Person.Name.firstName" in by_name(bundle)
        assert '"Unknown": "Not Selected"' in by_name(bundle)["Person.SexAndGender.sex"]["Expression"]

    def test_matches_reference_file_expression_and_paths(self, edfi_openapi, lif_with_course_openapi):
        """The Course.CourseTitle -> Person.CourseLearningExperience.Course.name case, byte-for-byte against
        reference_data/transformations/Ed-Fi-v5_StateU-LIF__v1.0.json."""
        reference = json.loads(REFERENCE_FILE.read_text(encoding="utf-8"))
        expected = next(
            t for t in reference["Transformations"] if t["Name"] == "Person.CourseLearningExperience.Course.name"
        )
        t = by_name(
            generate_draft_bundle(edfi_openapi, lif_with_course_openapi, group_name="x", crosswalk=self.CROSSWALK)[0]
        )["Person.CourseLearningExperience.Course.name"]
        assert t["Expression"] == expected["Expression"]
        assert t["SourceAttributes"][0]["EntityIdPath"] == expected["SourceAttributes"][0]["EntityIdPath"]
        assert t["TargetAttribute"]["EntityIdPath"] == expected["TargetAttribute"]["EntityIdPath"]

    def test_identity_wins_over_crosswalk_for_same_target(self, district_openapi, college_openapi):
        crosswalk = {
            "properties": [
                {
                    "sourcePath": "Person.Name.lastName",
                    "targetPath": "Person.Name.firstName",
                    "confidence": 0.99,
                    "matchType": "EXACT_MATCH",
                    "cedsKey": "X",
                }
            ]
        }
        bundle, report = generate_draft_bundle(district_openapi, college_openapi, group_name="x", crosswalk=crosswalk)
        assert by_name(bundle)["Person.Name.firstName"]["Alignment"] == "identity"
        assert report["crosswalkMatches"] == 0

    def test_trailing_path_fallback(self, edfi_openapi, lif_with_course_openapi):
        crosswalk = {
            "properties": [
                {
                    "sourcePath": "Student.SexDescriptor",
                    "targetPath": "SexAndGender.sex",
                    "confidence": 0.9,
                    "matchType": "EXACT_MATCH",
                    "cedsKey": "P000255",
                }
            ],
            "values": [
                # Source side matches via the attribute's enum values equalling the value set (no descriptor
                # name match); target side matches on the trailing "Entity.attribute" pair.
                {
                    "sourceOptionSet": "SexValueSet",
                    "sourceValue": "Female",
                    "targetOptionSet": "SexAndGender.sex",
                    "targetValue": "F",
                    "confidence": 1.0,
                },
                {
                    "sourceOptionSet": "SexValueSet",
                    "sourceValue": "Male",
                    "targetOptionSet": "SexAndGender.sex",
                    "targetValue": "M",
                    "confidence": 1.0,
                },
            ],
        }
        t = by_name(
            generate_draft_bundle(edfi_openapi, lif_with_course_openapi, group_name="x", crosswalk=crosswalk)[0]
        )["Person.SexAndGender.sex"]
        assert t["Expression"] == (
            '{ "Person": Student. { "SexAndGender": [{ "sex": $lookup({"Female": "F", "Male": "M"}, SexDescriptor) }] } }'
        )


class TestBundleEnvelope:
    def test_checksum_is_deterministic_and_excludes_manifest(self, district_openapi, college_openapi):
        bundle_a, _ = generate_draft_bundle(district_openapi, college_openapi, group_name="x", publisher="a")
        bundle_b, _ = generate_draft_bundle(district_openapi, college_openapi, group_name="x", publisher="b")
        assert bundle_a["manifest"]["checksum"] == bundle_b["manifest"]["checksum"]
        assert bundle_a["manifest"]["checksum"].startswith("sha256:")
        assert compute_checksum(bundle_a) == bundle_a["manifest"]["checksum"]
        # Hand-computed per the contract: canonical JSON of everything but "manifest".
        body = {k: v for k, v in bundle_a.items() if k != "manifest"}
        import hashlib

        expected = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        assert bundle_a["manifest"]["checksum"] == f"sha256:{expected}"
        # Content changes do change it.
        changed, _ = generate_draft_bundle(district_openapi, college_openapi, group_name="y")
        assert changed["manifest"]["checksum"] != bundle_a["manifest"]["checksum"]

    def test_manifest_and_data_model_blocks(self, district_openapi, college_openapi):
        bundle, _ = generate_draft_bundle(
            district_openapi, college_openapi, group_name="D->C", group_version="0.2", publisher="district-a"
        )
        manifest = bundle["manifest"]
        assert manifest["kind"] == "transformation-group"
        assert manifest["formatVersion"] == "1"
        assert manifest["publisher"] == "district-a"
        assert manifest["name"] == "D->C"
        assert manifest["version"] == "0.2"
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", manifest["exportedAt"])
        assert bundle["sourceDataModel"]["name"] == "District LIF"
        assert bundle["sourceDataModel"]["version"] == "1.0"
        assert bundle["sourceDataModel"]["openapi"] is district_openapi
        assert bundle["targetDataModel"]["name"] == "College LIF"
        assert bundle["targetDataModel"]["version"] == "2.0"
        assert set(bundle) == {"manifest", "sourceDataModel", "targetDataModel", "transformationGroup"}

    def test_bare_openapi_and_bundle_input_are_equivalent(self, district_openapi, college_openapi):
        wrapped_source = {
            "manifest": {"kind": "data-model"},
            "dataModel": {
                "name": "District LIF",
                "version": "1.0",
                "type": "OrgLIF",
                "openapi": copy.deepcopy(district_openapi),
            },
        }
        from_bare, report_bare = generate_draft_bundle(district_openapi, college_openapi, group_name="x")
        from_bundle, report_bundle = generate_draft_bundle(wrapped_source, college_openapi, group_name="x")
        assert from_bare["transformationGroup"] == from_bundle["transformationGroup"]
        assert report_bare == report_bundle
        assert from_bundle["sourceDataModel"]["type"] == "OrgLIF"
        assert from_bundle["sourceDataModel"]["openapi"] == district_openapi
        assert data_model_block(district_openapi)["type"] is None

    def test_entity_id_path_grammar_matches_reference_files(self, district_openapi, college_openapi):
        bundle, _ = generate_draft_bundle(district_openapi, college_openapi, group_name="x")
        paths = [t["TargetAttribute"]["EntityIdPath"] for t in bundle["transformationGroup"]["Transformations"]]
        paths += [
            s["EntityIdPath"] for t in bundle["transformationGroup"]["Transformations"] for s in t["SourceAttributes"]
        ]
        assert paths
        for path in paths:
            assert ENTITY_ID_PATH_RE.match(path), path
        reference = json.loads(REFERENCE_FILE.read_text(encoding="utf-8"))
        for t in reference["Transformations"]:
            assert ENTITY_ID_PATH_RE.match(t["TargetAttribute"]["EntityIdPath"])


class TestHelpers:
    def test_extract_attributes_skips_inlined_reference_copies(self, lif_with_course_openapi):
        person = lif_with_course_openapi["components"]["schemas"]["Person"]
        ref = copy.deepcopy(person["properties"]["CourseLearningExperience"]["properties"]["Course"])
        ref["type"] = "object"  # add_ref() forces object; Name stays "Course" while the key is "RefCourse"
        person["properties"]["RefCourse"] = ref
        paths = extract_attributes(lif_with_course_openapi)
        assert "Person.CourseLearningExperience.Course.name" in paths
        assert not any(p.startswith("Person.RefCourse") for p in paths)
        assert paths["Person.SexAndGender.sex"].enum == ("Female", "Male", "Not Selected")
        assert paths["Person.Birth.birthDate"].entity_arrays == (True, False)

    def test_extract_attributes_without_metadata_falls_back_to_names(self):
        bare = openapi("Bare", {"Person": {"type": "object", "properties": {"identifier": {"type": "string"}}}})
        assert extract_attributes(bare)["Person.identifier"].entity_id_path == "0:Person,0:~Person.identifier"

    def test_resolve_path_prefers_full_then_trailing(self, district_openapi):
        attributes = extract_attributes(district_openapi)
        assert resolve_path("Person.Name.firstName", attributes).path == "Person.Name.firstName"
        assert resolve_path("Student.Name.firstName", attributes).path == "Person.Name.firstName"
        assert resolve_path("Nope.nothing", attributes) is None

    def test_build_lookup_term_sorts_keys(self):
        assert build_lookup_term({"b": "2", "a": "1"}, "x") == '$lookup({"a": "1", "b": "2"}, x)'


class TestValueLookupRequiresBothSides:
    def test_target_only_hit_does_not_wrap_identity_copy(self, district_openapi, college_openapi):
        """An Ed-Fi->LIF value crosswalk must not be applied to a LIF->LIF identity copy: the source
        holds LIF values ("Female"), not Ed-Fi descriptor values, so a $lookup keyed on the Ed-Fi
        side would null out every real value (observed as Georgia -> null in the demo)."""
        crosswalk = {
            "values": [
                {
                    "sourceOptionSet": "SexDescriptor",
                    "sourceValue": "F",
                    "targetOptionSet": "Person.SexAndGender.sex",
                    "targetValue": "Female",
                    "confidence": 1.0,
                },
                {
                    "sourceOptionSet": "SexDescriptor",
                    "sourceValue": "M",
                    "targetOptionSet": "Person.SexAndGender.sex",
                    "targetValue": "Male",
                    "confidence": 1.0,
                },
            ]
        }
        bundle, report = generate_draft_bundle(district_openapi, college_openapi, group_name="x", crosswalk=crosswalk)
        expression = by_name(bundle)["Person.SexAndGender.sex"]["Expression"]
        assert "$lookup" not in expression
        assert report["valueLookupsApplied"] == 0


class TestAuthoredRulesAndDescriptorTolerance:
    """Ed-Fi source models in the MDR name descriptors without the suffix and without enums; authored
    (LIF-written) rules seed a draft and EDUcore value lookups are folded into them where they apply."""

    @staticmethod
    def edfi_address_openapi() -> dict:
        return openapi(
            "Ed-Fi v5",
            {
                "Address": entity(
                    "Address",
                    "Address",
                    {
                        "StateAbbreviation": attribute("StateAbbreviation", "Address.StateAbbreviation", 6),
                        "City": attribute("City", "Address.City", 6),
                    },
                    data_model_id=6,
                ),
                "BirthData": entity(
                    "BirthData",
                    "BirthData",
                    {
                        "BirthStateAbbreviation": attribute(
                            "BirthStateAbbreviation", "BirthData.BirthStateAbbreviation", 6
                        )
                    },
                    data_model_id=6,
                ),
                "Course": entity(
                    "Course",
                    "Course",
                    {
                        "OfferedGradeLevel": attribute("OfferedGradeLevel", "Course.OfferedGradeLevel", 6),
                        "CourseTitle": attribute("CourseTitle", "Course.CourseTitle", 6),
                    },
                    data_model_id=6,
                ),
            },
        )

    @staticmethod
    def lif_with_address_openapi() -> dict:
        person = lif_person()
        person["properties"]["Contact"]["properties"]["Address"] = entity(
            "Address",
            "Common.Contact.Address",
            {
                "addressState": attribute(
                    "addressState", "Common.Contact.Address.addressState", enum=["Georgia", "Alabama"]
                )
            },
        )
        return openapi("StateU LIF", {"Person": person})

    def test_resolve_source_path_descriptor_tolerance(self):
        attributes = extract_attributes(self.edfi_address_openapi())
        assert (
            resolve_source_path("Address.StateAbbreviationDescriptor", attributes).path == "Address.StateAbbreviation"
        )
        assert resolve_source_path("Course.GradeLevelDescriptor", attributes).path == "Course.OfferedGradeLevel"
        # "State" is contained in two attribute names -> ambiguous -> no match
        assert resolve_source_path("Anything.StateDescriptor", attributes) is None
        assert resolve_source_path("Course.CourseTitle", attributes).path == "Course.CourseTitle"

    def test_authored_rule_is_repointed_and_kept_verbatim(self, edfi_openapi, lif_with_course_openapi):
        rule = {
            "Name": "Person.CourseLearningExperience.Course.name",
            "Expression": '{ "Person": Course. { "CourseLearningExperience": [{ "Course": [{ "name": CourseTitle }] }] } }',
            "ExpressionLanguage": "JSONata",
            "SourceAttributes": [{"AttributeId": 999, "EntityIdPath": "42:Course,42:~Course.CourseTitle"}],
            "TargetAttribute": {
                "EntityIdPath": "17:Person,17:Person.CourseLearningExperience,17:Course,17:~Course.name"
            },
        }
        bundle, report = generate_draft_bundle(
            edfi_openapi, lif_with_course_openapi, group_name="x", authored=[rule], authored_label="lif-authored"
        )
        t = by_name(bundle)["Person.CourseLearningExperience.Course.name"]
        assert t["Expression"] == rule["Expression"]
        assert t["Alignment"] == "lif-authored"
        # EntityIdPaths are re-emitted from the two models, not copied from the file (its 42:/17: prefixes are foreign)
        assert t["SourceAttributes"] == [{"EntityIdPath": "6:Course,6:~Course.CourseTitle"}]
        assert (
            t["TargetAttribute"]["EntityIdPath"] == "1:Person,1:Person.CourseLearningExperience,1:Course,1:~Course.name"
        )
        assert report["authoredMatches"] == 1
        assert "Course.CourseTitle" not in report["unmatchedSource"]
        assert report["authoredSkipped"] == []

    def test_authored_rule_notes_are_carried_through(self, edfi_openapi, lif_with_course_openapi):
        """A hand-written rule's Notes (its rationale / caveat) must survive into the draft; a rule without
        Notes gets the seeded-from marker so provenance is never blank."""
        with_notes = {
            "Name": "Person.CourseLearningExperience.Course.name",
            "Expression": '{ "Person": Course. { "CourseLearningExperience": [{ "Course": [{ "name": CourseTitle }] }] } }',
            "Notes": "Lossy: CourseTitle is free text, not a controlled name.",
            "SourceAttributes": [{"EntityIdPath": "42:Course,42:~Course.CourseTitle"}],
            "TargetAttribute": {
                "EntityIdPath": "17:Person,17:Person.CourseLearningExperience,17:Course,17:~Course.name"
            },
        }
        without_notes = {
            "Name": "sex-by-hand",
            "Expression": '{ "Person": Student. { "SexAndGender": [{ "sex": SexDescriptor }] } }',
            "SourceAttributes": [{"EntityIdPath": "6:Student,6:~Student.SexDescriptor"}],
            "TargetAttribute": {"EntityIdPath": "1:Person,1:Person.SexAndGender,1:~Person.SexAndGender.sex"},
        }
        bundle, _ = generate_draft_bundle(
            edfi_openapi, lif_with_course_openapi, group_name="x", authored=[with_notes, without_notes]
        )
        rows = by_name(bundle)
        assert rows["Person.CourseLearningExperience.Course.name"]["Notes"] == with_notes["Notes"]
        assert rows["Person.SexAndGender.sex"]["Notes"] == "seeded from authored rule 'sex-by-hand'"

    def test_notes_overlay_replaces_authored_appends_generated_and_reports_unmatched(
        self, edfi_openapi, lif_with_course_openapi
    ):
        """A notes overlay keyed by target path: authored rules take the note verbatim, generated (EDUcore)
        rows keep their provenance text and gain the note, attribute-level notes land on the matching source
        leg, and keys that hit no rule are reported instead of silently ignored."""
        crosswalk = {
            "properties": [
                {
                    "sourcePath": "Student.SexDescriptor",
                    "targetPath": "Person.SexAndGender.sex",
                    "confidence": 0.9,
                    "matchType": "EXACT_MATCH",
                    "cedsKey": "P000255",
                }
            ]
        }
        authored = [
            {
                "Name": "Person.CourseLearningExperience.Course.name",
                "Expression": '{ "Person": Course. { "CourseLearningExperience": [{ "Course": [{ "name": CourseTitle }] }] } }',
                "SourceAttributes": [{"EntityIdPath": "6:Course,6:~Course.CourseTitle"}],
                "TargetAttribute": {
                    "EntityIdPath": "17:Person,17:Person.CourseLearningExperience,17:Course,17:~Course.name"
                },
            }
        ]
        notes = {
            "Person.CourseLearningExperience.Course.name": {
                "Notes": "Direct copy of CourseTitle.",
                "SourceAttributeNotes": {"Course.CourseTitle": "Free text, not a controlled name."},
                "TargetAttributeNotes": "Riverbend shows this on the transcript.",
            },
            "Person.SexAndGender.sex": "Descriptor copied verbatim.",
            "Person.Nope.nothing": "no such rule",
        }
        bundle, report = generate_draft_bundle(
            edfi_openapi,
            lif_with_course_openapi,
            group_name="x",
            crosswalk=crosswalk,
            authored=authored,
            rule_notes=notes,
        )
        rows = by_name(bundle)
        course = rows["Person.CourseLearningExperience.Course.name"]
        assert course["Notes"] == "Direct copy of CourseTitle."
        assert course["SourceAttributes"][0]["Notes"] == "Free text, not a controlled name."
        assert course["TargetAttribute"]["Notes"] == "Riverbend shows this on the transcript."
        sex = rows["Person.SexAndGender.sex"]
        assert sex["Notes"].startswith("EDUcore crosswalk") and sex["Notes"].endswith("; Descriptor copied verbatim.")
        assert report["notesApplied"] == 2
        assert report["notesUnmatched"] == ["Person.Nope.nothing"]

    def test_authored_rule_skipped_when_unresolved_or_target_taken(self, edfi_openapi, lif_with_course_openapi):
        crosswalk = {
            "properties": [
                {
                    "sourcePath": "Student.SexDescriptor",
                    "targetPath": "Person.SexAndGender.sex",
                    "confidence": 0.9,
                    "matchType": "EXACT_MATCH",
                    "cedsKey": "P000255",
                }
            ]
        }
        authored = [
            {
                "Name": "nowhere",
                "Expression": "$",
                "SourceAttributes": [{"EntityIdPath": "6:Course,6:~Course.CourseTitle"}],
                "TargetAttribute": {"EntityIdPath": "1:Nope,1:~Nope.nothing"},
            },
            {
                "Name": "sex-by-hand",
                "Expression": '{ "Person": Student. { "SexAndGender": [{ "sex": SexDescriptor }] } }',
                "SourceAttributes": [{"EntityIdPath": "6:Student,6:~Student.SexDescriptor"}],
                "TargetAttribute": {"EntityIdPath": "1:Person,1:Person.SexAndGender,1:~Person.SexAndGender.sex"},
            },
        ]
        bundle, report = generate_draft_bundle(
            edfi_openapi, lif_with_course_openapi, group_name="x", crosswalk=crosswalk, authored=authored
        )
        assert by_name(bundle)["Person.SexAndGender.sex"]["Alignment"].startswith("educore:")
        reasons = {row["name"]: row["reason"] for row in report["authoredSkipped"]}
        assert reasons["nowhere"].startswith("unresolved")
        assert reasons["sex-by-hand"] == "target already mapped: Person.SexAndGender.sex"
        assert report["authoredMatches"] == 0

    def test_value_lookup_folded_into_authored_expression(self, edfi_openapi, lif_with_course_openapi):
        crosswalk = {
            "values": [
                {
                    "sourceOptionSet": "SexDescriptor",
                    "sourceValue": v,
                    "targetOptionSet": "Person.SexAndGender.sex",
                    "targetValue": v,
                    "confidence": 1.0,
                }
                for v in ("Female", "Male")
            ]
        }
        authored = [
            {
                "Name": "sex-by-hand",
                "Expression": '{ "Person": Student. { "SexAndGender": [{ "sex": SexDescriptor }] } }',
                "SourceAttributes": [{"EntityIdPath": "6:Student,6:~Student.SexDescriptor"}],
                "TargetAttribute": {"EntityIdPath": "1:Person,1:Person.SexAndGender,1:~Person.SexAndGender.sex"},
            }
        ]
        bundle, report = generate_draft_bundle(
            edfi_openapi, lif_with_course_openapi, group_name="x", crosswalk=crosswalk, authored=authored
        )
        t = by_name(bundle)["Person.SexAndGender.sex"]
        assert t["Expression"] == (
            '{ "Person": Student. { "SexAndGender": [{ "sex": $lookup({"Female": "Female", "Male": "Male"}, SexDescriptor) }] } }'
        )
        assert t["Alignment"] == "lif-authored+educore-values"
        assert report["valueLookupsApplied"] == 1

    def test_enum_less_descriptor_source_gets_lookup_via_stem(self):
        """MDR's Ed-Fi ``Address.StateAbbreviation`` has no enum; the EDUcore option set is
        ``StateAbbreviationDescriptor``. Stem tolerance applies the GA -> Georgia lookup."""
        crosswalk = {
            "properties": [
                {
                    "sourcePath": "Address.StateAbbreviationDescriptor",
                    "targetPath": "Person.Contact.Address.addressState",
                    "confidence": 0.8,
                    "matchType": "VALUE_SET_EQUIVALENCE",
                    "cedsKey": "",
                }
            ],
            "values": [
                {
                    "sourceOptionSet": "StateAbbreviationDescriptor",
                    "sourceValue": "GA",
                    "targetOptionSet": "Person.Contact.Address.addressState",
                    "targetValue": "Georgia",
                    "confidence": 1.0,
                }
            ],
        }
        bundle, report = generate_draft_bundle(
            self.edfi_address_openapi(), self.lif_with_address_openapi(), group_name="x", crosswalk=crosswalk
        )
        t = by_name(bundle)["Person.Contact.Address.addressState"]
        assert '$lookup({"GA": "Georgia"}, StateAbbreviation)' in t["Expression"]
        assert t["Alignment"] == "educore:VALUE_SET_EQUIVALENCE:0.8:"
        assert report["crosswalkMatches"] == 1 and report["valueLookupsApplied"] == 1

    def test_apply_value_map_to_expression_edge_cases(self):
        value_map = {"GA": "Georgia"}
        # quoted key is not a term; the bare term is rewritten once
        rewritten = apply_value_map_to_expression(
            '{ "Address": [{ "StateAbbreviation": StateAbbreviation }] }', "StateAbbreviation", value_map
        )
        assert rewritten == '{ "Address": [{ "StateAbbreviation": $lookup({"GA": "Georgia"}, StateAbbreviation) }] }'
        # term inside a function call still counts as the single bare occurrence
        assert "$lookup(" in apply_value_map_to_expression('{ "n": $trim(Name) }', "Name", value_map)
        # repeated term, missing term, or an existing lookup -> leave the expression alone
        assert apply_value_map_to_expression("Name & Name", "Name", value_map) is None
        assert apply_value_map_to_expression('{ "n": Other }', "Name", value_map) is None
        assert apply_value_map_to_expression("$lookup({}, Name)", "Name", value_map) is None
