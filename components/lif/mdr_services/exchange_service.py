"""Schema exchange between MDR instances (``/exchange``).

The publishing side (catalog + bundles) only reads Published content; the receiving side re-creates
the bundled data models and transformation group locally. The bundle contract and its checksum
helpers live in ``lif.mdr_dto.exchange_dto`` so they can be shared without a database.
"""

from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from lif.datatypes.mdr_sql_model import DataModelType, DataModel, StateType, TransformationGroup
from lif.mdr_dto.exchange_dto import (
    ExchangeBundleDTO,
    ExchangeBundleError,
    ExchangeCatalogDataModelDTO,
    ExchangeCatalogDataModelRefDTO,
    ExchangeCatalogDTO,
    ExchangeCatalogTransformationGroupDTO,
    ExchangeDataModelDTO,
    ExchangeReceivedDataModelDTO,
    ExchangeReceivedTransformationGroupDTO,
    ExchangeReceiveResultDTO,
    build_manifest,
    verify_bundle,
)
from lif.mdr_dto.transformation_group_dto import CreateTransformationGroupDTO
from lif.mdr_services.helper_service import check_datamodel_by_id
from lif.mdr_services.schema_generation_service import generate_openapi_schema
from lif.mdr_services.schema_upload_service import create_data_model_from_openapi_schema
from lif.mdr_services.transformation_service import (
    create_transformation_group,
    find_transformation_group_by_triplet,
    get_paginated_transformations_for_a_group,
    get_transformation_group_by_id,
    import_transformations_into_group,
)
from lif.mdr_utils.logger_config import get_logger
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlmodel import select

logger = get_logger(__name__)


def _data_model_ref(data_model: DataModel) -> ExchangeCatalogDataModelRefDTO:
    return ExchangeCatalogDataModelRefDTO(id=data_model.Id, name=data_model.Name, version=data_model.DataModelVersion)


async def _get_published_data_model(session: AsyncSession, data_model_id: int) -> DataModel:
    """404 if missing/deleted (via ``check_datamodel_by_id``), 409 if not Published."""
    data_model = await check_datamodel_by_id(session=session, id=data_model_id)
    if data_model.State != StateType.Published:
        raise HTTPException(
            status_code=409,
            detail=f"Data model {data_model_id} is not Published (state: {data_model.State}) and cannot be exchanged",
        )
    return data_model


async def _bundle_data_model(session: AsyncSession, data_model: DataModel, public_only: bool) -> Dict[str, Any]:
    # Same flags the round-trip upload helper uses so the bundled OpenAPI re-uploads faithfully.
    openapi = await generate_openapi_schema(
        session=session,
        data_model_id=data_model.Id,
        include_attr_md=True,
        include_entity_md=True,
        public_only=public_only,
        full_export=True,
    )
    return {
        "name": data_model.Name,
        "version": data_model.DataModelVersion,
        "type": getattr(data_model.Type, "value", data_model.Type),
        "description": data_model.Description,
        "openapi": jsonable_encoder(openapi),
    }


async def get_catalog(session: AsyncSession, publisher: str) -> ExchangeCatalogDTO:
    """Published, non-deleted data models; non-deleted groups whose source AND target are Published."""
    data_models_result = await session.execute(
        select(DataModel)
        .where(DataModel.Deleted.isnot(True), DataModel.State == StateType.Published)
        .order_by(DataModel.Id)
    )
    data_models = [
        ExchangeCatalogDataModelDTO(
            id=data_model.Id,
            name=data_model.Name,
            version=data_model.DataModelVersion,
            type=getattr(data_model.Type, "value", data_model.Type),
            state=getattr(data_model.State, "value", data_model.State),
            description=data_model.Description,
        )
        for data_model in data_models_result.scalars().all()
    ]

    source_model = aliased(DataModel)
    target_model = aliased(DataModel)
    groups_result = await session.execute(
        select(TransformationGroup, source_model, target_model)
        .join(source_model, source_model.Id == TransformationGroup.SourceDataModelId)
        .join(target_model, target_model.Id == TransformationGroup.TargetDataModelId)
        .where(
            TransformationGroup.Deleted.isnot(True),
            source_model.Deleted.isnot(True),
            source_model.State == StateType.Published,
            target_model.Deleted.isnot(True),
            target_model.State == StateType.Published,
        )
        .order_by(TransformationGroup.Id)
    )
    transformation_groups = [
        ExchangeCatalogTransformationGroupDTO(
            id=group.Id,
            name=group.Name,
            version=group.GroupVersion,
            sourceDataModel=_data_model_ref(source),
            targetDataModel=_data_model_ref(target),
        )
        for group, source, target in groups_result.all()
    ]

    return ExchangeCatalogDTO(publisher=publisher, dataModels=data_models, transformationGroups=transformation_groups)


async def build_data_model_bundle(
    session: AsyncSession, data_model_id: int, publisher: str, public_only: bool
) -> Dict[str, Any]:
    data_model = await _get_published_data_model(session, data_model_id)
    bundle: Dict[str, Any] = {"dataModel": await _bundle_data_model(session, data_model, public_only)}
    bundle["manifest"] = build_manifest("data-model", publisher, data_model.Name, data_model.DataModelVersion, bundle)
    return bundle


async def build_transformation_group_bundle(
    session: AsyncSession, transformation_group_id: int, publisher: str
) -> Dict[str, Any]:
    group = await get_transformation_group_by_id(session=session, id=transformation_group_id)
    source_data_model = await _get_published_data_model(session, group.SourceDataModelId)
    target_data_model = await _get_published_data_model(session, group.TargetDataModelId)

    total_count, group_data = await get_paginated_transformations_for_a_group(
        session=session, group_id=transformation_group_id, pagination=False, make_exportable=True
    )
    if total_count == 0:
        # Same rule (and message) as GET /transformation_groups/{id}/export.
        raise HTTPException(
            status_code=400,
            detail=(
                "There are no valid transformations to export for this group / version. "
                "Please add a transformation to this group's version and retry the export."
            ),
        )

    bundle: Dict[str, Any] = {
        "sourceDataModel": await _bundle_data_model(session, source_data_model, public_only=False),
        "targetDataModel": await _bundle_data_model(session, target_data_model, public_only=False),
        "transformationGroup": jsonable_encoder(group_data),
    }
    bundle["manifest"] = build_manifest("transformation-group", publisher, group.Name, group.GroupVersion or "", bundle)
    return bundle


async def _receive_data_model(
    session: AsyncSession, bundled: ExchangeDataModelDTO, data_model_type: str, contributor_organization: str
) -> tuple[DataModel, ExchangeReceivedDataModelDTO]:
    """Reuse an existing model, else create it (uncommitted).

    Match order: a non-deleted (Name, Version, ContributorOrganization) row first; then a non-deleted
    (Name, Version) row that is one of this MDR's own native models (BaseLIF/OrgLIF/PartnerLIF), whatever
    its organization. The fallback is what lets a group bundle drafted against the receiver's OWN
    published model (e.g. the college's "Riverbend CC LIF", org "Riverbend") land on that model instead of
    spawning a duplicate SourceSchema copy tagged with the peer's organization. Received copies are
    SourceSchema, so two peers' same-named models never collapse into each other.
    """
    by_name_version = select(DataModel).where(
        DataModel.Name == bundled.name, DataModel.DataModelVersion == bundled.version, DataModel.Deleted.isnot(True)
    )
    existing = (
        (await session.execute(by_name_version.where(DataModel.ContributorOrganization == contributor_organization)))
        .scalars()
        .first()
    )
    if existing is None:
        native_types = [DataModelType.BaseLIF.value, DataModelType.OrgLIF.value, DataModelType.PartnerLIF.value]
        existing = (
            (await session.execute(by_name_version.where(DataModel.Type.in_(native_types)).order_by(DataModel.Id)))
            .scalars()
            .first()
        )
    if existing:
        return existing, ExchangeReceivedDataModelDTO(
            name=existing.Name, version=existing.DataModelVersion, id=existing.Id, status="exists"
        )

    created = await create_data_model_from_openapi_schema(
        session=session,
        openapi_schema=bundled.openapi,
        data_model_name=bundled.name,
        data_model_version=bundled.version,
        data_model_type=data_model_type,
        data_model_description=bundled.description,
        base_data_model_id=None,
        use_considerations=None,
        notes=None,
        activation_date=None,
        deprecation_date=None,
        contributor=None,
        contributor_organization=contributor_organization,
        state=StateType.Draft.value,
        commit=False,
    )
    data_model = await check_datamodel_by_id(session=session, id=created.Id)
    return data_model, ExchangeReceivedDataModelDTO(
        name=created.Name, version=created.DataModelVersion or "", id=created.Id, status="created"
    )


async def receive_bundle(
    session: AsyncSession,
    raw_bundle: Dict[str, Any],
    data_model_type: str,
    contributor_organization: Optional[str],
    allow_missing_paths: bool,
) -> ExchangeReceiveResultDTO:
    """Apply a bundle of either kind in one transaction (commit at the end, rollback on any failure).

    ``data_model_type`` defaults to SourceSchema at the endpoint: PartnerLIF/OrgLIF models are
    defined by inclusions from a base model, which a bundled OpenAPI export does not carry, so the
    prototype deliberately does not create them.
    """
    # The checksum covers the raw JSON, so verify before pydantic normalizes anything.
    try:
        verify_bundle(raw_bundle)
        bundle = ExchangeBundleDTO.model_validate(raw_bundle)
    except (ExchangeBundleError, ValidationError) as error:
        raise HTTPException(status_code=400, detail=f"Invalid exchange bundle: {error}") from error

    organization = contributor_organization or bundle.manifest.publisher
    if bundle.manifest.kind == "data-model":
        bundled_models = [bundle.dataModel]
    else:
        bundled_models = [bundle.sourceDataModel, bundle.targetDataModel]
    if any(model is None for model in bundled_models) or (
        bundle.manifest.kind == "transformation-group" and bundle.transformationGroup is None
    ):
        raise HTTPException(
            status_code=400, detail=f"Invalid exchange bundle: missing sections for kind '{bundle.manifest.kind}'"
        )

    try:
        received_models: List[ExchangeReceivedDataModelDTO] = []
        data_models: List[DataModel] = []
        for bundled in bundled_models:
            assert bundled is not None  # narrowed by the check above
            data_model, received = await _receive_data_model(session, bundled, data_model_type, organization)
            data_models.append(data_model)
            received_models.append(received)

        if bundle.manifest.kind == "data-model" or bundle.transformationGroup is None:
            await session.commit()
            return ExchangeReceiveResultDTO(dataModels=received_models, transformationGroup=None)

        source_data_model, target_data_model = data_models
        group_version = bundle.manifest.version
        existing_group = await find_transformation_group_by_triplet(
            session=session,
            source_id=source_data_model.Id,
            target_id=target_data_model.Id,
            group_version=group_version,
            include_deleted=False,
        )
        if existing_group:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"A transformation group already exists at version '{group_version}' for these data models. "
                    "Editing an existing version via exchange is not supported."
                ),
            )

        group_data = bundle.transformationGroup
        new_group = await create_transformation_group(
            session=session,
            data=CreateTransformationGroupDTO(
                SourceDataModelId=source_data_model.Id,
                TargetDataModelId=target_data_model.Id,
                GroupVersion=group_version,
                Name=group_data.Name or bundle.manifest.name,
                Description=group_data.Description,
                Notes=group_data.Notes,
                CreationDate=group_data.CreationDate,
                ActivationDate=group_data.ActivationDate,
                DeprecationDate=group_data.DeprecationDate,
                Contributor=group_data.Contributor,
                ContributorOrganization=group_data.ContributorOrganization,
            ),
            commit=False,
        )
        imported_count, skipped, had_path_non_match = await import_transformations_into_group(
            session=session,
            group_id=new_group.Id,
            source_data_model=source_data_model,
            target_data_model=target_data_model,
            transformations=group_data.Transformations or [],
        )
        if (had_path_non_match and not allow_missing_paths) or imported_count == 0:
            skipped_detail = [skip.model_dump() for skip in skipped]
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "No transformations were received; the bundle was not applied.",
                    "skippedTransformations": skipped_detail,
                },
            )

        await session.commit()
        return ExchangeReceiveResultDTO(
            dataModels=received_models,
            transformationGroup=ExchangeReceivedTransformationGroupDTO(
                id=new_group.Id,
                version=group_version,
                importedTransformationCount=imported_count,
                skippedTransformationCount=len(skipped),
                skippedTransformations=skipped,
            ),
        )
    except Exception:
        await session.rollback()
        raise
