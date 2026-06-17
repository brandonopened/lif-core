"""Unit tests for the EDUcore import endpoints.

These tests never hit the network: they exercise the pure conversion helper and
the SSE/JSON-RPC parsing against captured sample payloads (with ``requests``
mocked). They focus on the non-obvious logic — the MDR-specific
``ValueSetId``-marker shape, name normalization, ``Yes``/``No`` string casing,
array handling, and defensive SSE/row extraction.
"""

from __future__ import annotations

import json
import os

import pytest

# Importing the endpoint module pulls in `database_setup`, which builds a
# SQLAlchemy async engine from POSTGRESQL_* env vars AT IMPORT TIME. These tests
# are pure (no DB connection is opened), so provide harmless placeholder values
# so URL parsing succeeds during collection. (Mirrors why conftest defers the
# MDR `core` import into fixtures.)
os.environ.setdefault("POSTGRESQL_USER", "postgres")
os.environ.setdefault("POSTGRESQL_PASSWORD", "postgres")
os.environ.setdefault("POSTGRESQL_HOST", "localhost")
os.environ.setdefault("POSTGRESQL_PORT", "5432")
os.environ.setdefault("POSTGRESQL_DB", "postgres")

from lif.mdr_restapi import educore_endpoints as ee  # noqa: E402


# ---------------------------------------------------------------------------
# standard_to_openapi_schema
# ---------------------------------------------------------------------------


def test_standard_to_openapi_schema_lif_like_rows():
    entities = [
        {"entityName": "Name", "entityDescription": "A person's name."},
        {"entityName": "Course", "entityDescription": ""},
    ]
    properties = [
        {"entityName": "Name", "propName": "firstName", "dataType": "string", "required": True, "isRef": False},
        {"entityName": "Name", "propName": "lastName", "dataType": "string", "required": False, "isRef": False},
        {
            "entityName": "Course",
            "propName": "accreditedByRefOrganization",
            "dataType": "object",
            "required": False,
            "isRef": True,
        },
    ]

    schema = ee.standard_to_openapi_schema("LIF", entities, properties)
    schemas = schema["components"]["schemas"]

    # PascalCase entity keys, with the LIF-style entity defaults.
    assert set(schemas) == {"Name", "Course"}
    assert schemas["Name"]["Array"] == "Yes"
    assert schemas["Name"]["Required"] == "No"
    assert schemas["Course"]["Description"] is None  # empty string -> None

    name_props = schemas["Name"]["properties"]
    # camelCase attribute keys, each carrying the ValueSetId marker.
    assert set(name_props) == {"firstName", "lastName"}
    assert name_props["firstName"]["ValueSetId"] is None
    assert name_props["firstName"]["Required"] == "Yes"  # required bool -> "Yes"
    assert name_props["lastName"]["Required"] == "No"
    assert name_props["firstName"]["DataType"] == "string"
    assert name_props["firstName"]["Array"] == "No"
    # UniqueName is qualified by the entity to stay collision-proof.
    assert name_props["firstName"]["UniqueName"] == "Name.firstName"

    # Ref/object property is still emitted as a flat attribute in v1.
    course_props = schemas["Course"]["properties"]
    assert "accreditedByRefOrganization" in course_props
    assert course_props["accreditedByRefOrganization"]["ValueSetId"] is None
    assert course_props["accreditedByRefOrganization"]["DataType"] == "string"  # object -> string


def test_standard_to_openapi_schema_normalizes_spaced_names():
    """CEDS/CTDL names contain spaces; they must become PascalCase/camelCase."""
    entities = [{"entityName": "Academic Certificate", "entityDescription": "x"}]
    properties = [
        {
            "entityName": "Academic Certificate",
            "propName": "Accredited By",
            "dataType": "ceterms:Organization",
            "required": False,
            "isRef": True,
        }
    ]
    schemas = ee.standard_to_openapi_schema("CTDL", entities, properties)["components"]["schemas"]
    assert "AcademicCertificate" in schemas
    props = schemas["AcademicCertificate"]["properties"]
    assert "accreditedBy" in props
    # Native ref type string preserved verbatim for the mapping UI.
    assert props["accreditedBy"]["DataType"] == "ceterms:Organization"
    assert props["accreditedBy"]["UniqueName"] == "AcademicCertificate.accreditedBy"


def test_standard_to_openapi_schema_array_datatype():
    entities = [{"entityName": "Thing", "entityDescription": ""}]
    properties = [{"entityName": "Thing", "propName": "tags", "dataType": "array", "required": False, "isRef": False}]
    props = ee.standard_to_openapi_schema("X", entities, properties)["components"]["schemas"]["Thing"]["properties"]
    assert props["tags"]["Array"] == "Yes"
    assert props["tags"]["DataType"] == "string"


def test_standard_to_openapi_schema_skips_orphan_properties():
    """A property whose entity was not surfaced is dropped, not crashed on."""
    entities = [{"entityName": "Known", "entityDescription": ""}]
    properties = [{"entityName": "Unknown", "propName": "x", "dataType": "string", "required": False, "isRef": False}]
    schemas = ee.standard_to_openapi_schema("X", entities, properties)["components"]["schemas"]
    assert schemas["Known"]["properties"] == {}


# ---------------------------------------------------------------------------
# map_datatype
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dt", "is_ref", "expected"),
    [
        ("array", False, ("string", True)),
        ("string", False, ("string", False)),
        ("boolean", False, ("boolean", False)),
        ("nonNegativeInteger", False, ("nonNegativeInteger", False)),  # native preserved
        ("ceterms:Organization", True, ("ceterms:Organization", False)),
        ("", False, ("string", False)),  # empty -> default string
    ],
)
def test_map_datatype(dt, is_ref, expected):
    assert ee.map_datatype(dt, is_ref) == expected


# ---------------------------------------------------------------------------
# name normalization helpers
# ---------------------------------------------------------------------------


def test_educore_short_and_framework_label_map():
    assert ee._educore_short("(EDUcore) SIF") == "SIF"
    assert ee._educore_short("(EDUcore) MedBiquitous") == "MedBiquitous"
    assert ee._educore_short("Some Other Model") is None
    assert ee._educore_short(None) is None
    # short label -> graph framework label (used to query MAPS_TO)
    assert ee._GRAPH_LABEL_BY_SHORT["SIF"] == "SifModel"
    assert ee._GRAPH_LABEL_BY_SHORT["CEDS"] == "CEDS"


def test_resolve_attr_normalizes_and_tries_each_entity():
    # resolution map mirrors what the importer stores: PascalCase entity, camelCase attr.
    resolution = {
        ("OrganizationCalendar", "calendarDescription"): (101, 202),
        ("AccountingPeriod", "endDate"): (110, 220),
    }
    # graph names (spaces / raw casing) normalize and resolve
    assert ee._resolve_attr(resolution, ["Organization Calendar"], "Calendar Description") == (101, 202)
    # tries each owning entity until one hits
    assert ee._resolve_attr(resolution, ["Nope", "Accounting Period"], "End Date") == (110, 220)
    # unresolved -> None
    assert ee._resolve_attr(resolution, ["Missing"], "whatever") is None
    assert ee._resolve_attr(resolution, [], "x") is None
    assert ee._resolve_attr(resolution, ["AccountingPeriod"], None) is None


def test_to_pascal_and_camel():
    assert ee.to_pascal("Academic Certificate") == "AcademicCertificate"
    assert ee.to_pascal("firstName") == "FirstName"
    assert ee.to_camel("Accredited By") == "accreditedBy"
    assert ee.to_camel("FirstName") == "firstName"
    # already-camel stays camel
    assert ee.to_camel("firstName") == "firstName"


# ---------------------------------------------------------------------------
# SSE / JSON-RPC parsing
# ---------------------------------------------------------------------------


def test_parse_sse_json_extracts_data_line():
    rpc = {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "[]"}]}}
    sse = "event: message\ndata: " + json.dumps(rpc) + "\n\n"
    assert ee._parse_sse_json(sse) == rpc


def test_parse_sse_json_plain_json_fallback():
    rpc = {"jsonrpc": "2.0", "result": {}}
    assert ee._parse_sse_json(json.dumps(rpc)) == rpc


def test_extract_rows_from_content_text():
    rows = [
        {"key": "LifRoot", "title": "LIF", "version": "2.0", "description": "x"},
        {"key": "PescRoot", "title": "PESC", "version": None, "description": ""},
    ]
    rpc = {"result": {"content": [{"type": "text", "text": json.dumps(rows)}]}}
    assert ee._extract_rows(rpc) == rows


def test_extract_rows_from_structured_content():
    rows = [{"entityName": "Name", "propName": "firstName"}]
    rpc = {"result": {"structuredContent": {"result": rows}}}
    assert ee._extract_rows(rpc) == rows


def test_extract_rows_empty_when_no_content():
    assert ee._extract_rows({"result": {}}) == []


# ---------------------------------------------------------------------------
# run_cypher: full handshake via mocked requests (no network)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, *, headers=None, text="", json_body=None):
        self.headers = headers or {}
        self.text = text
        self._json_body = json_body

    def raise_for_status(self):
        return None

    def json(self):
        return self._json_body


class _EncodingAwareResponse:
    """Models ``requests.Response`` encoding behavior for the UTF-8 decode test.

    Holds the raw response *bytes* and decodes ``.text`` using ``self.encoding``
    at access time — exactly like ``requests`` does. ``encoding`` starts at the
    RFC-2616 default (``ISO-8859-1``) that ``requests`` uses for ``text/*``
    bodies without a charset, which is what produced the em-dash mojibake.
    """

    def __init__(self, *, headers=None, raw_bytes=b""):
        self.headers = headers or {}
        self._raw = raw_bytes
        self.encoding = "ISO-8859-1"  # requests' default for charset-less text/*

    @property
    def text(self) -> str:
        return self._raw.decode(self.encoding or "ISO-8859-1")

    def raise_for_status(self):
        return None


def test_run_cypher_handshake_and_call(monkeypatch):
    """Drive the 3-step handshake against a scripted fake ``requests.post``."""
    rows = [{"key": "LifRoot", "title": "LIF"}]
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        method = json.get("method")
        if method == "initialize":
            return _FakeResponse(
                headers={"mcp-session-id": "sess-123"},
                text="data: " + __import__("json").dumps({"jsonrpc": "2.0", "id": 1, "result": {}}),
            )
        if method == "notifications/initialized":
            assert headers["mcp-session-id"] == "sess-123"
            return _FakeResponse(text="")
        if method == "tools/call":
            assert headers["mcp-session-id"] == "sess-123"
            rpc = {"result": {"content": [{"type": "text", "text": __import__("json").dumps(rows)}]}}
            return _FakeResponse(text="data: " + __import__("json").dumps(rpc))
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(ee.requests, "post", fake_post)

    client = ee.EducoreClient(url="https://educore.test/mcp")
    result = client.run_cypher("MATCH (n) RETURN n", {"rootName": "LIF"})

    assert result == rows
    methods = [c.get("method") for c in calls]
    assert methods == ["initialize", "notifications/initialized", "tools/call"]
    # The tool call carried the query + params through unchanged.
    tool_args = calls[-1]["params"]["arguments"]
    assert tool_args["query"] == "MATCH (n) RETURN n"
    assert tool_args["params"] == {"rootName": "LIF"}


def test_run_cypher_raises_runtime_error_on_failure(monkeypatch):
    def boom(*args, **kwargs):
        raise ee.requests.RequestException("connection refused")

    monkeypatch.setattr(ee.requests, "post", boom)
    client = ee.EducoreClient(url="https://educore.test/mcp")
    with pytest.raises(RuntimeError, match="EDUcore request failed"):
        client.run_cypher("MATCH (n) RETURN n")


# ---------------------------------------------------------------------------
# run_cypher_paged: accumulation + stop conditions (monkeypatched run_cypher)
# ---------------------------------------------------------------------------


def test_run_cypher_paged_accumulates_and_stops(monkeypatch):
    """Two full pages then a short page -> all rows accumulated, then stop.

    Also asserts the runner drives ``skip`` itself (0, 100, 200) and leaves the
    caller's other params (``rootName``) intact.
    """
    page_size = 100
    page0 = [{"entityName": "E", "propName": f"p{i}"} for i in range(page_size)]
    page1 = [{"entityName": "E", "propName": f"p{i}"} for i in range(page_size, 2 * page_size)]
    page2 = [{"entityName": "E", "propName": f"p{i}"} for i in range(2 * page_size, 2 * page_size + 7)]
    pages = [page0, page1, page2]

    seen_params: list[dict] = []

    def fake_run_cypher(query, params=None):
        seen_params.append(params)
        return pages[params["skip"] // page_size]

    monkeypatch.setattr(ee, "run_cypher", fake_run_cypher)

    rows = ee.run_cypher_paged("CYPHER", {"rootName": "LIF"}, page_size=page_size)

    # All three pages accumulated (100 + 100 + 7), and looping stopped on the
    # short page (no 4th call).
    assert len(rows) == 2 * page_size + 7
    assert rows == page0 + page1 + page2
    # The runner owned skip/limit and preserved the caller's rootName.
    assert [p["skip"] for p in seen_params] == [0, page_size, 2 * page_size]
    assert all(p["limit"] == page_size for p in seen_params)
    assert all(p["rootName"] == "LIF" for p in seen_params)
    # Exactly three calls — it stopped after the short page.
    assert len(seen_params) == 3


def test_run_cypher_paged_stops_on_empty_page(monkeypatch):
    """A full first page followed by an empty page (exact multiple) stops cleanly."""
    page_size = 100
    page0 = [{"i": i} for i in range(page_size)]
    pages = [page0, []]

    def fake_run_cypher(query, params=None):
        return pages[params["skip"] // page_size]

    monkeypatch.setattr(ee, "run_cypher", fake_run_cypher)
    rows = ee.run_cypher_paged("CYPHER", {}, page_size=page_size)
    assert len(rows) == page_size


def test_run_cypher_paged_caps_runaway_loop(monkeypatch, caplog):
    """If the server ignores SKIP (every page is full), the runner caps + warns."""
    page_size = 100
    full_page = [{"i": i} for i in range(page_size)]

    def fake_run_cypher(query, params=None):
        return list(full_page)

    monkeypatch.setattr(ee, "run_cypher", fake_run_cypher)
    monkeypatch.setattr(ee, "_MAX_PAGES", 5)

    import logging

    with caplog.at_level(logging.WARNING):
        rows = ee.run_cypher_paged("CYPHER", {}, page_size=page_size)

    assert len(rows) == 5 * page_size  # capped at _MAX_PAGES pages
    assert any("max-pages cap" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# UTF-8 decoding of the SSE body (Bug #3: em-dash mojibake)
# ---------------------------------------------------------------------------


def test_call_cypher_decodes_sse_body_as_utf8(monkeypatch):
    """An em-dash in the SSE body must decode cleanly, not as latin-1 mojibake.

    The fake response models ``requests``: it holds raw UTF-8 *bytes* and decodes
    ``.text`` via ``self.encoding`` (defaulting to ISO-8859-1, the source of the
    bug). The client must set ``encoding = "utf-8"`` before reading ``.text``.
    """
    em_dash = "—"  # —
    rows = [{"entityName": "CTDL", "entityDescription": f"Description {em_dash} 138 classes"}]
    rpc = {"result": {"content": [{"type": "text", "text": json.dumps(rows, ensure_ascii=False)}]}}
    sse_bytes = ("event: message\ndata: " + json.dumps(rpc, ensure_ascii=False) + "\n\n").encode("utf-8")

    def fake_post(url, headers=None, json=None, timeout=None):
        method = json.get("method")
        if method == "initialize":
            init_rpc = {"jsonrpc": "2.0", "id": 1, "result": {}}
            init_bytes = ("data: " + __import__("json").dumps(init_rpc)).encode("utf-8")
            return _EncodingAwareResponse(headers={"mcp-session-id": "sess-1"}, raw_bytes=init_bytes)
        if method == "notifications/initialized":
            return _EncodingAwareResponse(raw_bytes=b"")
        if method == "tools/call":
            return _EncodingAwareResponse(raw_bytes=sse_bytes)
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(ee.requests, "post", fake_post)

    client = ee.EducoreClient(url="https://educore.test/mcp")
    result = client.run_cypher("MATCH (n) RETURN n", {"rootName": "CTDL"})

    assert result == rows
    desc = result[0]["entityDescription"]
    assert em_dash in desc  # clean em-dash
    assert "â€" not in desc  # no "â€" mojibake
