"""Unit tests for the EDUcore Cypher loader registry.

These are pure (no network): they assert the dispatch contract and the
paginatable-query shape that ``run_cypher_paged`` depends on. The Cypher itself
is verified live against the graph (see the per-standard comments in
``educore_queries``); here we lock in the structural invariants that a typo or a
bad future edit would silently break.
"""

from __future__ import annotations

import os

import pytest

# Importing anything under `lif.mdr_restapi` pulls in `database_setup`, which
# builds a SQLAlchemy engine from POSTGRESQL_* env vars at import time. These
# tests are pure, so provide harmless placeholders (mirrors test_educore_endpoints).
os.environ.setdefault("POSTGRESQL_USER", "postgres")
os.environ.setdefault("POSTGRESQL_PASSWORD", "postgres")
os.environ.setdefault("POSTGRESQL_HOST", "localhost")
os.environ.setdefault("POSTGRESQL_PORT", "5432")
os.environ.setdefault("POSTGRESQL_DB", "postgres")

from lif.mdr_restapi import educore_queries as q  # noqa: E402

# Every standard that currently has a verified loader. CIP/SOC/SEDM are
# deliberately excluded (flat code taxonomies / heterogeneous models — they list
# as "Coming soon" rather than importing as empty/meaningless DataModels).
_EXPECTED_SUPPORTED = {
    "LifRoot",
    "PescRoot",
    "CaseRoot",
    "CedsOntology",
    "EdfiRoot",
    "CtdlRoot",
    "ClrRoot",
    "EduApiRoot",
    "OpenBadgesRoot",
    "JedxRoot",
    "SifRoot",
    "DctapRoot",
    "MedBiqModel",
}

_NOT_IMPORTABLE = {"CipRoot", "SocRoot", "SedmRoot"}


def test_supported_keys_match_expected():
    assert set(q.SUPPORTED_KEYS) == _EXPECTED_SUPPORTED


@pytest.mark.parametrize("key", sorted(_EXPECTED_SUPPORTED))
def test_loader_is_paginatable_and_well_formed(key):
    """Each loader exposes entities/properties Cypher plus skip/limit defaults.

    ``run_cypher_paged`` requires every query to end with the stable
    ``ORDER BY ... SKIP $skip LIMIT $limit`` clause so pages don't overlap.
    """
    loader = q.get_load_cypher(key)
    assert set(loader) == {"entities", "properties", "params"}
    # Pagination defaults are always bound so a single-shot call still works.
    assert loader["params"]["skip"] == 0
    assert loader["params"]["limit"] == 100
    assert loader["params"]["rootName"]  # non-empty label
    for cypher in (loader["entities"], loader["properties"]):
        assert "ORDER BY" in cypher
        assert cypher.rstrip().endswith("SKIP toInteger($skip) LIMIT toInteger($limit)")


def test_get_load_cypher_returns_copies():
    """Callers must not be able to mutate the module-level templates."""
    a = q.get_load_cypher("SifRoot")
    a["params"]["skip"] = 999
    b = q.get_load_cypher("SifRoot")
    assert b["params"]["skip"] == 0


@pytest.mark.parametrize("key", sorted(_NOT_IMPORTABLE))
def test_taxonomy_standards_have_no_loader(key):
    with pytest.raises(KeyError):
        q.get_load_cypher(key)


def test_graph_framework_labels_cover_all_standards():
    """Every standard key (importable or not) maps to a graph framework label."""
    # The 16 keys = 13 supported + 3 taxonomy (CIP/SOC/SEDM).
    assert set(q.GRAPH_FRAMEWORK_LABEL_BY_KEY) == _EXPECTED_SUPPORTED | _NOT_IMPORTABLE
    assert q.GRAPH_FRAMEWORK_LABEL_BY_KEY["SifRoot"] == "SifModel"
    assert q.GRAPH_FRAMEWORK_LABEL_BY_KEY["CedsOntology"] == "CEDS"
    assert q.GRAPH_FRAMEWORK_LABEL_BY_KEY["MedBiqModel"] == "MedBiqModel"


def test_maps_to_field_cypher_valid_and_paginatable():
    cypher = q.maps_to_field_cypher("SifModel", "CEDS")
    assert "(s:SifModel)-[:MAPS_TO]->(t:CEDS)" in cypher
    assert cypher.rstrip().endswith("SKIP toInteger($skip) LIMIT toInteger($limit)")
    # Field-level only: predicate filters Property/Field/Element/Attribute.
    assert "ENDS WITH 'Field'" in cypher
    assert "sourceField" in cypher and "targetField" in cypher


def test_maps_to_field_cypher_rejects_unknown_label():
    """Guards the f-string label injection (labels can't be parameterized)."""
    with pytest.raises(KeyError):
        q.maps_to_field_cypher("SifModel", "Bobby'; DROP")
    with pytest.raises(KeyError):
        q.maps_to_field_cypher("NotAFramework", "CEDS")


def test_list_query_anchors_medbiquitous():
    """MedBiquitous has no ``*Root`` node, so the list query must UNION it in.

    Without the explicit ``role: 'portfolio-root'`` branch it would never appear
    in the drawer (the regression this fix addresses).
    """
    cypher = q.LIST_STANDARDS_CYPHER
    assert "MedBiqModel" in cypher
    assert "portfolio-root" in cypher
    # Still returns the standardized list columns.
    for col in ("key", "title", "version", "description"):
        assert col in cypher
