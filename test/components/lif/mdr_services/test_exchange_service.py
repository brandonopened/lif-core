"""DB-free tests for the schema-exchange bundle contract (checksum/manifest helpers, DTO validation,
and the receive-side guards that run before any database access)."""

import hashlib
import json
from unittest import mock

import pytest
from fastapi import HTTPException
from lif.mdr_dto.exchange_dto import (
    EXCHANGE_FORMAT_VERSION,
    ExchangeBundleDTO,
    ExchangeBundleError,
    ExchangeCatalogDTO,
    ExchangeReceiveResultDTO,
    build_manifest,
    canonical_json,
    compute_checksum,
    verify_bundle,
)
from lif.mdr_services.exchange_service import receive_bundle
from pydantic import ValidationError


def _data_model_bundle() -> dict:
    bundle = {
        "dataModel": {
            "name": "District LIF",
            "version": "1.0",
            "type": "OrgLIF",
            "description": None,
            "openapi": {"openapi": "3.0.0", "components": {"schemas": {"Person": {"type": "object"}}}},
        }
    }
    bundle["manifest"] = build_manifest("data-model", "Lakeside USD", "District LIF", "1.0", bundle)
    return bundle


# --- checksum / manifest ------------------------------------------------------------------------


def test_canonical_json_is_key_sorted_compact_and_utf8():
    assert canonical_json({"b": 1, "a": {"d": "é", "c": [1, 2]}}) == '{"a":{"c":[1,2],"d":"é"},"b":1}'.encode("utf-8")


def test_compute_checksum_ignores_manifest_and_matches_spec_formula():
    payload = {"dataModel": {"name": "x", "openapi": {"k": "v"}}}
    expected = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
    )
    assert compute_checksum(payload) == expected
    assert compute_checksum({**payload, "manifest": {"anything": "here"}}) == expected


def test_build_manifest_shape():
    bundle = _data_model_bundle()
    manifest = bundle["manifest"]
    assert manifest["kind"] == "data-model"
    assert manifest["formatVersion"] == EXCHANGE_FORMAT_VERSION
    assert manifest["publisher"] == "Lakeside USD"
    assert manifest["name"] == "District LIF"
    assert manifest["version"] == "1.0"
    assert manifest["exportedAt"].endswith("Z")
    assert manifest["checksum"] == compute_checksum(bundle)
    verify_bundle(bundle)  # a freshly built bundle verifies


def test_verify_bundle_rejects_tampered_content():
    bundle = _data_model_bundle()
    bundle["dataModel"]["openapi"]["components"]["schemas"]["Person"]["type"] = "string"
    with pytest.raises(ExchangeBundleError, match="checksum"):
        verify_bundle(bundle)


def test_verify_bundle_rejects_unknown_format_version():
    bundle = _data_model_bundle()
    bundle["manifest"]["formatVersion"] = "2"
    with pytest.raises(ExchangeBundleError, match="formatVersion"):
        verify_bundle(bundle)


def test_verify_bundle_accepts_missing_checksum():
    bundle = _data_model_bundle()
    del bundle["manifest"]["checksum"]
    verify_bundle(bundle)


def test_verify_bundle_requires_manifest():
    with pytest.raises(ExchangeBundleError, match="manifest"):
        verify_bundle({"dataModel": {}})


# --- DTO validation -----------------------------------------------------------------------------


def test_bundle_dto_parses_transformation_group_bundle_ignoring_db_ids():
    bundle = {
        "manifest": {
            "kind": "transformation-group",
            "formatVersion": "1",
            "publisher": "p",
            "exportedAt": "2026-09-03T00:00:00Z",
            "name": "g",
            "version": "1.0",
        },
        "sourceDataModel": {"name": "s", "version": "1.0", "type": "SourceSchema", "openapi": {}},
        "targetDataModel": {"name": "t", "version": "1.0", "type": "SourceSchema", "openapi": {}},
        "transformationGroup": {
            "Id": 42,
            "SourceDataModelId": 7,
            "TargetDataModelId": 8,
            "Name": "g",
            "GroupVersion": "1.0",
            "Transformations": [
                {
                    "Id": 9,
                    "TransformationGroupId": 42,
                    "Name": "t1",
                    "Expression": "{}",
                    "ExpressionLanguage": "JSONata",
                    "SourceAttributes": [{"AttributeId": 1, "EntityIdPath": "7:person,7:~person.name"}],
                    "TargetAttribute": {"AttributeId": 2, "EntityIdPath": "8:user,8:~user.name"},
                }
            ],
        },
    }
    parsed = ExchangeBundleDTO.model_validate(bundle)
    assert parsed.manifest.kind == "transformation-group"
    assert parsed.dataModel is None
    assert parsed.transformationGroup is not None
    assert parsed.transformationGroup.Transformations is not None
    assert parsed.transformationGroup.Transformations[0].TargetAttribute is not None
    assert parsed.transformationGroup.Transformations[0].TargetAttribute.EntityIdPath == "8:user,8:~user.name"
    assert not hasattr(parsed.transformationGroup, "Id")


def test_bundle_dto_rejects_unknown_kind():
    bundle = _data_model_bundle()
    bundle["manifest"]["kind"] = "value-set"
    with pytest.raises(ValidationError):
        ExchangeBundleDTO.model_validate(bundle)


def test_catalog_and_result_dtos_validate_spec_shapes():
    catalog = ExchangeCatalogDTO.model_validate(
        {
            "publisher": "Lakeside USD",
            "dataModels": [{"id": 1, "name": "District LIF", "version": "1.0", "type": "OrgLIF", "state": "Published"}],
            "transformationGroups": [
                {
                    "id": 3,
                    "name": "SIS -> LIF",
                    "version": "1.0",
                    "sourceDataModel": {"id": 2, "name": "SIS", "version": "1.0"},
                    "targetDataModel": {"id": 1, "name": "District LIF", "version": "1.0"},
                }
            ],
        }
    )
    assert catalog.dataModels[0].description is None

    result = ExchangeReceiveResultDTO.model_validate(
        {
            "dataModels": [{"name": "SIS", "version": "1.0", "id": 10, "status": "created"}],
            "transformationGroup": {
                "id": 11,
                "version": "1.0",
                "importedTransformationCount": 2,
                "skippedTransformationCount": 1,
                "skippedTransformations": [{"TransformationName": "t", "Reason": "missing target path"}],
            },
        }
    )
    assert result.transformationGroup is not None
    assert result.transformationGroup.skippedTransformations[0].Reason == "missing target path"
    with pytest.raises(ValidationError):
        ExchangeReceiveResultDTO.model_validate(
            {"dataModels": [{"name": "SIS", "version": "1.0", "id": 10, "status": "replaced"}]}
        )


# --- receive guards (run before any DB access) ----------------------------------------------------


async def _receive(bundle: dict) -> HTTPException:
    session = mock.AsyncMock()
    with pytest.raises(HTTPException) as exc_info:
        await receive_bundle(
            session=session,
            raw_bundle=bundle,
            data_model_type="SourceSchema",
            contributor_organization=None,
            allow_missing_paths=True,
        )
    session.execute.assert_not_awaited()
    return exc_info.value


async def test_receive_checksum_mismatch_is_400():
    bundle = _data_model_bundle()
    bundle["dataModel"]["name"] = "Tampered"
    error = await _receive(bundle)
    assert error.status_code == 400
    assert "checksum" in str(error.detail)


async def test_receive_unknown_format_version_is_400():
    bundle = _data_model_bundle()
    bundle["manifest"]["formatVersion"] = "0"
    error = await _receive(bundle)
    assert error.status_code == 400
    assert "formatVersion" in str(error.detail)


async def test_receive_group_bundle_missing_sections_is_400():
    bundle = {"sourceDataModel": {"name": "s", "version": "1.0", "type": "SourceSchema", "openapi": {}}}
    bundle["manifest"] = build_manifest("transformation-group", "p", "g", "1.0", bundle)
    error = await _receive(bundle)
    assert error.status_code == 400
    assert "missing sections" in str(error.detail)
