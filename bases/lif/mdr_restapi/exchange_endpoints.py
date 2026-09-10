"""Schema exchange between MDR instances (pull-based; see ADR 0002 — no partner credentials stored).

A publishing MDR serves ``GET /exchange/catalog`` and ``GET /exchange/bundles/...``; a peer MDR
fetches a bundle and POSTs it to its own ``/exchange/receive``.
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from lif.datatypes.mdr_sql_model import DataModelType
from lif.mdr_dto.exchange_dto import ExchangeCatalogDTO, ExchangeReceiveResultDTO
from lif.mdr_services import exchange_service
from lif.mdr_utils.config import get_settings
from lif.mdr_utils.database_setup import get_session
from lif.mdr_utils.logger_config import get_logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()
logger = get_logger(__name__)


def _publisher() -> str:
    return get_settings().mdr__exchange__publisher_name


@router.get("/catalog", response_model=ExchangeCatalogDTO)
async def get_exchange_catalog(session: AsyncSession = Depends(get_session)):
    return await exchange_service.get_catalog(session=session, publisher=_publisher())


@router.get("/bundles/data-models/{data_model_id}")
async def get_data_model_bundle(
    data_model_id: int, public_only: bool = Query(False), session: AsyncSession = Depends(get_session)
):
    bundle = await exchange_service.build_data_model_bundle(
        session=session, data_model_id=data_model_id, publisher=_publisher(), public_only=public_only
    )
    return JSONResponse(content=bundle)


@router.get("/bundles/transformation-groups/{transformation_group_id}")
async def get_transformation_group_bundle(transformation_group_id: int, session: AsyncSession = Depends(get_session)):
    bundle = await exchange_service.build_transformation_group_bundle(
        session=session, transformation_group_id=transformation_group_id, publisher=_publisher()
    )
    return JSONResponse(content=bundle)


@router.post("/receive", response_model=ExchangeReceiveResultDTO)
async def receive_bundle(
    bundle: Dict[str, Any] = Body(..., description="A bundle fetched from a peer MDR's /exchange/bundles/... endpoint"),
    data_model_type: DataModelType = DataModelType.SourceSchema,
    contributor_organization: Optional[str] = None,
    allowMissingPaths: bool = True,  # noqa: N803 — matches the /transformation_groups/{id}/import query param
    session: AsyncSession = Depends(get_session),
):
    """Receive a data-model or transformation-group bundle published by a peer MDR.

    Data models are reused when a non-deleted (Name, Version, ContributorOrganization) match exists,
    else created as Draft ``data_model_type`` models. ``contributor_organization`` defaults to the
    bundle's ``manifest.publisher``.
    """
    try:
        return await exchange_service.receive_bundle(
            session=session,
            raw_bundle=bundle,
            data_model_type=data_model_type.value,
            contributor_organization=contributor_organization,
            allow_missing_paths=allowMissingPaths,
        )
    except IntegrityError as exc:
        logger.exception("Exchange receive failed due to a database integrity error")
        raise HTTPException(
            status_code=409, detail="The bundle could not be received due to a database integrity error."
        ) from exc
