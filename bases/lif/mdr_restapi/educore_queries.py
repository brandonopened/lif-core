"""Verified Cypher queries for loading education-standard data models from the
EDUcore knowledge graph (remote MCP server / Neo4j) into the LIF MDR app.

This module is PURE: it contains only Cypher strings and a small dispatch
function. It performs no I/O and no network calls. The backend agent owns the
actual MCP/Bolt execution and the MDR ingestion.

Every node in EDUcore also carries the super-label ``ForgedNode`` — do NOT
filter with ``NOT x:ForgedNode`` (it removes everything).

Graph shapes differ per standard. Each loader below was verified live against
the graph via ``mcp__educore-standards__cypherQuery``. A 1-line sample of the
real observed output is pasted in a comment above each query block.

Output-column contract (standardized across every standard via Cypher aliases):

  LIST_STANDARDS_CYPHER rows  -> key, title, version, description
  entities cypher rows        -> entityName, entityDescription
  properties cypher rows      -> entityName, propName, dataType, required, isRef

``dataType`` is the standard's NATIVE type string. ``required`` and ``isRef``
are always booleans (nulls coalesced to false).

Pagination contract
--------------------
EDUcore's MCP ``cypherQuery`` tool caps each call at ~100–101 rows regardless of
any ``LIMIT`` baked into the query, so large standards (e.g. LIF's ~842 property
rows) get truncated. To work around this, the ``entities`` and ``properties``
queries are deterministically ordered and paginatable: each ends with a stable
``ORDER BY`` followed by ``SKIP $skip LIMIT $limit``. Queries that dedup (via
``DISTINCT``/``UNION``/``collect``) wrap the dedup in an inner block that
produces exactly one row per item, then order/skip/limit at the very end so
pages are stable and non-overlapping.

The returned ``params`` carry ``skip``/``limit`` defaults (``skip: 0,
limit: 100``). The runner in ``educore_endpoints.py`` overrides ``skip`` (and may
override ``limit``) per page and loops until a short page is returned. Callers
that want a single page can pass the params through unchanged.
"""

from __future__ import annotations

# Default page parameters merged into every loader's ``params`` so the
# entities/properties Cypher always have ``$skip``/``$limit`` bound even on a
# single-shot call. The paged runner overrides ``skip`` per page.
_PAGE_DEFAULTS: dict[str, int] = {"skip": 0, "limit": 100}

# ---------------------------------------------------------------------------
# LIST: one row per standard data-model root, returned DYNAMICALLY.
# ---------------------------------------------------------------------------
#
# `key` is the standard-specific root label (e.g. "LifRoot", "PescRoot",
# "CedsOntology"). It is stable and is what get_load_cypher() dispatches on.
# We exclude DmeSchemaRoot (graph infrastructure, not an education standard).
#
# Most standards expose a dedicated ``<Std>Root`` node (plus CEDS via the
# ``CedsOntology`` label). MedBiquitous is the exception: it has NO ``*Root``
# node — its portfolio root is a ``MedBiqModel`` node with
# ``role = 'portfolio-root'`` (the other MedBiqModel nodes are per-XSD
# content-standard anchors). It is UNION-ed in explicitly under the synthetic
# key ``MedBiqModel`` so it shows up in the drawer like every other standard.
#
# Verified sample row:
#   {"key":"LifRoot","title":"Machine-Readable Schema for LIF",
#    "version":"2.0","description":"OpenAPI Spec"}
# Live run returned 16 standards (CASE, CEDS, CIP, CLR, CTDL, DCTAP, Ed-Fi,
# EduApi, JEDx, LIF, MedBiquitous, OpenBadges, PESC, SEDM, SIF, SOC), all keys
# distinct.
LIST_STANDARDS_CYPHER: str = """
CALL {
  MATCH (r)
  WHERE (any(l IN labels(r) WHERE l ENDS WITH 'Root') OR r:CedsOntology)
    AND NOT r:DmeSchemaRoot
  WITH r, [l IN labels(r) WHERE (l ENDS WITH 'Root' OR l = 'CedsOntology')][0] AS key
  RETURN key AS key,
         coalesce(r.title, r.name) AS title,
         r.version AS version,
         coalesce(r.description, '') AS description
  UNION
  MATCH (r:MedBiqModel {role: 'portfolio-root'})
  RETURN 'MedBiqModel' AS key,
         coalesce(r.title, r.name) AS title,
         r.version AS version,
         coalesce(r.description, '') AS description
}
RETURN key, title, version, description
ORDER BY title
""".strip()


# ===========================================================================
# LIF  (key = "LifRoot")
# ===========================================================================
#
# Shape: LifRoot -[:HAS_ENTITY]-> LifEntity -[:HAS_PROPERTY]-> LifProperty
# BUT the Person model (and a few others) expose their sub-structure as
# LifComposite nodes instead of flat properties:
#   LifRoot -[:HAS_ENTITY]-> LifEntity -[:HAS_COMPOSITE]-> LifComposite
#                                       -[:HAS_PROPERTY]-> LifProperty
# Composites (Name, Contact, Identifier, CredentialAward, Proficiency, ...) are
# exactly the PascalCase child-entities of the LIF convention; their properties
# (firstName, lastName, ...) are the camelCase scalar attributes. So we surface
# BOTH the direct entities (those with HAS_PROPERTY) AND the composites as
# "entities", and collect properties from either owner.
#
# LifProperty carries: name, dataType, required, isRef  (clean booleans).
#
# Verified entity sample:   {"entityName":"Name", "entityDescription":"In the treatment..."}
# Verified property sample: {"entityName":"Name","propName":"firstName",
#                            "dataType":"string","required":true,"isRef":false}
# Counts: 7 entities-with-direct-props + 33 composites = 40 LIF "entities".
# The UNION dedups across the direct-entity and composite branches; wrapping it
# in a CALL subquery lets a single ORDER BY + SKIP/LIMIT page the combined,
# deduped result (one row per entity), so pages stay stable and non-overlapping.
_LIF_ENTITIES = """
CALL {
  MATCH (r:LifRoot {name: $rootName})-[:HAS_ENTITY]->(e:LifEntity)
  WHERE (e)-[:HAS_PROPERTY]->(:LifProperty)
  RETURN DISTINCT e.name AS entityName, coalesce(e.description, '') AS entityDescription
  UNION
  MATCH (r:LifRoot {name: $rootName})-[:HAS_ENTITY]->(:LifEntity)-[:HAS_COMPOSITE]->(c:LifComposite)
  RETURN DISTINCT c.name AS entityName, coalesce(c.description, '') AS entityDescription
}
RETURN entityName, entityDescription
ORDER BY entityName
SKIP toInteger($skip) LIMIT toInteger($limit)
""".strip()

_LIF_PROPERTIES = """
CALL {
  MATCH (r:LifRoot {name: $rootName})-[:HAS_ENTITY]->(e:LifEntity)-[:HAS_PROPERTY]->(p:LifProperty)
  RETURN DISTINCT e.name AS entityName, p.name AS propName,
         coalesce(p.dataType, '') AS dataType,
         coalesce(p.required, false) AS required,
         coalesce(p.isRef, false) AS isRef
  UNION
  MATCH (r:LifRoot {name: $rootName})-[:HAS_ENTITY]->(:LifEntity)-[:HAS_COMPOSITE]->(c:LifComposite)-[:HAS_PROPERTY]->(p:LifProperty)
  RETURN DISTINCT c.name AS entityName, p.name AS propName,
         coalesce(p.dataType, '') AS dataType,
         coalesce(p.required, false) AS required,
         coalesce(p.isRef, false) AS isRef
}
RETURN entityName, propName, dataType, required, isRef
ORDER BY entityName, propName
SKIP toInteger($skip) LIMIT toInteger($limit)
""".strip()


# ===========================================================================
# PESC  (key = "PescRoot")
# ===========================================================================
#
# Shape: PescRoot -[:HAS_SOURCE_FILE]-> PescSourceFile -[:DEFINES_TYPE]->
#        PescComplexType -[:HAS_ELEMENT]-> PescElement
# (PescComplexType has NO direct edge from the root; reach it via source files.)
#
# PescElement has no dataType/required/isRef fields, so they are DERIVED:
#   dataType = el.typeName  (native XSD type ref string, e.g. "core:NameType")
#   required = (minOccurs <> "0")            -- minOccurs/maxOccurs are strings
#   isRef    = element points at a complex type via HAS_TYPE (object reference)
#
# Complex-type names repeat across source files (600 nodes, 256 distinct names);
# the entities query dedupes by name (keeps one description). Properties are
# keyed by complex-type name.
#
# Verified entity sample:   {"entityName":"AcademicAwardType",
#                            "entityDescription":"Academic awards, degrees, ..."}
# Verified property sample: {"entityName":"AcademicAwardType",
#                            "propName":"AcademicAwardProgram",
#                            "dataType":"AcRec:AcademicProgramType",
#                            "required":false,"isRef":true}
# Counts: 256 distinct complex types.
_PESC_ENTITIES = """
MATCH (r:PescRoot {name: $rootName})-[:HAS_SOURCE_FILE]->(:PescSourceFile)-[:DEFINES_TYPE]->(ct:PescComplexType)
WITH ct.name AS entityName, collect(coalesce(ct.description, ''))[0] AS entityDescription
RETURN entityName, entityDescription
ORDER BY entityName
SKIP toInteger($skip) LIMIT toInteger($limit)
""".strip()

# The DISTINCT collapses element-name repeats across source files; collect those
# into an inner row-per-property set first, then order/skip/limit at the end so
# pages are stable and non-overlapping.
_PESC_PROPERTIES = """
CALL {
  MATCH (r:PescRoot {name: $rootName})-[:HAS_SOURCE_FILE]->(:PescSourceFile)-[:DEFINES_TYPE]->(ct:PescComplexType)-[:HAS_ELEMENT]->(el:PescElement)
  OPTIONAL MATCH (el)-[:HAS_TYPE]->(refct:PescComplexType)
  RETURN DISTINCT ct.name AS entityName, el.name AS propName,
         coalesce(el.typeName, '') AS dataType,
         (coalesce(el.minOccurs, '0') <> '0') AS required,
         (refct IS NOT NULL) AS isRef
}
RETURN entityName, propName, dataType, required, isRef
ORDER BY entityName, propName
SKIP toInteger($skip) LIMIT toInteger($limit)
""".strip()


# ===========================================================================
# CASE  (key = "CaseRoot")
# ===========================================================================
#
# Shape: CaseRoot -[:HAS_CLASS]-> CaseClass -[:HAS_PROPERTY]-> CaseProperty
#
# CaseProperty has native fields: dataType, required (real boolean). There is no
# isRef field, so it is derived from refCount (>0 means the property references
# another schema). dataType is the native CASE type ("string","reference",
# "array","integer","number","anyOf",...).
#
# Verified entity sample:   {"entityName":"CFAssociation","entityDescription":"This is the container..."}
# Verified property sample: {"entityName":"CFAssociation","propName":"destinationNodeURI",
#                            "dataType":"reference","required":true,"isRef":true}
# Counts: 36 classes.
_CASE_ENTITIES = """
MATCH (r:CaseRoot {name: $rootName})-[:HAS_CLASS]->(c:CaseClass)
WITH c.name AS entityName, collect(coalesce(c.description, ''))[0] AS entityDescription
RETURN entityName, entityDescription
ORDER BY entityName
SKIP toInteger($skip) LIMIT toInteger($limit)
""".strip()

_CASE_PROPERTIES = """
CALL {
  MATCH (r:CaseRoot {name: $rootName})-[:HAS_CLASS]->(c:CaseClass)-[:HAS_PROPERTY]->(p:CaseProperty)
  RETURN DISTINCT c.name AS entityName, p.name AS propName,
         coalesce(p.dataType, '') AS dataType,
         coalesce(p.required, false) AS required,
         (coalesce(p.refCount, 0) > 0) AS isRef
}
RETURN entityName, propName, dataType, required, isRef
ORDER BY entityName, propName
SKIP toInteger($skip) LIMIT toInteger($limit)
""".strip()


# ===========================================================================
# CEDS  (key = "CedsOntology")
# ===========================================================================
#
# The CedsOntology root has NO outgoing structural edges. CedsClass nodes are
# identified by label (a single CEDS ontology). Shape:
#   CedsClass -[:HAS_PROPERTY]-> CedsProperty
#
# CedsProperty has dataType on ~42% of nodes; it has NO `required` and NO ref
# concept (class relationships are SUBCLASS_OF, not property refs). So:
#   dataType = coalesce(p.dataType, '')   (native XSD-ish, e.g. "nonNegativeInteger")
#   required = false (always)
#   isRef    = false (always)
#
# $rootName is accepted but unused (CEDS is matched by label); kept for a
# uniform params contract.
#
# Verified property sample: {"entityName":"Accessibility Feature",
#                            "propName":"Assessment Extended Time Duration",
#                            "dataType":"nonNegativeInteger","required":false,"isRef":false}
# Counts: 402 classes (all distinct names).
_CEDS_ENTITIES = """
MATCH (c:CedsClass)
WITH c.name AS entityName, collect(coalesce(c.description, ''))[0] AS entityDescription
RETURN entityName, entityDescription
ORDER BY entityName
SKIP toInteger($skip) LIMIT toInteger($limit)
""".strip()

_CEDS_PROPERTIES = """
CALL {
  MATCH (c:CedsClass)-[:HAS_PROPERTY]->(p:CedsProperty)
  RETURN DISTINCT c.name AS entityName, p.name AS propName,
         coalesce(p.dataType, '') AS dataType,
         false AS required,
         false AS isRef
}
RETURN entityName, propName, dataType, required, isRef
ORDER BY entityName, propName
SKIP toInteger($skip) LIMIT toInteger($limit)
""".strip()


# ===========================================================================
# Ed-Fi  (key = "EdfiRoot")
# ===========================================================================
#
# Shape: EdfiRoot -[:HAS_ENTITY]-> EdfiEntity -[:HAS_FIELD]-> EdfiField
#
# EdfiField has: elementType (the type, e.g. "String"/"Date"/"Number"/
# "Boolean"/"Descriptor") and required (real boolean). No isRef field, so:
#   dataType = elementType
#   required = field.required
#   isRef    = (elementType == "Descriptor")  -- references a controlled vocab
#
# Verified property sample: {"entityName":"AcademicHonor",
#                            "propName":"AcademicHonorCategoryDescriptor",
#                            "dataType":"Descriptor","required":false,"isRef":true}
# Counts: 201 entities.
_EDFI_ENTITIES = """
MATCH (r:EdfiRoot {name: $rootName})-[:HAS_ENTITY]->(e:EdfiEntity)
WITH e.name AS entityName, collect(coalesce(e.description, ''))[0] AS entityDescription
RETURN entityName, entityDescription
ORDER BY entityName
SKIP toInteger($skip) LIMIT toInteger($limit)
""".strip()

_EDFI_PROPERTIES = """
CALL {
  MATCH (r:EdfiRoot {name: $rootName})-[:HAS_ENTITY]->(e:EdfiEntity)-[:HAS_FIELD]->(f:EdfiField)
  RETURN DISTINCT e.name AS entityName, f.name AS propName,
         coalesce(f.elementType, '') AS dataType,
         coalesce(f.required, false) AS required,
         (f.elementType = 'Descriptor') AS isRef
}
RETURN entityName, propName, dataType, required, isRef
ORDER BY entityName, propName
SKIP toInteger($skip) LIMIT toInteger($limit)
""".strip()


# ===========================================================================
# CTDL  (key = "CtdlRoot")
# ===========================================================================
#
# Shape: CtdlRoot -[:HAS_CLASS]-> CtdlClass -[:HAS_PROPERTY]-> CtdlProperty
# (RDF-style: properties are shared across class domains; 7210 class-property
# pairs over 138 classes.)
#
# CtdlProperty has rangeTypes (a LIST of type URIs) and no dataType/required/
# isRef fields, so:
#   dataType = rangeTypes[0]  (e.g. "ceterms:Organization", "xsd:string", "rdf:langString")
#   required = false (always)
#   isRef    = first range type is NOT an xsd:/rdf: literal -> it points at a
#              ceterms:/ceasn:/skos: class (an object reference)
#
# Verified property sample: {"entityName":"Academic Certificate",
#                            "propName":"Accredited By","dataType":"ceterms:Organization",
#                            "required":false,"isRef":true}
# Counts: 138 classes.
_CTDL_ENTITIES = """
MATCH (r:CtdlRoot {name: $rootName})-[:HAS_CLASS]->(c:CtdlClass)
WITH c.name AS entityName, collect(coalesce(c.description, ''))[0] AS entityDescription
RETURN entityName, entityDescription
ORDER BY entityName
SKIP toInteger($skip) LIMIT toInteger($limit)
""".strip()

_CTDL_PROPERTIES = """
CALL {
  MATCH (r:CtdlRoot {name: $rootName})-[:HAS_CLASS]->(c:CtdlClass)-[:HAS_PROPERTY]->(p:CtdlProperty)
  WITH c, p, coalesce(p.rangeTypes[0], '') AS firstRange
  RETURN DISTINCT c.name AS entityName, p.name AS propName,
         firstRange AS dataType,
         false AS required,
         (firstRange <> '' AND NOT (firstRange STARTS WITH 'xsd:' OR firstRange STARTS WITH 'rdf:')) AS isRef
}
RETURN entityName, propName, dataType, required, isRef
ORDER BY entityName, propName
SKIP toInteger($skip) LIMIT toInteger($limit)
""".strip()


# ===========================================================================
# CLR / Edu-API / Open Badges  (keys = "ClrRoot" / "EduApiRoot" / "OpenBadgesRoot")
# ===========================================================================
#
# These three are JSON-Schema/OpenAPI standards sharing the exact CASE shape,
# just with standard-specific labels:
#   <Std>Root -[:HAS_CLASS]-> <Std>Class -[:HAS_PROPERTY]-> <Std>Property
# We match the class by label (every <Std>Class belongs to the one standard
# root, so the root hop is unnecessary — same approach as CEDS).
#
# <Std>Property carries: dataType (native JSON type: "string"/"anyOf"/"array"/
# "integer"/...), required (real boolean), and referencedSchemas (a LIST of
# referenced schema names; non-empty => the property is an object reference).
#
# Verified property sample (CLR): {"entityName":"Address","propName":"addressLocality",
#   "dataType":"string","required":false,"isRef":false}
# Counts: CLR 25 classes, Edu-API 34 classes, Open Badges 23 classes.


def _classprop_entities(class_label: str) -> str:
    return f"""
MATCH (c:{class_label})
WITH c.name AS entityName, collect(coalesce(c.description, ''))[0] AS entityDescription
RETURN entityName, entityDescription
ORDER BY entityName
SKIP toInteger($skip) LIMIT toInteger($limit)
""".strip()


def _classprop_properties(class_label: str, prop_label: str) -> str:
    return f"""
CALL {{
  MATCH (c:{class_label})-[:HAS_PROPERTY]->(p:{prop_label})
  RETURN DISTINCT c.name AS entityName, p.name AS propName,
         coalesce(p.dataType, '') AS dataType,
         coalesce(p.required, false) AS required,
         (size(coalesce(p.referencedSchemas, [])) > 0) AS isRef
}}
RETURN entityName, propName, dataType, required, isRef
ORDER BY entityName, propName
SKIP toInteger($skip) LIMIT toInteger($limit)
""".strip()


_CLR_ENTITIES = _classprop_entities("ClrClass")
_CLR_PROPERTIES = _classprop_properties("ClrClass", "ClrProperty")
_EDUAPI_ENTITIES = _classprop_entities("EduApiClass")
_EDUAPI_PROPERTIES = _classprop_properties("EduApiClass", "EduApiProperty")
_OPENBADGES_ENTITIES = _classprop_entities("OpenBadgesClass")
_OPENBADGES_PROPERTIES = _classprop_properties("OpenBadgesClass", "OpenBadgesProperty")


# ===========================================================================
# JEDx  (key = "JedxRoot")
# ===========================================================================
#
# Shape: JedxRoot -[:HAS_ENTITY]-> JedxEntity -[:HAS_FIELD]-> JedxField
#
# JedxField has: type (the native type string, e.g. "string"/"Competences"),
# isPrimaryKey and isForeignKey (real booleans). No explicit required flag, so:
#   dataType = field.type
#   required = field.isPrimaryKey  (PKs are mandatory; nothing else is marked)
#   isRef    = field.isForeignKey  (FK fields point at another entity)
#
# Verified property sample: {"entityName":"job","propName":"RefId",
#   "dataType":"string","required":false,"isRef":false}
# Counts: 5 entities.
_JEDX_ENTITIES = """
MATCH (e:JedxEntity)
WITH e.name AS entityName, collect(coalesce(e.description, ''))[0] AS entityDescription
RETURN entityName, entityDescription
ORDER BY entityName
SKIP toInteger($skip) LIMIT toInteger($limit)
""".strip()

_JEDX_PROPERTIES = """
CALL {
  MATCH (e:JedxEntity)-[:HAS_FIELD]->(f:JedxField)
  RETURN DISTINCT e.name AS entityName, f.name AS propName,
         coalesce(f.type, '') AS dataType,
         coalesce(f.isPrimaryKey, false) AS required,
         coalesce(f.isForeignKey, false) AS isRef
}
RETURN entityName, propName, dataType, required, isRef
ORDER BY entityName, propName
SKIP toInteger($skip) LIMIT toInteger($limit)
""".strip()


# ===========================================================================
# SIF  (key = "SifRoot")
# ===========================================================================
#
# SifRoot has no outgoing edges (like CedsOntology), so we match the entities by
# label. Shape: SifObject -[:HAS_FIELD]-> SifField
#
# SifField has NO native dataType (types live on shared SifComplexType nodes we
# don't traverse in this flat v1), so dataType is left '' (-> "string"). It does
# carry ``mandatory`` (real boolean) and ``isAttribute`` (XML attribute vs
# element — NOT a reference). So:
#   dataType = ''        (defaults to "string")
#   required = field.mandatory
#   isRef    = false
#
# Object names are unique (159), but one object can carry TWO SifField nodes with
# the SAME name and different ``mandatory`` (e.g. AccountingPeriod.@Codeset), so a
# plain DISTINCT would emit two rows for one (entity, prop) and break stable
# paging. We GROUP by (entityName, propName) and mark required if mandatory on ANY
# matching field — guaranteeing exactly one row per property.
#
# Verified property sample: {"entityName":"AccountingPeriod","propName":"@Name",
#   "dataType":"","required":true,"isRef":false}
# Counts: 159 objects.
_SIF_ENTITIES = """
MATCH (o:SifObject)
WITH o.name AS entityName, collect(coalesce(o.description, ''))[0] AS entityDescription
RETURN entityName, entityDescription
ORDER BY entityName
SKIP toInteger($skip) LIMIT toInteger($limit)
""".strip()

_SIF_PROPERTIES = """
CALL {
  MATCH (o:SifObject)-[:HAS_FIELD]->(f:SifField)
  WITH o.name AS entityName, f.name AS propName,
       collect(coalesce(f.mandatory, false)) AS reqFlags
  RETURN entityName, propName, '' AS dataType,
         (true IN reqFlags) AS required, false AS isRef
}
RETURN entityName, propName, dataType, required, isRef
ORDER BY entityName, propName
SKIP toInteger($skip) LIMIT toInteger($limit)
""".strip()


# ===========================================================================
# DCTAP  (key = "DctapRoot")
# ===========================================================================
#
# Shape: DctapRoot -[:HAS_COMPONENT]-> DctapComponent -[:HAS_FIELD]-> DctapElement
# (matched by label; tiny standard.)
#
# DctapElement has no dataType/required/isRef, only ``cardinality`` (e.g. "1",
# "0..1"). So:
#   dataType = ''                          (-> "string")
#   required = cardinality STARTS WITH '1' ("1"/"1..1" => mandatory)
#   isRef    = false
#
# Verified property sample: {"entityName":<component>,"propName":"propertyID",
#   "dataType":"","required":true,"isRef":false}
# Counts: 2 components.
_DCTAP_ENTITIES = """
MATCH (c:DctapComponent)
WITH c.name AS entityName, collect(coalesce(c.description, ''))[0] AS entityDescription
RETURN entityName, entityDescription
ORDER BY entityName
SKIP toInteger($skip) LIMIT toInteger($limit)
""".strip()

_DCTAP_PROPERTIES = """
CALL {
  MATCH (c:DctapComponent)-[:HAS_FIELD]->(e:DctapElement)
  RETURN DISTINCT c.name AS entityName, e.name AS propName,
         '' AS dataType,
         (coalesce(e.cardinality, '') STARTS WITH '1') AS required,
         false AS isRef
}
RETURN entityName, propName, dataType, required, isRef
ORDER BY entityName, propName
SKIP toInteger($skip) LIMIT toInteger($limit)
""".strip()


# ===========================================================================
# MedBiquitous  (key = "MedBiqModel")
# ===========================================================================
#
# MedBiquitous is an XSD/WSDL portfolio (27 XSDs). Its complex types are matched
# by label (the portfolio has no single structural root edge). Shape:
#   MedBiqComplexType -[:HAS_ELEMENT]->   MedBiqElement
#   MedBiqComplexType -[:HAS_ATTRIBUTE]-> MedBiqAttribute
# Both elements and attributes surface as "properties" of the complex type.
#
# MedBiqElement: xsdType (native type), isOptional (real boolean),
#   referencesShared (points at a shared type). MedBiqAttribute: xsdType, use
#   ("required"/"optional"). So:
#   dataType = xsdType
#   required = NOT isOptional       (elements);  use = 'required'  (attributes)
#   isRef    = referencesShared     (elements);  false             (attributes)
#
# 318 complex-type nodes share only 292 distinct names (the same type recurs
# across the portfolio's 27 XSDs), so a (entity, prop) pair can appear in more
# than one node. The entities query dedups by name (one description); the
# properties query UNION-s elements + attributes and then GROUPs by
# (entityName, propName) — required = true on ANY, isRef = true on ANY, dataType
# = first non-empty — to guarantee exactly one stable row per property.
#
# Verified property sample: {"entityName":"AcademicAppointmentType",
#   "propName":"ChairAppointment","dataType":"ChairAppointmentType",
#   "required":false,"isRef":false}
# Counts: 292 distinct complex types.
_MEDBIQ_ENTITIES = """
MATCH (ct:MedBiqComplexType)
WITH ct.name AS entityName, collect(coalesce(ct.description, ''))[0] AS entityDescription
RETURN entityName, entityDescription
ORDER BY entityName
SKIP toInteger($skip) LIMIT toInteger($limit)
""".strip()

_MEDBIQ_PROPERTIES = """
CALL {
  MATCH (ct:MedBiqComplexType)-[:HAS_ELEMENT]->(el:MedBiqElement)
  RETURN ct.name AS entityName, el.name AS propName,
         coalesce(el.xsdType, '') AS dataType,
         (NOT coalesce(el.isOptional, true)) AS required,
         coalesce(el.referencesShared, false) AS isRef
  UNION
  MATCH (ct:MedBiqComplexType)-[:HAS_ATTRIBUTE]->(a:MedBiqAttribute)
  RETURN ct.name AS entityName, a.name AS propName,
         coalesce(a.xsdType, '') AS dataType,
         (coalesce(a.use, '') = 'required') AS required,
         false AS isRef
}
WITH entityName, propName,
     collect(dataType) AS dataTypes,
     collect(required) AS reqFlags,
     collect(isRef) AS refFlags
RETURN entityName, propName,
       head([d IN dataTypes WHERE d <> ''] + ['']) AS dataType,
       (true IN reqFlags) AS required,
       (true IN refFlags) AS isRef
ORDER BY entityName, propName
SKIP toInteger($skip) LIMIT toInteger($limit)
""".strip()


# ---------------------------------------------------------------------------
# Dispatch table.
# ---------------------------------------------------------------------------
#
# Maps a standard `key` (the root label) -> the queries + params needed to load
# it. `params` always carries `rootName` = the `r.name` value the entities/
# properties Cypher filters on (verified live):
#   LifRoot -> "LIF", PescRoot -> "PESC", CaseRoot -> "CASE",
#   EdfiRoot -> "Ed-Fi", CtdlRoot -> "CTDL".
# The label-matched loaders (CedsOntology, CLR, Edu-API, Open Badges, SIF,
# DCTAP, MedBiquitous) ignore rootName but still carry it for a uniform params
# contract. CIP/SOC/SEDM are intentionally absent: they are flat code
# taxonomies / heterogeneous models, not entity-property schemas, so they list
# in the drawer as "Coming soon" rather than importing as empty DataModels.
_LOADERS: dict[str, dict] = {
    "LifRoot": {"entities": _LIF_ENTITIES, "properties": _LIF_PROPERTIES, "params": {"rootName": "LIF"}},
    "PescRoot": {"entities": _PESC_ENTITIES, "properties": _PESC_PROPERTIES, "params": {"rootName": "PESC"}},
    "CaseRoot": {"entities": _CASE_ENTITIES, "properties": _CASE_PROPERTIES, "params": {"rootName": "CASE"}},
    "CedsOntology": {"entities": _CEDS_ENTITIES, "properties": _CEDS_PROPERTIES, "params": {"rootName": "CEDS"}},
    "EdfiRoot": {"entities": _EDFI_ENTITIES, "properties": _EDFI_PROPERTIES, "params": {"rootName": "Ed-Fi"}},
    "CtdlRoot": {"entities": _CTDL_ENTITIES, "properties": _CTDL_PROPERTIES, "params": {"rootName": "CTDL"}},
    "ClrRoot": {"entities": _CLR_ENTITIES, "properties": _CLR_PROPERTIES, "params": {"rootName": "CLR"}},
    "EduApiRoot": {"entities": _EDUAPI_ENTITIES, "properties": _EDUAPI_PROPERTIES, "params": {"rootName": "Edu-API"}},
    "OpenBadgesRoot": {
        "entities": _OPENBADGES_ENTITIES,
        "properties": _OPENBADGES_PROPERTIES,
        "params": {"rootName": "Open Badges"},
    },
    "JedxRoot": {"entities": _JEDX_ENTITIES, "properties": _JEDX_PROPERTIES, "params": {"rootName": "JEDx"}},
    "SifRoot": {"entities": _SIF_ENTITIES, "properties": _SIF_PROPERTIES, "params": {"rootName": "SIF"}},
    "DctapRoot": {"entities": _DCTAP_ENTITIES, "properties": _DCTAP_PROPERTIES, "params": {"rootName": "DCTAP"}},
    "MedBiqModel": {
        "entities": _MEDBIQ_ENTITIES,
        "properties": _MEDBIQ_PROPERTIES,
        "params": {"rootName": "MedBiquitous"},
    },
}


SUPPORTED_KEYS: list[str] = list(_LOADERS.keys())


def get_load_cypher(key: str) -> dict:
    """Return the queries needed to load one standard's structure.

    Args:
        key: A standard key as returned by ``LIST_STANDARDS_CYPHER`` (the root
            label, e.g. ``"LifRoot"``, ``"PescRoot"``, ``"CedsOntology"``).

    Returns:
        A dict with keys:
            - ``"entities"``:   Cypher returning rows ``entityName, entityDescription``
            - ``"properties"``: Cypher returning rows
              ``entityName, propName, dataType, required, isRef``
            - ``"params"``:     dict of named params consumed by BOTH queries:
              ``{"rootName": ..., "skip": 0, "limit": 100}``. ``rootName`` is the
              standard's root name; ``skip``/``limit`` drive pagination. The
              paged runner overrides ``skip`` (and may override ``limit``) per
              page; pass these straight through as the parameter map otherwise.

    Raises:
        KeyError: if ``key`` is not a supported standard.

    The entities/properties Cypher use ``$``-named params (from ``params``),
    never string interpolation.
    """
    if key not in _LOADERS:
        raise KeyError(key)
    loader = _LOADERS[key]
    # Return shallow copies so callers can't mutate the module-level templates.
    # Merge in the pagination defaults (skip/limit) so the entities/properties
    # Cypher always have those params bound. The runner overrides ``skip`` (and
    # may override ``limit``) per page; an explicit per-loader value still wins.
    params = {**_PAGE_DEFAULTS, **loader["params"]}
    return {"entities": loader["entities"], "properties": loader["properties"], "params": params}


# ---------------------------------------------------------------------------
# Cross-standard MAPS_TO mappings.
# ---------------------------------------------------------------------------
#
# Every node carries a framework label: the ``<Std>Model`` label for most
# standards, or the bare ``CEDS`` label for CEDS. MAPS_TO is EDUcore's
# authoritative crosswalk (confidence 1.0); IMPLIED_MAPPING (inferred) is NOT
# included here. Keyed by the same standard key as ``LIST_STANDARDS_CYPHER``.
GRAPH_FRAMEWORK_LABEL_BY_KEY: dict[str, str] = {
    "LifRoot": "LifModel",
    "PescRoot": "PescModel",
    "CaseRoot": "CaseModel",
    "CedsOntology": "CEDS",
    "EdfiRoot": "EdfiModel",
    "CtdlRoot": "CtdlModel",
    "ClrRoot": "ClrModel",
    "EduApiRoot": "EduApiModel",
    "OpenBadgesRoot": "OpenBadgesModel",
    "JedxRoot": "JedxModel",
    "SifRoot": "SifModel",
    "DctapRoot": "DctapModel",
    "MedBiqModel": "MedBiqModel",
    "CipRoot": "CipModel",
    "SedmRoot": "SedmModel",
    "SocRoot": "SocModel",
}

# Allowlist of valid framework labels — guards the f-string label injection in
# ``maps_to_field_cypher`` (labels can't be parameterized in Cypher).
_FRAMEWORK_LABELS: frozenset[str] = frozenset(GRAPH_FRAMEWORK_LABEL_BY_KEY.values())

_FIELD_LABEL_PREDICATE = (
    "any(l IN labels({n}) WHERE l ENDS WITH 'Property' OR l ENDS WITH 'Field' "
    "OR l ENDS WITH 'Element' OR l ENDS WITH 'Attribute')"
)


def maps_to_field_cypher(source_label: str, target_label: str) -> str:
    """Build a paginatable query for field-level MAPS_TO edges between two frameworks.

    Returns rows ``sourceField, sourceEntities, targetField, targetEntities``
    (the *Entities lists are the owning entity/type names, since a field title
    like "id" repeats and the owner disambiguates it). Field-level only
    (Property/Field/Element/Attribute on both ends); class/concept mappings are
    excluded because they have no attribute to wire in the MDR.

    ``source_label``/``target_label`` MUST be framework labels from
    ``GRAPH_FRAMEWORK_LABEL_BY_KEY`` (validated against ``_FRAMEWORK_LABELS``);
    they are injected as Cypher labels, which cannot be parameterized.
    """
    if source_label not in _FRAMEWORK_LABELS or target_label not in _FRAMEWORK_LABELS:
        raise KeyError(f"Unknown framework label(s): {source_label!r} -> {target_label!r}")
    return f"""
MATCH (s:{source_label})-[:MAPS_TO]->(t:{target_label})
WHERE {_FIELD_LABEL_PREDICATE.format(n="s")}
  AND {_FIELD_LABEL_PREDICATE.format(n="t")}
OPTIONAL MATCH (sp)-[:HAS_PROPERTY|HAS_FIELD|HAS_ELEMENT|HAS_ATTRIBUTE]->(s)
OPTIONAL MATCH (tp)-[:HAS_PROPERTY|HAS_FIELD|HAS_ELEMENT|HAS_ATTRIBUTE]->(t)
WITH s, t, collect(DISTINCT sp.name) AS sEntities, collect(DISTINCT tp.name) AS tEntities
RETURN s.name AS sourceField, sEntities AS sourceEntities,
       t.name AS targetField, tEntities AS targetEntities
ORDER BY s._id, t._id
SKIP toInteger($skip) LIMIT toInteger($limit)
""".strip()


# Distinct directed framework pairs that have a field-level MAPS_TO crosswalk,
# with edge counts. One row per (sourceLabel, targetLabel) — ~33 rows, well under
# the ~100-row cap, so a single (non-paginated) call returns them all. The
# endpoint maps the framework labels back to standard keys / imported DataModels.
MAPS_TO_PAIRS_CYPHER: str = f"""
MATCH (s)-[:MAPS_TO]->(t)
WHERE {_FIELD_LABEL_PREDICATE.format(n="s")}
  AND {_FIELD_LABEL_PREDICATE.format(n="t")}
WITH coalesce([l IN labels(s) WHERE l ENDS WITH 'Model'][0],
              CASE WHEN 'CEDS' IN labels(s) THEN 'CEDS' ELSE 'UNKNOWN' END) AS sourceLabel,
     coalesce([l IN labels(t) WHERE l ENDS WITH 'Model'][0],
              CASE WHEN 'CEDS' IN labels(t) THEN 'CEDS' ELSE 'UNKNOWN' END) AS targetLabel
RETURN sourceLabel, targetLabel, count(*) AS mappingCount
ORDER BY mappingCount DESC
""".strip()
