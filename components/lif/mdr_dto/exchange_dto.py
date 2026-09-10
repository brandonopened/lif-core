"""Wire shapes and pure helpers for the schema-exchange bundle format (``/exchange``).

Schema exchange is pull-based: a publishing MDR exposes bundles under ``GET /exchange/...`` and a
peer MDR fetches them and POSTs them to its own ``/exchange/receive``. No partner URLs or
credentials are stored on either side (ADR 0002). Everything in this module is importable without
a database so the checksum/manifest contract can be unit-tested in isolation.
"""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from lif.mdr_dto.transformation_group_dto import ImportTransformationGroupRequestDTO, TransformationImportSkipDTO
from pydantic import BaseModel

EXCHANGE_FORMAT_VERSION = "1"

BundleKind = Literal["data-model", "transformation-group"]


# --- Bundle (shared contract between MDR instances) -------------------------------------------


class ExchangeManifestDTO(BaseModel):
    kind: BundleKind
    formatVersion: str
    publisher: str
    exportedAt: str
    name: str
    version: str
    checksum: Optional[str] = None


class ExchangeDataModelDTO(BaseModel):
    """A data model as carried in a bundle: identity plus its MDR OpenAPI export."""

    name: str
    version: str
    type: str
    description: Optional[str] = None
    openapi: Dict[str, Any]


class ExchangeBundleDTO(BaseModel):
    """Body of ``POST /exchange/receive`` (either bundle kind).

    ``transformationGroup`` is parsed with the same DTO as ``/transformation_groups/{id}/import`` so
    every database ID in the bundle is ignored and paths resolve portably by ``UniqueName``.
    """

    manifest: ExchangeManifestDTO
    dataModel: Optional[ExchangeDataModelDTO] = None
    sourceDataModel: Optional[ExchangeDataModelDTO] = None
    targetDataModel: Optional[ExchangeDataModelDTO] = None
    transformationGroup: Optional[ImportTransformationGroupRequestDTO] = None


# --- Catalog ----------------------------------------------------------------------------------


class ExchangeCatalogDataModelDTO(BaseModel):
    id: int
    name: str
    version: str
    type: str
    state: str
    description: Optional[str] = None


class ExchangeCatalogDataModelRefDTO(BaseModel):
    id: int
    name: str
    version: str


class ExchangeCatalogTransformationGroupDTO(BaseModel):
    id: int
    name: str
    version: Optional[str] = None
    sourceDataModel: ExchangeCatalogDataModelRefDTO
    targetDataModel: ExchangeCatalogDataModelRefDTO


class ExchangeCatalogDTO(BaseModel):
    publisher: str
    dataModels: List[ExchangeCatalogDataModelDTO]
    transformationGroups: List[ExchangeCatalogTransformationGroupDTO]


# --- Receive result ---------------------------------------------------------------------------


class ExchangeReceivedDataModelDTO(BaseModel):
    name: str
    version: str
    id: int
    status: Literal["created", "exists"]


class ExchangeReceivedTransformationGroupDTO(BaseModel):
    id: int
    version: str
    importedTransformationCount: int
    skippedTransformationCount: int
    skippedTransformations: List[TransformationImportSkipDTO] = []


class ExchangeReceiveResultDTO(BaseModel):
    dataModels: List[ExchangeReceivedDataModelDTO]
    transformationGroup: Optional[ExchangeReceivedTransformationGroupDTO] = None


# --- Pure helpers: checksum + manifest ------------------------------------------------------------


class ExchangeBundleError(ValueError):
    """A bundle violates the exchange contract (bad checksum, unknown format version, ...)."""


def canonical_json(payload: Any) -> bytes:
    """Deterministic JSON encoding shared by every MDR that computes bundle checksums."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def compute_checksum(bundle: Dict[str, Any]) -> str:
    """``sha256:<hex>`` over the canonical JSON of the bundle WITHOUT its ``manifest`` key."""
    payload = {key: value for key, value in bundle.items() if key != "manifest"}
    return "sha256:" + hashlib.sha256(canonical_json(payload)).hexdigest()


def build_manifest(kind: BundleKind, publisher: str, name: str, version: str, bundle: Dict[str, Any]) -> Dict[str, Any]:
    """Manifest for ``bundle`` (whose non-manifest keys are already final)."""
    return {
        "kind": kind,
        "formatVersion": EXCHANGE_FORMAT_VERSION,
        "publisher": publisher,
        "exportedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "name": name,
        "version": version,
        "checksum": compute_checksum(bundle),
    }


def verify_bundle(bundle: Dict[str, Any]) -> None:
    """Reject an unknown ``formatVersion`` and, when a checksum is present, a checksum mismatch."""
    manifest = bundle.get("manifest")
    if not isinstance(manifest, dict):
        raise ExchangeBundleError("Bundle is missing its manifest")
    format_version = manifest.get("formatVersion")
    if format_version != EXCHANGE_FORMAT_VERSION:
        raise ExchangeBundleError(
            f"Unsupported bundle formatVersion '{format_version}' (this MDR supports '{EXCHANGE_FORMAT_VERSION}')"
        )
    expected = manifest.get("checksum")
    if expected is not None and expected != compute_checksum(bundle):
        raise ExchangeBundleError("Bundle checksum does not match its content")
