"""FastAPI layer for importing EDUcore education-standard data models into the
LIF MDR.

This module exposes three endpoints under the ``/educore`` prefix:

* ``GET  /educore/standards``               — list the standards EDUcore knows.
* ``POST /educore/standards/{key}/import``   — import one standard as an MDR
  ``SourceSchema`` DataModel (idempotent on the DataModel name).
* ``POST /educore/cypher``                   — read-only Cypher passthrough.

It talks to the EDUcore knowledge graph over MCP-streamable-HTTP using only
``requests`` (already a dependency of ``lif_mdr_api``) so no new packages are
pulled in. The verified Cypher lives in :mod:`educore_queries` (do not edit it).
The EDUcore rows are converted to the NON-STANDARD ``openapi_schema`` dict shape
that ``schema_upload_service.create_data_model_from_openapi_schema`` ingests
(see ``EDUCORE_INTEGRATION_SPEC.md``), then handed to that service directly.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Optional

import requests
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lif.datatypes.mdr_sql_model import (
    Attribute,
    DataModel,
    Entity,
    EntityAttributeAssociation,
    ExpressionLanguageType,
    TransformationGroup,
)
from lif.mdr_dto.transformation_dto import (
    CreateTransformationAttributeDTO,
    CreateTransformationGroupDTO,
    CreateTransformationWithTransformationGroupDTO,
)
from lif.mdr_restapi import educore_queries
from lif.mdr_services import schema_upload_service, transformation_service
from lif.mdr_utils.database_setup import get_session
from lif.mdr_utils.logger_config import get_logger

router = APIRouter()
logger = get_logger(__name__)


# ===========================================================================
# (a) EDUcore MCP-over-HTTP client (streamable HTTP, JSON-RPC, SSE responses)
# ===========================================================================

EDUCORE_MCP_URL = os.environ.get("EDUCORE_MCP_URL", "https://educore.dev/mcp")
_MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
_REQUEST_TIMEOUT = 30  # seconds
_SESSION_TTL = 300  # seconds — re-handshake after this long


def _parse_sse_json(text: str) -> dict:
    """Extract the JSON object from an SSE (``text/event-stream``) payload.

    The MCP server replies with one or more ``data:`` lines; the JSON-RPC body
    is the remainder of a ``data:`` line. We iterate lines, find the first
    ``data:`` whose remainder parses as JSON, and return it. Falls back to
    parsing the whole body as plain JSON (some servers reply without SSE).
    """
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            continue
    # Fallback: maybe the whole body is plain JSON.
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise ValueError(f"Could not parse MCP response as SSE/JSON: {text[:200]!r}") from exc


def _extract_rows(rpc_result: dict) -> list[dict]:
    """Pull the list of row dicts out of an MCP ``tools/call`` result.

    The ``cypherQuery`` tool returns rows either as ``structuredContent`` or as
    a JSON string inside ``content[0].text``. Handle both defensively.
    """
    result = rpc_result.get("result", rpc_result)

    # 1. structuredContent (typed) — may wrap the array under a key.
    structured = result.get("structuredContent")
    if structured is not None:
        if isinstance(structured, list):
            return [r for r in structured if isinstance(r, dict)]
        if isinstance(structured, dict):
            for value in structured.values():
                if isinstance(value, list):
                    return [r for r in value if isinstance(r, dict)]

    # 2. content[].text holding a JSON string.
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if not isinstance(text, str):
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, list):
                return [r for r in parsed if isinstance(r, dict)]
            if isinstance(parsed, dict):
                for value in parsed.values():
                    if isinstance(value, list):
                        return [r for r in value if isinstance(r, dict)]

    return []


class EducoreClient:
    """Tiny MCP-streamable-HTTP client for EDUcore's ``cypherQuery`` tool.

    Caches the ``mcp-session-id`` for a short TTL and re-initializes on failure.
    Thread-safe via a lock around the handshake/session state.
    """

    def __init__(self, url: str = EDUCORE_MCP_URL, session_ttl: int = _SESSION_TTL):
        self._url = url
        self._session_ttl = session_ttl
        self._lock = threading.Lock()
        self._session_id: Optional[str] = None
        self._session_acquired_at: float = 0.0
        self._rpc_id = 0

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    def _initialize(self) -> str:
        """Perform the MCP handshake and return a fresh ``mcp-session-id``."""
        init_body = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "lif-mdr", "version": "0.1"},
            },
        }
        resp = requests.post(self._url, headers=_MCP_HEADERS, json=init_body, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        session_id = resp.headers.get("mcp-session-id")
        if not session_id:
            raise ValueError("EDUcore initialize did not return an mcp-session-id header")
        # The SSE body is UTF-8; pin the encoding so requests doesn't fall back
        # to latin-1 (which mangles em-dashes and other multi-byte chars).
        resp.encoding = "utf-8"
        # Confirm the body parses (and surface any JSON-RPC error early).
        rpc = _parse_sse_json(resp.text)
        if "error" in rpc:
            raise ValueError(f"EDUcore initialize error: {rpc['error']}")

        # Send the required 'initialized' notification (no id => no response body).
        notify_headers = {**_MCP_HEADERS, "mcp-session-id": session_id}
        notify_body = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        notify_resp = requests.post(self._url, headers=notify_headers, json=notify_body, timeout=_REQUEST_TIMEOUT)
        notify_resp.raise_for_status()
        return session_id

    def _ensure_session(self, force: bool = False) -> str:
        now = time.monotonic()
        if not force and self._session_id is not None and (now - self._session_acquired_at) < self._session_ttl:
            return self._session_id
        self._session_id = self._initialize()
        self._session_acquired_at = time.monotonic()
        return self._session_id

    def _call_cypher(self, session_id: str, query: str, params: dict) -> list[dict]:
        headers = {**_MCP_HEADERS, "mcp-session-id": session_id}
        body = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": "cypherQuery", "arguments": {"query": query, "params": params}},
        }
        resp = requests.post(self._url, headers=headers, json=body, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        # The SSE body is UTF-8; pin the encoding so requests doesn't fall back
        # to latin-1 (which turns an em-dash into mojibake like "â€"").
        resp.encoding = "utf-8"
        rpc = _parse_sse_json(resp.text)
        if "error" in rpc:
            raise ValueError(f"EDUcore cypherQuery error: {rpc['error']}")
        return _extract_rows(rpc)

    def run_cypher(self, query: str, params: Optional[dict] = None) -> list[dict]:
        """Execute a read-only Cypher query and return the list of row dicts.

        Reuses a cached session id; on any failure it re-initializes once and
        retries (handles an expired/invalidated session id transparently).
        """
        params = params or {}
        with self._lock:
            try:
                session_id = self._ensure_session()
                return self._call_cypher(session_id, query, params)
            except (requests.RequestException, ValueError) as first_exc:
                logger.warning("EDUcore call failed (%s); re-initializing session", first_exc)
                try:
                    session_id = self._ensure_session(force=True)
                    return self._call_cypher(session_id, query, params)
                except (requests.RequestException, ValueError) as exc:
                    raise RuntimeError(f"EDUcore request failed: {exc}") from exc


# Module-level singleton client.
_client = EducoreClient()


def run_cypher(query: str, params: Optional[dict] = None) -> list[dict]:
    """Module-level convenience wrapper around the singleton :class:`EducoreClient`."""
    return _client.run_cypher(query, params)


# EDUcore's MCP cypherQuery caps a single call at ~100–101 rows regardless of any
# baked-in LIMIT, so large standards must be fetched page-by-page.
_DEFAULT_PAGE_SIZE = 100
_MAX_PAGES = 200  # safety cap: 200 * page_size (~20k rows) before bailing out


def run_cypher_paged(query: str, params: Optional[dict] = None, page_size: int = _DEFAULT_PAGE_SIZE) -> list[dict]:
    """Run a ``$skip``/``$limit`` paginatable Cypher query across all pages.

    The query must end with ``ORDER BY ... SKIP $skip LIMIT $limit`` (see
    :mod:`educore_queries`). This loops: set ``skip`` to 0, ``page_size``,
    ``2*page_size`` … with ``limit = page_size``, calling :func:`run_cypher` each
    time and accumulating rows. It stops when a page returns fewer than
    ``page_size`` rows (the last page). A ``_MAX_PAGES`` cap guards against an
    infinite loop if the server ever ignores ``SKIP``; hitting it logs a warning.

    The incoming ``params`` are passed through unchanged except for ``skip`` and
    ``limit``, which this function controls.
    """
    base_params = dict(params or {})
    base_params.pop("skip", None)
    base_params.pop("limit", None)

    all_rows: list[dict] = []
    for page in range(_MAX_PAGES):
        page_params = {**base_params, "skip": page * page_size, "limit": page_size}
        rows = run_cypher(query, page_params)
        all_rows.extend(rows)
        if len(rows) < page_size:
            break
    else:
        logger.warning(
            "run_cypher_paged hit the max-pages cap (%d pages, page_size=%d); "
            "results may be truncated for query starting %r",
            _MAX_PAGES,
            page_size,
            query[:80],
        )
    return all_rows


# ===========================================================================
# (b) Conversion: EDUcore rows -> OpenAPI-style schema dict for MDR ingestion
# ===========================================================================

# Per spec §4: LIF "object" ref props normalize to "string"; "string"/"boolean"
# stay as-is; everything else preserves its native type string for the UI.
_TYPE_MAP = {"string": "string", "boolean": "boolean", "object": "string"}


def _split_words(name: str) -> list[str]:
    """Split an arbitrary identifier into word tokens.

    Handles spaces/punctuation (CEDS/CTDL names like ``"Academic Certificate"``,
    ``"Accredited By"``) and embedded camelCase/PascalCase boundaries.
    """
    if not name:
        return []
    # Replace any non-alphanumeric separator with a space, then split camelCase.
    spaced = re.sub(r"[^0-9A-Za-z]+", " ", name)
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", spaced)
    return [w for w in spaced.split() if w]


def to_pascal(name: str) -> str:
    """Normalize an entity/object name to PascalCase (LIF convention)."""
    words = _split_words(name)
    if not words:
        return name
    return "".join(w[:1].upper() + w[1:] for w in words)


def to_camel(name: str) -> str:
    """Normalize a scalar attribute name to camelCase (LIF convention)."""
    pascal = to_pascal(name)
    if not pascal:
        return name
    return pascal[:1].lower() + pascal[1:]


def map_datatype(data_type: str, is_ref: bool) -> tuple[str, bool]:
    """Map an EDUcore native ``dataType`` to ``(MDR DataType, is_array)``.

    Per the spec: ``array`` becomes ``Array=Yes`` with element type unknown
    (default ``string``); ``object``/``string``/``boolean`` map via ``_TYPE_MAP``;
    every other native type string is preserved verbatim so the mapping UI stays
    informative. ``is_ref`` is accepted for future child-entity modeling but, in
    this flat v1, does not change the output.
    """
    dt = (data_type or "").strip()
    if dt == "array":
        return "string", True
    if not dt:
        return "string", False
    return _TYPE_MAP.get(dt, dt), False


def standard_to_openapi_schema(title: str, entities_rows: list[dict], properties_rows: list[dict]) -> dict:
    """Convert EDUcore entity/property rows into the MDR ``openapi_schema`` dict.

    One ``components.schemas`` entry per entity (PascalCase). Each property is a
    flat Attribute (camelCase) carrying the ``ValueSetId: null`` marker that the
    MDR parser keys on, plus ``Required``/``Array`` as the strings ``"Yes"``/
    ``"No"`` and ``DataType`` from :func:`map_datatype`.
    """
    schemas: dict[str, dict] = {}

    for row in entities_rows:
        entity_name = row.get("entityName")
        if not entity_name:
            continue
        pascal = to_pascal(entity_name)
        if not pascal or pascal in schemas:
            continue
        description = row.get("entityDescription") or None
        schemas[pascal] = {
            "Name": pascal,
            "UniqueName": pascal,
            "Description": description,
            "Required": "No",
            "Array": "Yes",  # entities are arrays in the LIF convention
            "properties": {},
        }

    for row in properties_rows:
        entity_name = row.get("entityName")
        prop_name = row.get("propName")
        if not entity_name or not prop_name:
            continue
        entity = schemas.get(to_pascal(entity_name))
        if entity is None:
            # Property whose owning entity was not surfaced — skip defensively.
            continue
        camel = to_camel(prop_name)
        if not camel or camel in entity["properties"]:
            continue
        mdr_dt, is_array = map_datatype(row.get("dataType", ""), bool(row.get("isRef")))
        entity["properties"][camel] = {
            "Name": camel,
            "UniqueName": f"{entity['UniqueName']}.{camel}",
            "DataType": mdr_dt,
            "Required": "Yes" if row.get("required") else "No",
            "Array": "Yes" if is_array else "No",
            "ValueSetId": None,  # marks this as a leaf Attribute for the MDR parser
        }

    return {
        "openapi": "3.0.0",
        "info": {"title": title, "version": "1.0", "description": "Imported from EDUcore"},
        "paths": {},
        "components": {"schemas": schemas},
    }


# ===========================================================================
# (c) Endpoints
# ===========================================================================


# Short, recognizable labels per standard root key. Used for the imported
# DataModel name ("(EDUcore) CASE") and the drawer's primary label, so the long
# native titles (e.g. "Competencies and Academic Standards Exchange (CASE)
# Service OpenAPI (YAML) Definition") don't dominate the UI.
_SHORT_NAMES: dict[str, str] = {
    "LifRoot": "LIF",
    "PescRoot": "PESC",
    "CaseRoot": "CASE",
    "CedsOntology": "CEDS",
    "EdfiRoot": "Ed-Fi",
    "CtdlRoot": "CTDL",
    "SifRoot": "SIF",
    "CipRoot": "CIP",
    "DctapRoot": "DCTAP",
    "JedxRoot": "JEDx",
    "ClrRoot": "CLR",
    "EduApiRoot": "Edu-API",
    "OpenBadgesRoot": "Open Badges",
    "SedmRoot": "SEDM",
    "SocRoot": "SOC",
    "MedBiqModel": "MedBiquitous",
}

# Reverse the short-name map and join it to the graph framework label, so an
# imported DataModel named "(EDUcore) {short}" can be resolved back to the
# MAPS_TO framework label (e.g. "SIF" -> "SifModel", "CEDS" -> "CEDS").
_GRAPH_LABEL_BY_SHORT: dict[str, str] = {
    short: educore_queries.GRAPH_FRAMEWORK_LABEL_BY_KEY[key]
    for key, short in _SHORT_NAMES.items()
    if key in educore_queries.GRAPH_FRAMEWORK_LABEL_BY_KEY
}

_EDUCORE_NAME_PREFIX = "(EDUcore) "


def _short_label(key: str, title: Optional[str]) -> str:
    """A short standard label (e.g. "CASE") for display and the model name.

    Prefers the curated map; otherwise extracts a parenthetical acronym from the
    title (e.g. "...(CASE)..." -> "CASE"); otherwise strips the Root/Ontology
    suffix off the key; otherwise falls back to the title or key.
    """
    if key in _SHORT_NAMES:
        return _SHORT_NAMES[key]
    if title:
        match = re.search(r"\(([A-Za-z0-9*\-]{2,12})\)", title)
        if match:
            return match.group(1)
    stripped = re.sub(r"(Root|Ontology|Model)$", "", key)
    return stripped or title or key


def _educore_model_name(key: str, title: Optional[str]) -> str:
    """The MDR DataModel name for an imported standard, e.g. "(EDUcore) CASE"."""
    return f"(EDUcore) {_short_label(key, title)}"


class StandardSummary(BaseModel):
    key: str
    title: Optional[str] = None
    displayName: Optional[str] = None
    importable: bool = False
    version: Optional[str] = None
    description: Optional[str] = None


class CypherRequest(BaseModel):
    query: str
    params: Optional[dict[str, Any]] = None


class ImportResponse(BaseModel):
    Id: Optional[int]
    Name: str
    alreadyExisted: bool


class MappingPair(BaseModel):
    sourceShort: str
    targetShort: str
    sourceDataModelId: int
    targetDataModelId: int
    mappingCount: int  # field-level MAPS_TO edges in the graph for this directed pair


class LoadMappingsRequest(BaseModel):
    sourceDataModelId: int
    targetDataModelId: int


class LoadMappingsResponse(BaseModel):
    transformationGroupId: int
    groupName: str
    transformationsCreated: int  # one per distinct target attribute (sources merged)
    sourcePairsMapped: int  # total source->target attribute wires created
    edgesFound: int  # field-level MAPS_TO edges in the EDUcore graph
    edgesUnresolved: int  # edges whose entity/field didn't resolve in the imported models
    alreadyExisted: bool
    reverseDirectionHint: bool  # true when 0 edges this direction but the reverse has data


async def _find_existing_datamodel(session: AsyncSession, name: str) -> Optional[DataModel]:
    """Return a non-deleted DataModel with the given Name, or None."""
    stmt = select(DataModel).where(DataModel.Name == name, DataModel.Deleted == False)
    result = await session.execute(stmt)
    return result.scalars().first()


def _educore_short(name: Optional[str]) -> Optional[str]:
    """Extract the short standard label from an "(EDUcore) {short}" DataModel name."""
    if name and name.startswith(_EDUCORE_NAME_PREFIX):
        return name[len(_EDUCORE_NAME_PREFIX) :].strip() or None
    return None


async def _build_attr_resolution(session: AsyncSession, data_model_id: int) -> dict[tuple[str, str], tuple[int, int]]:
    """Map ``(Entity.Name, Attribute.Name)`` -> ``(EntityId, AttributeId)`` for a model.

    Built from the EntityAttributeAssociation rows the EDUcore importer created.
    Entity names are PascalCase and attribute names camelCase (the importer's
    normalization), so the MAPS_TO graph names are matched after the same
    ``to_pascal``/``to_camel`` transform.
    """
    stmt = (
        select(Entity.Name, Entity.Id, Attribute.Name, Attribute.Id)
        .join(EntityAttributeAssociation, EntityAttributeAssociation.EntityId == Entity.Id)
        .join(Attribute, Attribute.Id == EntityAttributeAssociation.AttributeId)
        .where(
            Entity.DataModelId == data_model_id,
            Entity.Deleted == False,
            Attribute.Deleted == False,
            EntityAttributeAssociation.Deleted == False,
        )
    )
    result = await session.execute(stmt)
    resolution: dict[tuple[str, str], tuple[int, int]] = {}
    for entity_name, entity_id, attr_name, attr_id in result.all():
        resolution[(entity_name, attr_name)] = (entity_id, attr_id)
    return resolution


def _resolve_attr(
    resolution: dict[tuple[str, str], tuple[int, int]], entity_names: list[str], field_name: Optional[str]
) -> Optional[tuple[int, int]]:
    """Resolve a graph (owning-entity, field) to imported ``(EntityId, AttributeId)``.

    A field can have several owning entities in the graph; try each until one
    resolves. Names are normalized with the importer's ``to_pascal``/``to_camel``.
    """
    if not field_name:
        return None
    camel = to_camel(field_name)
    for entity_name in entity_names or []:
        if not entity_name:
            continue
        hit = resolution.get((to_pascal(entity_name), camel))
        if hit is not None:
            return hit
    return None


@router.get("/standards", response_model=list[StandardSummary])
async def list_standards() -> list[StandardSummary]:
    """List every standard model-root known to EDUcore (frontend filters)."""
    try:
        rows = run_cypher(educore_queries.LIST_STANDARDS_CYPHER)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    supported = set(educore_queries.SUPPORTED_KEYS)
    summaries: list[StandardSummary] = []
    for row in rows:
        key = row.get("key")
        if not key:
            continue
        key = str(key)
        title = row.get("title")
        summaries.append(
            StandardSummary(
                key=key,
                title=title,
                displayName=_short_label(key, title),
                importable=key in supported,
                version=row.get("version"),
                description=row.get("description"),
            )
        )
    return summaries


@router.post("/standards/{key}/import", response_model=ImportResponse)
async def import_standard(key: str, session: AsyncSession = Depends(get_session)) -> ImportResponse:
    """Import one EDUcore standard into the MDR as a SourceSchema DataModel.

    Idempotent: if a DataModel named ``"(EDUcore) {short}"`` already exists, it is
    returned without recreating.
    """
    try:
        loader = educore_queries.get_load_cypher(key)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unsupported EDUcore standard key: {key!r}. Supported: {educore_queries.SUPPORTED_KEYS}",
        ) from exc

    params = loader["params"]
    try:
        # Paginate: EDUcore's MCP caps each call at ~100 rows, so a single-shot
        # fetch silently truncates large standards (LIF has ~842 property rows).
        entities_rows = run_cypher_paged(loader["entities"], params)
        properties_rows = run_cypher_paged(loader["properties"], params)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    # Resolve a human title from the standards list (fall back to the key).
    title = key
    try:
        for row in run_cypher(educore_queries.LIST_STANDARDS_CYPHER):
            if row.get("key") == key:
                title = row.get("title") or key
                break
    except RuntimeError:
        logger.warning("Could not fetch standards list for title resolution; using key %s", key)

    data_model_name = _educore_model_name(key, title)

    existing = await _find_existing_datamodel(session, data_model_name)
    if existing is not None:
        return ImportResponse(Id=existing.Id, Name=existing.Name, alreadyExisted=True)

    openapi_schema = standard_to_openapi_schema(title, entities_rows, properties_rows)

    try:
        dto = await schema_upload_service.create_data_model_from_openapi_schema(
            session=session,
            openapi_schema=openapi_schema,
            data_model_name=data_model_name,
            data_model_version="1.0",
            data_model_type="SourceSchema",
            data_model_description=f"Imported from EDUcore knowledge graph ({key})",
            base_data_model_id=None,
            use_considerations=None,
            notes=None,
            activation_date=None,
            deprecation_date=None,
            contributor="EDUcore Importer",
            contributor_organization="EDUcore",
            state="Draft",
            tags="educore,imported",
        )
    except ValueError as exc:
        # Uniqueness guard raced with a concurrent import — treat as existing.
        existing = await _find_existing_datamodel(session, data_model_name)
        if existing is not None:
            return ImportResponse(Id=existing.Id, Name=existing.Name, alreadyExisted=True)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return ImportResponse(Id=dto.Id, Name=dto.Name, alreadyExisted=False)


@router.get("/mappings/available", response_model=list[MappingPair])
async def list_available_mapping_pairs(session: AsyncSession = Depends(get_session)) -> list[MappingPair]:
    """List directed framework pairs with a canonical crosswalk where BOTH specs are imported.

    These are the pairs the UI can load right now: each has MAPS_TO field edges
    in the graph and both standards exist as "(EDUcore) {short}" DataModels.
    """
    try:
        rows = run_cypher(educore_queries.MAPS_TO_PAIRS_CYPHER)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    # Imported EDUcore DataModels, keyed by short label.
    dm_stmt = select(DataModel).where(DataModel.Name.like(f"{_EDUCORE_NAME_PREFIX}%"), DataModel.Deleted == False)
    dm_by_short: dict[str, int] = {}
    for dm in (await session.execute(dm_stmt)).scalars().all():
        short = _educore_short(dm.Name)
        if short:
            dm_by_short[short] = dm.Id

    key_by_label = {label: key for key, label in educore_queries.GRAPH_FRAMEWORK_LABEL_BY_KEY.items()}

    pairs: list[MappingPair] = []
    for row in rows:
        src_key = key_by_label.get(row.get("sourceLabel"))
        tgt_key = key_by_label.get(row.get("targetLabel"))
        if not src_key or not tgt_key:
            continue
        src_short = _SHORT_NAMES.get(src_key)
        tgt_short = _SHORT_NAMES.get(tgt_key)
        if not src_short or not tgt_short:
            continue
        src_dm = dm_by_short.get(src_short)
        tgt_dm = dm_by_short.get(tgt_short)
        if src_dm is None or tgt_dm is None:
            # Only surface pairs that can be loaded now (both specs imported).
            continue
        pairs.append(
            MappingPair(
                sourceShort=src_short,
                targetShort=tgt_short,
                sourceDataModelId=src_dm,
                targetDataModelId=tgt_dm,
                mappingCount=int(row.get("mappingCount") or 0),
            )
        )
    return pairs


@router.post("/mappings/load", response_model=LoadMappingsResponse)
async def load_educore_mappings(
    body: LoadMappingsRequest, session: AsyncSession = Depends(get_session)
) -> LoadMappingsResponse:
    """Materialize the canonical EDUcore MAPS_TO crosswalk between two imported specs.

    Both DataModels must be EDUcore imports ("(EDUcore) {short}"). Fetches the
    authoritative field-level MAPS_TO edges (source->target direction) from the
    graph, resolves each end to the imported attribute, and creates a
    transformation group + one transformation per distinct target attribute
    (multiple sources merged onto a target). Idempotent on the group name; the
    returned ``transformationGroupId`` is what the UI navigates to.
    """
    src_dm = await session.get(DataModel, body.sourceDataModelId)
    tgt_dm = await session.get(DataModel, body.targetDataModelId)
    if src_dm is None or src_dm.Deleted:
        raise HTTPException(status_code=404, detail=f"Source DataModel {body.sourceDataModelId} not found")
    if tgt_dm is None or tgt_dm.Deleted:
        raise HTTPException(status_code=404, detail=f"Target DataModel {body.targetDataModelId} not found")

    src_short = _educore_short(src_dm.Name)
    tgt_short = _educore_short(tgt_dm.Name)
    if not src_short or not tgt_short:
        raise HTTPException(
            status_code=400, detail="Both source and target must be EDUcore-imported DataModels (named '(EDUcore) …')."
        )
    src_label = _GRAPH_LABEL_BY_SHORT.get(src_short)
    tgt_label = _GRAPH_LABEL_BY_SHORT.get(tgt_short)
    if not src_label or not tgt_label:
        raise HTTPException(status_code=400, detail=f"Unknown EDUcore framework: {src_short!r} -> {tgt_short!r}")

    group_name = f"(EDUcore) {src_short} → {tgt_short}"

    # Idempotency: reuse an existing non-deleted group with this exact identity.
    existing_stmt = select(TransformationGroup).where(
        TransformationGroup.SourceDataModelId == src_dm.Id,
        TransformationGroup.TargetDataModelId == tgt_dm.Id,
        TransformationGroup.Name == group_name,
        TransformationGroup.Deleted == False,
    )
    existing_group = (await session.execute(existing_stmt)).scalars().first()

    # Fetch the authoritative field-level crosswalk (source -> target).
    try:
        query = educore_queries.maps_to_field_cypher(src_label, tgt_label)
        edges = run_cypher_paged(query, {"skip": 0, "limit": 100})
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    # If nothing this direction, see if the crosswalk was authored the other way.
    reverse_hint = False
    if not edges:
        try:
            reverse_hint = bool(
                run_cypher(educore_queries.maps_to_field_cypher(tgt_label, src_label), {"skip": 0, "limit": 1})
            )
        except RuntimeError:
            reverse_hint = False

    if existing_group is not None:
        return LoadMappingsResponse(
            transformationGroupId=existing_group.Id,
            groupName=group_name,
            transformationsCreated=0,
            sourcePairsMapped=0,
            edgesFound=len(edges),
            edgesUnresolved=0,
            alreadyExisted=True,
            reverseDirectionHint=reverse_hint,
        )

    # Resolve graph names -> imported attribute IDs, grouping sources per target.
    src_res = await _build_attr_resolution(session, src_dm.Id)
    tgt_res = await _build_attr_resolution(session, tgt_dm.Id)

    # (tgtEntityId, tgtAttrId) -> {"name": targetCamel, "sources": {(srcEntId, srcAttrId): srcCamel}}
    by_target: dict[tuple[int, int], dict[str, Any]] = {}
    unresolved = 0
    for edge in edges:
        s_hit = _resolve_attr(src_res, edge.get("sourceEntities") or [], edge.get("sourceField"))
        t_hit = _resolve_attr(tgt_res, edge.get("targetEntities") or [], edge.get("targetField"))
        if s_hit is None or t_hit is None:
            unresolved += 1
            continue
        slot = by_target.setdefault(t_hit, {"name": to_camel(edge.get("targetField") or ""), "sources": {}})
        slot["sources"][s_hit] = to_camel(edge.get("sourceField") or "")

    if not by_target:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Found {len(edges)} MAPS_TO field edges {src_short}->{tgt_short} but none resolved to "
                f"attributes in the imported models. "
                + (
                    "The crosswalk exists in the reverse direction — try swapping source/target."
                    if reverse_hint
                    else ""
                )
            ),
        )

    # Create the group (bump the major version if one already exists at that version).
    group = None
    last_exc: Optional[HTTPException] = None
    for major in range(1, 1000):
        try:
            group = await transformation_service.create_transformation_group(
                session,
                CreateTransformationGroupDTO(
                    SourceDataModelId=src_dm.Id,
                    TargetDataModelId=tgt_dm.Id,
                    Name=group_name,
                    GroupVersion=f"{major}.0",
                    Description=f"Loaded from EDUcore MAPS_TO crosswalk ({src_short} -> {tgt_short})",
                    Contributor="EDUcore Importer",
                    ContributorOrganization="EDUcore",
                ),
            )
            break
        except HTTPException as he:
            last_exc = he
            if he.status_code == 400 and "already exists" in str(he.detail):
                continue
            raise
    if group is None:
        raise last_exc or HTTPException(status_code=500, detail="Could not create transformation group")

    # Build one transformation per target attribute, merging all mapped sources.
    transforms: list[CreateTransformationWithTransformationGroupDTO] = []
    pairs = 0
    for (tgt_entity_id, tgt_attr_id), slot in by_target.items():
        source_attrs = [
            CreateTransformationAttributeDTO(
                AttributeId=s_attr_id,
                EntityId=s_entity_id,
                AttributeType="Source",
                EntityIdPath=f"{s_entity_id},-{s_attr_id}",
            )
            for (s_entity_id, s_attr_id) in slot["sources"]
        ]
        pairs += len(source_attrs)
        target_attr = CreateTransformationAttributeDTO(
            AttributeId=tgt_attr_id,
            EntityId=tgt_entity_id,
            AttributeType="Target",
            EntityIdPath=f"{tgt_entity_id},-{tgt_attr_id}",
        )
        source_names = sorted({name for name in slot["sources"].values() if name})
        expression = " , ".join(source_names) if source_names else slot["name"]
        transforms.append(
            CreateTransformationWithTransformationGroupDTO(
                Name=slot["name"] or None,
                Expression=expression or slot["name"] or "",
                ExpressionLanguage=ExpressionLanguageType.JSONata,
                SourceAttributes=source_attrs,
                TargetAttribute=target_attr,
            )
        )

    await transformation_service.create_multiple_transformations_for_a_group(session, group.Id, transforms)

    return LoadMappingsResponse(
        transformationGroupId=group.Id,
        groupName=group_name,
        transformationsCreated=len(transforms),
        sourcePairsMapped=pairs,
        edgesFound=len(edges),
        edgesUnresolved=unresolved,
        alreadyExisted=False,
        reverseDirectionHint=reverse_hint,
    )


@router.post("/cypher")
async def run_cypher_passthrough(body: CypherRequest) -> dict[str, list[dict]]:
    """Read-only Cypher passthrough convenience endpoint."""
    try:
        rows = run_cypher(body.query, body.params)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return {"rows": rows}
