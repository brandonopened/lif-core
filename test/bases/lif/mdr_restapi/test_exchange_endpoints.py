"""End-to-end tests for the schema-exchange endpoints (``/exchange``) against a live Postgres.

Both roles are exercised in the same database: the "publisher" side serves the catalog/bundles and the
"receiver" side re-creates the content under a different contributor organization, then the Translator
proves the received transformation group behaves like the original.
"""

import copy
import inspect
from pathlib import Path
from unittest import mock

import pytest
from lif.mdr_auth import core as auth_core
from lif.mdr_dto.exchange_dto import EXCHANGE_FORMAT_VERSION, compute_checksum

from test.utils.lif.datasets.transform_with_embeddings.loader import DatasetTransformWithEmbeddings
from test.utils.lif.mdr.api import create_data_model_by_upload, create_transformation
from test.utils.lif.translator.api import create_translation

FIXTURE_SCHEMA = Path(__file__).parent / "data_model_example_datasource_full_openapi_schema.json"
PARTNER_KEY = "test-exchange-partner-key"


async def _publish(async_client_mdr, data_model_id, headers):
    response = await async_client_mdr.put(f"/datamodels/{data_model_id}", headers=headers, json={"State": "Published"})
    assert response.status_code == 200, response.text


async def _catalog(async_client_mdr, headers) -> dict:
    response = await async_client_mdr.get("/exchange/catalog", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def _prepare_published_group(async_client_mdr, test_case_name: str) -> DatasetTransformWithEmbeddings:
    """Source/target models + a group with two JSONata transformations, all Published."""
    dataset = await DatasetTransformWithEmbeddings.prepare(
        async_client_mdr=async_client_mdr,
        source_data_model_name=test_case_name,
        target_data_model_name=test_case_name,
        transformation_group_name=test_case_name,
    )
    await create_transformation(
        async_client_mdr=async_client_mdr,
        transformation_group_id=dataset.transformation_group_id,
        source_parent_entity_id=None,
        source_attribute_id=dataset.flow1_source_attribute_id,
        source_entity_path=dataset.flow1_source_entity_id_path,
        target_parent_entity_id=None,
        target_attribute_id=dataset.flow1_target_attribute_id,
        target_entity_path=dataset.flow1_target_entity_id_path,
        mapping_expression=(
            '{ "User": { "Workplace": { "Abilities": { "Skills": '
            '{ "LevelOfSkillAbility": Person.Employment.SkillsGainedFromCourses.SkillLevel } } } } }'
        ),
        transformation_name="User.Workplace.Abilities.Skills.LevelOfSkillAbility",
    )
    await create_transformation(
        async_client_mdr=async_client_mdr,
        transformation_group_id=dataset.transformation_group_id,
        source_parent_entity_id=None,
        source_attribute_id=dataset.flow2_source_attribute_id,
        source_entity_path=dataset.flow2_source_entity_id_path,
        target_parent_entity_id=None,
        target_attribute_id=dataset.flow2_target_attribute_id,
        target_entity_path=dataset.flow2_target_entity_id_path,
        mapping_expression=(
            '{ "User": { "Abilities": { "Skills": '
            '{ "LevelOfSkillAbility": Person.Employment.Profession.DurationAtProfession } } } }'
        ),
        transformation_name="User.Abilities.Skills.LevelOfSkillAbility",
    )
    headers = {"X-API-Key": "changeme1"}
    await _publish(async_client_mdr, dataset.source_data_model_id, headers)
    await _publish(async_client_mdr, dataset.target_data_model_id, headers)
    return dataset


@pytest.mark.asyncio
async def test_catalog_lists_only_published_content(async_client_mdr, mdr_api_headers):
    test_case_name = inspect.currentframe().f_code.co_name
    dataset = await DatasetTransformWithEmbeddings.prepare(
        async_client_mdr=async_client_mdr,
        source_data_model_name=test_case_name,
        target_data_model_name=test_case_name,
        transformation_group_name=test_case_name,
    )

    # Freshly uploaded models are Draft: neither they nor their group are advertised.
    catalog = await _catalog(async_client_mdr, mdr_api_headers)
    assert catalog["publisher"] == "unnamed-mdr"
    assert dataset.source_data_model_id not in {dm["id"] for dm in catalog["dataModels"]}
    assert dataset.transformation_group_id not in {g["id"] for g in catalog["transformationGroups"]}

    # Publishing only the source is not enough for the group (both ends must be Published).
    await _publish(async_client_mdr, dataset.source_data_model_id, mdr_api_headers)
    catalog = await _catalog(async_client_mdr, mdr_api_headers)
    source_entry = next(dm for dm in catalog["dataModels"] if dm["id"] == dataset.source_data_model_id)
    assert source_entry == {
        "id": dataset.source_data_model_id,
        "name": f"{test_case_name}_source",
        "version": "1.0",
        "type": "SourceSchema",
        "state": "Published",
        "description": None,
    }
    assert dataset.transformation_group_id not in {g["id"] for g in catalog["transformationGroups"]}

    await _publish(async_client_mdr, dataset.target_data_model_id, mdr_api_headers)
    catalog = await _catalog(async_client_mdr, mdr_api_headers)
    group_entry = next(g for g in catalog["transformationGroups"] if g["id"] == dataset.transformation_group_id)
    assert group_entry == {
        "id": dataset.transformation_group_id,
        "name": f"{test_case_name}_transform_group",
        "version": "1.0",
        "sourceDataModel": {"id": dataset.source_data_model_id, "name": f"{test_case_name}_source", "version": "1.0"},
        "targetDataModel": {"id": dataset.target_data_model_id, "name": f"{test_case_name}_target", "version": "1.0"},
    }


@pytest.mark.asyncio
async def test_data_model_bundle_requires_published_model(async_client_mdr, mdr_api_headers):
    test_case_name = inspect.currentframe().f_code.co_name
    (data_model_id, _) = await create_data_model_by_upload(
        async_client_mdr=async_client_mdr,
        schema_path=FIXTURE_SCHEMA,
        data_model_name=test_case_name,
        data_model_type="SourceSchema",
    )

    response = await async_client_mdr.get(f"/exchange/bundles/data-models/{data_model_id}", headers=mdr_api_headers)
    assert response.status_code == 409, response.text

    response = await async_client_mdr.get("/exchange/bundles/data-models/999999", headers=mdr_api_headers)
    assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_data_model_bundle_round_trips_through_receive(async_client_mdr, mdr_api_headers):
    test_case_name = inspect.currentframe().f_code.co_name
    (data_model_id, exported_schema) = await create_data_model_by_upload(
        async_client_mdr=async_client_mdr,
        schema_path=FIXTURE_SCHEMA,
        data_model_name=test_case_name,
        data_model_type="SourceSchema",
    )
    await _publish(async_client_mdr, data_model_id, mdr_api_headers)

    response = await async_client_mdr.get(f"/exchange/bundles/data-models/{data_model_id}", headers=mdr_api_headers)
    assert response.status_code == 200, response.text
    bundle = response.json()
    manifest = bundle["manifest"]
    assert manifest["kind"] == "data-model"
    assert manifest["formatVersion"] == EXCHANGE_FORMAT_VERSION
    assert manifest["publisher"] == "unnamed-mdr"
    assert manifest["name"] == test_case_name
    assert manifest["version"] == "1.0"
    assert manifest["checksum"] == compute_checksum(bundle)
    assert bundle["dataModel"]["type"] == "SourceSchema"
    assert set(bundle["dataModel"]["openapi"]["components"]["schemas"]) == set(exported_schema["components"]["schemas"])

    # Receive into the same DB under the peer's contributor organization -> a new Draft model.
    response = await async_client_mdr.post(
        "/exchange/receive",
        headers=mdr_api_headers,
        params={"contributor_organization": f"{test_case_name}_peer"},
        json=bundle,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["transformationGroup"] is None
    assert len(result["dataModels"]) == 1
    received = result["dataModels"][0]
    assert received["status"] == "created"
    assert received["name"] == test_case_name
    assert received["version"] == "1.0"
    assert received["id"] != data_model_id

    details = await async_client_mdr.get(f"/datamodels/{received['id']}", headers=mdr_api_headers)
    assert details.status_code == 200, details.text
    assert details.json()["State"] == "Draft"
    assert details.json()["Type"] == "SourceSchema"
    assert details.json()["ContributorOrganization"] == f"{test_case_name}_peer"

    re_exported = await async_client_mdr.get(
        f"/datamodels/open_api_schema/{received['id']}?include_entity_md=true&include_attr_md=true&full_export=true",
        headers=mdr_api_headers,
    )
    assert re_exported.status_code == 200, re_exported.text
    assert set(re_exported.json()["components"]["schemas"]) == set(exported_schema["components"]["schemas"])

    # Receiving the same bundle again reuses the existing model instead of failing.
    response = await async_client_mdr.post(
        "/exchange/receive",
        headers=mdr_api_headers,
        params={"contributor_organization": f"{test_case_name}_peer"},
        json=bundle,
    )
    assert response.status_code == 200, response.text
    assert response.json()["dataModels"] == [
        {"name": test_case_name, "version": "1.0", "id": received["id"], "status": "exists"}
    ]


@pytest.mark.asyncio
async def test_receive_rejects_checksum_mismatch(async_client_mdr, mdr_api_headers):
    test_case_name = inspect.currentframe().f_code.co_name
    (data_model_id, _) = await create_data_model_by_upload(
        async_client_mdr=async_client_mdr,
        schema_path=FIXTURE_SCHEMA,
        data_model_name=test_case_name,
        data_model_type="SourceSchema",
    )
    await _publish(async_client_mdr, data_model_id, mdr_api_headers)
    response = await async_client_mdr.get(f"/exchange/bundles/data-models/{data_model_id}", headers=mdr_api_headers)
    assert response.status_code == 200, response.text

    tampered = copy.deepcopy(response.json())
    tampered["dataModel"]["description"] = "tampered in transit"
    response = await async_client_mdr.post("/exchange/receive", headers=mdr_api_headers, json=tampered)
    assert response.status_code == 400, response.text
    assert "checksum" in response.json()["detail"]


@pytest.mark.asyncio
async def test_transformation_group_bundle_received_group_translates(
    async_client_mdr, async_client_translator, mdr_api_headers
):
    test_case_name = inspect.currentframe().f_code.co_name
    dataset = await _prepare_published_group(async_client_mdr, test_case_name)

    response = await async_client_mdr.get(
        f"/exchange/bundles/transformation-groups/{dataset.transformation_group_id}", headers=mdr_api_headers
    )
    assert response.status_code == 200, response.text
    bundle = response.json()
    assert bundle["manifest"]["kind"] == "transformation-group"
    assert bundle["manifest"]["name"] == f"{test_case_name}_transform_group"
    assert bundle["manifest"]["version"] == "1.0"
    assert bundle["manifest"]["checksum"] == compute_checksum(bundle)
    assert bundle["sourceDataModel"]["name"] == f"{test_case_name}_source"
    assert bundle["targetDataModel"]["name"] == f"{test_case_name}_target"
    # The group section is the regular export payload (portable named paths, JSONata only).
    assert bundle["transformationGroup"]["Id"] == dataset.transformation_group_id
    assert len(bundle["transformationGroup"]["Transformations"]) == 2
    assert all("~" in t["TargetAttribute"]["EntityIdPath"] for t in bundle["transformationGroup"]["Transformations"])

    peer_org = f"{test_case_name}_peer"
    response = await async_client_mdr.post(
        "/exchange/receive", headers=mdr_api_headers, params={"contributor_organization": peer_org}, json=bundle
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert [dm["status"] for dm in result["dataModels"]] == ["created", "created"]
    received_source_id, received_target_id = (dm["id"] for dm in result["dataModels"])
    assert received_source_id not in (dataset.source_data_model_id, dataset.target_data_model_id)
    group = result["transformationGroup"]
    assert group["version"] == "1.0"
    assert group["importedTransformationCount"] == 2
    assert group["skippedTransformationCount"] == 0
    assert group["skippedTransformations"] == []
    assert group["id"] != dataset.transformation_group_id

    # The received group drives the Translator between the RECEIVED models exactly like the original.
    translated_json = await create_translation(
        async_client_translator=async_client_translator,
        source_data_model_id=received_source_id,
        target_data_model_id=received_target_id,
        json_to_translate={
            "Person": {
                "Employment": {
                    "SkillsGainedFromCourses": {"SkillLevel": "Mastery"},
                    "Profession": {"DurationAtProfession": "10 Years"},
                }
            }
        },
        headers=mdr_api_headers,
    )
    assert translated_json == {
        "User": {
            "Workplace": {"Abilities": {"Skills": {"LevelOfSkillAbility": "Mastery"}}},
            "Abilities": {"Skills": {"LevelOfSkillAbility": "10 Years"}},
        }
    }

    # Receiving the same bundle again: models are reused, but the group version already exists.
    response = await async_client_mdr.post(
        "/exchange/receive", headers=mdr_api_headers, params={"contributor_organization": peer_org}, json=bundle
    )
    assert response.status_code == 409, response.text
    assert "already exists at version '1.0'" in response.json()["detail"]


@pytest.mark.asyncio
async def test_transformation_group_bundle_requires_published_models(async_client_mdr, mdr_api_headers):
    test_case_name = inspect.currentframe().f_code.co_name
    dataset = await DatasetTransformWithEmbeddings.prepare(
        async_client_mdr=async_client_mdr,
        source_data_model_name=test_case_name,
        target_data_model_name=test_case_name,
        transformation_group_name=test_case_name,
    )
    response = await async_client_mdr.get(
        f"/exchange/bundles/transformation-groups/{dataset.transformation_group_id}", headers=mdr_api_headers
    )
    assert response.status_code == 409, response.text

    # Published on both ends but with no exportable transformation -> same 400 as the export endpoint.
    await _publish(async_client_mdr, dataset.source_data_model_id, mdr_api_headers)
    await _publish(async_client_mdr, dataset.target_data_model_id, mdr_api_headers)
    response = await async_client_mdr.get(
        f"/exchange/bundles/transformation-groups/{dataset.transformation_group_id}", headers=mdr_api_headers
    )
    assert response.status_code == 400, response.text
    assert "no valid transformations to export" in response.json()["detail"]


@pytest.mark.asyncio
async def test_partner_key_is_confined_to_exchange_reads(async_client_mdr, mdr_api_headers):
    with mock.patch.dict(auth_core.API_KEYS, {PARTNER_KEY: auth_core.EXCHANGE_PARTNER_SERVICE_NAME}):
        partner_headers = {"X-API-Key": PARTNER_KEY}
        response = await async_client_mdr.get("/exchange/catalog", headers=partner_headers)
        assert response.status_code == 200, response.text

        response = await async_client_mdr.get("/datamodels/", headers=partner_headers)
        assert response.status_code == 403
        assert response.json() == {"detail": "Exchange partner keys are read-only and limited to /exchange"}

        response = await async_client_mdr.post("/exchange/receive", headers=partner_headers, json={})
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_receive_resolves_bundle_to_receivers_own_native_model(async_client_mdr, mdr_api_headers):
    """A bundle naming one of this MDR's own native models (here the seeded OrgLIF "StateU LIF", id 17)
    resolves to that model even when the caller's contributor organization differs — otherwise a group
    drafted by a peer against the receiver's published model would spawn a duplicate SourceSchema copy
    and the received group would point at the copy instead of the real model."""
    own_model_id = 17
    await _publish(async_client_mdr, own_model_id, mdr_api_headers)
    response = await async_client_mdr.get(f"/exchange/bundles/data-models/{own_model_id}", headers=mdr_api_headers)
    assert response.status_code == 200, response.text
    bundle = response.json()
    assert bundle["dataModel"]["type"] == "OrgLIF"

    response = await async_client_mdr.post(
        "/exchange/receive",
        headers=mdr_api_headers,
        params={"contributor_organization": "some-other-peer"},
        json=bundle,
    )
    assert response.status_code == 200, response.text
    assert response.json()["dataModels"] == [
        {
            "name": bundle["dataModel"]["name"],
            "version": bundle["dataModel"]["version"],
            "id": own_model_id,
            "status": "exists",
        }
    ]
