# EDUcore → LIF MDR Integration Spec

Data-access + ingestion spec for loading education-standard data models from the
**EDUcore knowledge graph** (remote MCP server `https://educore.dev/mcp`, a
Neo4j-style property graph) into the **LIF MDR** app so they appear in the
mapping UI.

This document is the contract for the backend agent. The verified Cypher lives
in `bases/lif/mdr_restapi/educore_queries.py`. This agent does NOT own the
FastAPI endpoint or the frontend.

> EDUcore gotcha: every node also carries the super-label `ForgedNode`. Never
> filter with `NOT x:ForgedNode` — it removes everything.

---

## 1. Standards found (live `LIST_STANDARDS_CYPHER` run)

`LIST_STANDARDS_CYPHER` returns **all** standard model-roots dynamically (it does
not hardcode a list). It excludes `DmeSchemaRoot` (graph infrastructure). The
`key` is the standard-specific root label and is what `get_load_cypher(key)`
dispatches on. 15 roots returned, all keys distinct:

| key | title | version |
|-----|-------|---------|
| `CaseRoot` | Competencies and Academic Standards Exchange (CASE) Service OpenAPI (YAML) Definition | 1.1 |
| `CedsOntology` | CEDS | 14.0.0.0 |
| `CipRoot` | CIP 2020 | 2020 |
| `ClrRoot` | OpenAPI schema for Comprehensive Learner Record Standard | 2.0 |
| `CtdlRoot` | CTDL | Release 20260327 |
| `DctapRoot` | DCTAP | Draft - Request for Comments |
| `EdfiRoot` | Ed-Fi | (null) |
| `EduApiRoot` | OpenAPI schema for Edu-API | 1.0 |
| `JedxRoot` | JEDx | (null) |
| `LifRoot` | Machine-Readable Schema for LIF | 2.0 |
| `OpenBadgesRoot` | OpenAPI schema for Open Badges | 3.0 |
| `PescRoot` | PESC | (null) |
| `SedmRoot` | SEDM | 1.0 |
| `SifRoot` | SIF | (null) |
| `SocRoot` | Standard Occupational Classification / O*NET | (null) |

`SUPPORTED_KEYS` (the keys `get_load_cypher` handles today): `LifRoot`,
`PescRoot`, `CaseRoot`, `CedsOntology`, `EdfiRoot`, `CtdlRoot`. The other roots
appear in the list (so the UI can show them) but calling `get_load_cypher` on
them raises `KeyError` until a loader is added.

---

## 2. Per-standard structure (verified live)

The loaders normalize every standard to the same row columns regardless of the
native graph shape:
- entities cypher → `entityName`, `entityDescription`
- properties cypher → `entityName`, `propName`, `dataType`, `required` (bool),
  `isRef` (bool)

`dataType` is the standard's **native** type string. `required`/`isRef` are
always booleans (nulls coalesced to `false`).

### LIF (`LifRoot`) — must-have, verified thoroughly
- Graph shape: `LifRoot -[:HAS_ENTITY]-> LifEntity -[:HAS_PROPERTY]-> LifProperty`.
  The Person model (and several others) instead expose sub-structure as
  `LifEntity -[:HAS_COMPOSITE]-> LifComposite -[:HAS_PROPERTY]-> LifProperty`.
  The loader surfaces **both** the 7 direct entities (Course, Program, Position,
  Credential, CompetencyFramework, Assessment, Organization) **and** the 33
  composites (Name, Contact, Identifier, CredentialAward, Proficiency,
  Demographics, …) as entities → **40 LIF entities** total.
- `LifProperty` carries clean `dataType` / `required` / `isRef` fields.
- These composites ARE the LIF PascalCase child-entities; their props are the
  camelCase scalars — matches the repo capitalization convention exactly.
- Example entity: `Name` (PascalCase).
- Example props of `Name`: `firstName` (`string`, required=true, isRef=false),
  `lastName` (`string`). Example ref prop on `Course`:
  `accreditedByRefOrganization` (`object`, isRef=true).

### PESC (`PescRoot`) — must-have, verified thoroughly
- Graph shape: `PescRoot -[:HAS_SOURCE_FILE]-> PescSourceFile -[:DEFINES_TYPE]->
  PescComplexType -[:HAS_ELEMENT]-> PescElement`. (Complex types have NO direct
  edge from the root.)
- **256 distinct complex types** (600 nodes; names repeat across source files —
  the entities query dedupes by name).
- `PescElement` has no native dataType/required/isRef, so they are derived:
  `dataType = typeName`; `required = (minOccurs <> "0")` (occurs are strings);
  `isRef = element -[:HAS_TYPE]-> PescComplexType` (object reference).
- Example entity: `AcademicAwardType` ("Academic awards, degrees, diplomas…").
- Example props: `AcademicAwardDate` (`core:AcademicAwardDateType`, isRef=false),
  `AcademicAwardProgram` (`AcRec:AcademicProgramType`, isRef=true).

### CASE (`CaseRoot`) — must-have, verified thoroughly
- Graph shape: `CaseRoot -[:HAS_CLASS]-> CaseClass -[:HAS_PROPERTY]-> CaseProperty`.
- **36 classes**.
- `CaseProperty` has native `dataType` + real boolean `required`. `isRef` derived
  from `refCount > 0`. dataType values: `string`, `reference`, `array`,
  `integer`, `number`, `anyOf`.
- Example entity: `CFAssociation`.
- Example props: `destinationNodeURI` (`reference`, required=true, isRef=true),
  `identifier` (`string`, required=true, isRef=false).

### CEDS (`CedsOntology`)
- Root has NO outgoing edges; `CedsClass` matched by label.
  `CedsClass -[:HAS_PROPERTY]-> CedsProperty`.
- **402 classes** (all distinct names).
- `CedsProperty` has `dataType` on ~42% of nodes; NO `required`, no ref concept
  (class relations are `SUBCLASS_OF`). So `required=false`, `isRef=false` always.
- Example entity: `Accessibility Feature`.
- Example prop: `Assessment Extended Time Duration` (`nonNegativeInteger`).
- Note: CEDS class/property names contain spaces (e.g. `Accessibility Feature`).
  See §3 on PascalCase normalization for MDR `UniqueName`.

### Ed-Fi (`EdfiRoot`)
- Graph shape: `EdfiRoot -[:HAS_ENTITY]-> EdfiEntity -[:HAS_FIELD]-> EdfiField`.
- **201 entities**.
- `EdfiField`: `elementType` is the type (`String`/`Date`/`Number`/`Boolean`/
  `Descriptor`/`Time`/`DateTime`), `required` is a real boolean. `isRef` derived
  as `elementType == "Descriptor"` (references a controlled vocabulary).
- Example prop: `AcademicHonor.AcademicHonorCategoryDescriptor`
  (`Descriptor`, isRef=true); `AcademicWeek.BeginDate` (`Date`, required=true).

### CTDL (`CtdlRoot`)
- Graph shape: `CtdlRoot -[:HAS_CLASS]-> CtdlClass -[:HAS_PROPERTY]-> CtdlProperty`.
  RDF-style — properties are shared across class domains (7210 class-property
  pairs over **138 classes**).
- `CtdlProperty` has `rangeTypes` (a LIST). `dataType = rangeTypes[0]`;
  `required=false`; `isRef = rangeTypes[0]` is NOT an `xsd:`/`rdf:` literal (so it
  points at a `ceterms:`/`ceasn:`/`skos:` class).
- Example entity: `Academic Certificate`.
- Example props: `Accredited By` (`ceterms:Organization`, isRef=true),
  `Alternate Name` (`rdf:langString`, isRef=false).

---

## 3. MDR ingestion shape (CRITICAL)

### Recommended route: in-memory OpenAPI-schema ingestion

Use **`schema_upload_service.create_data_model_from_openapi_schema(...)`**
(`components/lif/mdr_services/schema_upload_service.py:525`). It accepts an
**in-memory `openapi_schema: Dict`** (not just a file), creates the `DataModel`
+ all `Entity` + `Attribute` + their associations in one call, flushes/commits,
and returns a `DataModelDTO`. This is the cleanest single-call path.

> The FastAPI wrapper `POST /datamodels/open_api_schema/upload`
> (`bases/lif/mdr_restapi/datamodel_endpoints.py:185`) only exists to accept a
> multipart file upload; it then `json.loads` it and calls the same service at
> line 221. **Backend code should call the service directly with a Python dict**
> and skip the multipart round-trip. (The alternate `POST /import/` →
> `import_export_service.import_datamodel` at
> `components/lif/mdr_services/import_export_service.py:184` expects a full
> internal export DTO with pre-assigned Ids — heavier and not recommended here.)

### Function signature to call

```python
from lif.mdr_services import schema_upload_service

dto = await schema_upload_service.create_data_model_from_openapi_schema(
    session=session,                          # AsyncSession
    openapi_schema=openapi_schema_dict,       # the dict described below
    data_model_name="EDUcore: PESC",          # unique per (name, version, org)
    data_model_version="1.0",                 # standard's version or a slug
    data_model_type="SourceSchema",           # see note below
    data_model_description="Imported from EDUcore knowledge graph",
    base_data_model_id=None,                   # None for an independent standard
    use_considerations=None,
    notes=None,
    activation_date=None,
    deprecation_date=None,
    contributor="EDUcore Importer",
    contributor_organization="EDUcore",
    state="Draft",
    tags="educore,imported",
)
```

`data_model_type` must be a `DataModelType` enum value
(`mdr_sql_model.py:8`): `BaseLIF`, `OrgLIF`, `SourceSchema`, `PartnerLIF`.
**Use `SourceSchema`** for imported external standards. This matters: the upload
parser branches on type — only `OrgLIF`/`PartnerLIF` create `ExtInclusions` and
dedupe by pre-assigned `Id`; every other type (incl. `SourceSchema`) dedupes by
`UniqueName` + `DataModelId` and does NOT require Ids. So you can omit all
`Id`/`ValueSetId`-int fields.

Uniqueness guard: the service raises `ValueError` if a non-deleted `DataModel`
with the same `(Name, DataModelVersion, ContributorOrganization)` already
exists. Make the name/version unique per import (e.g. include the EDUcore key).

### The `openapi_schema` dict shape the parser expects (NON-STANDARD)

The parser walks `openapi_schema["components"]["schemas"]` — a dict of
`{schemaName: schemaObject}` (`schema_upload_service.py:578-592`). For each
schema and, recursively, each item under a schema's `properties`
(`create_entity_and_children_if_needed`, line 451), it decides entity vs
attribute with this **inverted, MDR-specific rule**:

- A property is treated as an **Attribute (scalar)** **iff it contains a
  `"ValueSetId"` key** (line 462 / 507). The value may be `null`.
- A property **without** a `"ValueSetId"` key is treated as a **child Entity**
  and recursed (its own `properties` are walked).
- A property whose value contains `"$ref"` is skipped on the first pass and
  wired up as an `EntityAssociation` (Placement `"Reference"`) in a second pass
  (`create_reference_associations_for_children`, line 71).

So you control the entity/attribute split with the presence of `ValueSetId`, not
with OpenAPI `type`. Field names the parser reads (PascalCase, from the SQL
models):

Entity object (`create_entity_if_needed`, line 374): `Name`, `UniqueName`,
`Description`, `Required` ("Yes"/"No"), `Array` ("Yes"/"No"), `properties`.

Attribute object (`create_attribute_if_needed`, line 196): `Name`, `UniqueName`,
`Description`, `DataType` (string), `Required` ("Yes"/"No"), `Array`
("Yes"/"No"), and the marker key `ValueSetId` (use `null`). Optionally `ValueSet`
+ `Values` for enums.

> Important: `Required` and `Array` are STRINGS `"Yes"`/`"No"` on both Entity and
> Attribute (`mdr_sql_model.py:103-104, 208-209`), not JSON booleans. Defaults if
> omitted: `Required="No"`, `Array="Yes"`.

### Concrete builder shape (one standard → one DataModel)

For each EDUcore `entityName` build one top-level schema; for each property of
that entity, emit a child object carrying `ValueSetId: null` (the attribute
marker). Use the entity name as the property key under `properties`.

```python
schemas = {}
for entity_name, entity_desc in entity_rows:           # from `entities` cypher
    schemas[to_pascal(entity_name)] = {
        "Name": to_pascal(entity_name),
        "UniqueName": to_pascal(entity_name),          # unique within the DM
        "Description": entity_desc or None,
        "Required": "No",
        "Array": "Yes",                                # entities are arrays in LIF
        "properties": {},
    }

for r in property_rows:                                # from `properties` cypher
    ent = schemas.get(to_pascal(r["entityName"]))
    if ent is None:                                    # property w/o a listed entity
        continue
    mdr_dt, is_array = map_datatype(r["dataType"], r["isRef"])
    ent["properties"][to_camel(r["propName"])] = {
        "Name": to_camel(r["propName"]),
        "UniqueName": f'{ent["UniqueName"]}.{to_camel(r["propName"])}',
        "DataType": mdr_dt,
        "Required": "Yes" if r["required"] else "No",
        "Array": "Yes" if is_array else "No",
        "ValueSetId": None,                            # <-- marks this as an Attribute
    }

openapi_schema = {
    "openapi": "3.0.0",
    "info": {"title": title, "version": version, "description": "Imported from EDUcore"},
    "paths": {},
    "components": {"schemas": schemas},
}
```

Helpers (`to_pascal`/`to_camel`) normalize names to the LIF convention. CEDS/CTDL
names contain spaces (`"Academic Certificate"`, `"Accredited By"`) — strip
spaces/punctuation: entity/object names → PascalCase, scalar attribute names →
camelCase. Make `UniqueName` collision-proof within a DataModel (qualifying the
attribute with its entity name, as above, is sufficient and the parser dedupes
on `UniqueName`).

> Keep it flat for v1: emit every EDUcore property as an MDR Attribute
> (`ValueSetId: null`). This avoids the recursive child-entity branch and the
> two-pass `$ref` machinery entirely, and reliably populates the mapping UI. See
> §4 for the (optional) richer treatment of `isRef` as child entities.

---

## 4. Type mapping: EDUcore `dataType` → MDR `DataType` + `Array`

`MDR Attribute.DataType` is a free-form string and `Array` is `"Yes"`/`"No"`.
EDUcore native types vary per standard. Recommended normalization:

| EDUcore `dataType` (examples) | MDR `DataType` | `Array` | Notes |
|---|---|---|---|
| `string`, `xsd:string`, `rdf:langString`, `String` | `string` | `No` | |
| `array` | (element type if known, else `string`) | `Yes` | LIF marks arrays via `Array=Yes`, not a "array" DataType |
| `object` (LIF ref props) | `string` | `No` | usually `isRef=true`; see ref handling below |
| `boolean`, `xsd:boolean`, `Boolean` | `boolean` | `No` | |
| `number`, `integer`, `xsd:float`, `xsd:date`, `nonNegativeInteger`, `Number`, `Date`, `Time`, `DateTime` | pass through native string | `No` | MDR DataType is free-form; preserve the native type so mapping UI shows it |
| `reference` (CASE), `Descriptor` (Ed-Fi), `ceterms:*` (CTDL), `core:*Type` (PESC) | native string | per source | these are the `isRef=true` cases |

Practical `map_datatype(data_type, is_ref)`:
- If `data_type == "array"` → `("string", True)` (or carry a known element type).
- Else `is_array = False`, `DataType = data_type` (preserve native string).
- The LIF native types are simple (`string`/`array`/`object`/`number`); for the
  other standards, preserving the native type string is the most faithful and
  keeps the mapping UI informative.

### Handling `isRef` properties

Two valid options:

1. **Simple (recommended for v1):** represent the ref as a plain Attribute
   (`ValueSetId: null`, `DataType` = the native ref type string, `Array` per the
   source). The mapping UI still shows the field; no entity graph needed.

2. **Faithful (object ref → child/related entity):** to model a reference as an
   inter-entity link, emit it as a property whose value is `{"$ref":
   "#/components/schemas/<TargetEntity>"}` and ensure `<TargetEntity>` is also a
   top-level schema. The parser's second pass
   (`create_reference_associations_for_children`,
   `schema_upload_service.py:71`) creates an `EntityAssociation` with
   `Placement="Reference"`. The `relationship` is derived by stripping the target
   entity name off the property name (line 48), so the property key should END
   with the target entity name (e.g. LIF's `accreditedByRefOrganization` →
   target `Organization`). This is fiddly across non-LIF standards (ref targets
   are type strings, not guaranteed to be top-level entities), so prefer option 1
   unless an entity-relationship view is specifically required.

---

## 5. Quick reference — public interface in `educore_queries.py`

```python
LIST_STANDARDS_CYPHER: str               # rows: key, title, version, description
SUPPORTED_KEYS: list[str]                 # ["LifRoot","PescRoot","CaseRoot","CedsOntology","EdfiRoot","CtdlRoot"]
def get_load_cypher(key: str) -> dict     # {"entities": cypher, "properties": cypher, "params": {...}}; KeyError if unsupported
```

`params` carries `{"rootName": ...}` (e.g. `"LIF"`, `"PESC"`, `"CASE"`,
`"Ed-Fi"`, `"CTDL"`; `"CEDS"` is unused since CEDS matches by label). Pass
`params` straight through as the parameter map for BOTH the `entities` and
`properties` Cypher. The queries use `$`-named params only — never interpolate.
```
