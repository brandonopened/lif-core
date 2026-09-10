# EDUcore crosswalks

Crosswalk files extracted from the EDUcore Education Standards Knowledge Graph
(the `educore-standards` MCP connector). They are the input to the
`crosswalk_draft` brick (`components/lif/crosswalk_draft/`), which turns them
into a Draft transformation-group bundle that a human then finishes in the MDR
mapping editor. See `docs/design/cross-cutting/schema-exchange.md`.

These files are **snapshots**, not a mirror of anything running in MDR. Re-extract
with the Cypher below when the graph changes and bump `meta.extractedAt`.

## How EDUcore expresses equivalence

Cross-standard meaning in EDUcore is anchored on CEDS. An element of any standard
points at a CEDS *hub* (`HubReference`) with either an authored `EXACT_MATCH` or
an inferred `CLOSE_MATCH` (scored 0-1). Two elements are equivalent when they
share a hub; the pair is only *candidate* quality unless both hops are
`EXACT_MATCH`.

What the graph held on 2026-09-03 for the standards this repo cares about:

| Standard | Property-level hub matches | Option-value hub matches |
|----------|----------------------------|--------------------------|
| Ed-Fi    | 918 `EXACT_MATCH`          | 4,341 `EXACT_MATCH`      |
| PESC     | 586 `CLOSE_MATCH`          | 0                        |
| LIF 2.0  | **0**                      | 688 `CLOSE_MATCH`        |

Consequence: EDUcore cannot yet propose **property** mappings to or from LIF. It
can propose **value** (enumeration) mappings into LIF, and property mappings
between Ed-Fi and PESC. LIF-to-LIF drafts therefore rely on the identity matching
in `crosswalk_draft`; EDUcore contributes the value lookups.

## Files

### `edfi-to-lif.json`

162 rows: every LIF 2.0 option value that shares a CEDS value hub with an Ed-Fi
descriptor value. 7 LIF option sets, 6 Ed-Fi descriptors. `confidence` is the
LIF-side `CLOSE_MATCH` score (the Ed-Fi side is always exact).

```cypher
MATCH (os:ForgedNode {role:'DmeOptionSet', _source:'LIF'})-[:HAS_VALUE]->
      (v:ForgedNode {role:'DmeOptionValue'})-[m1:CLOSE_MATCH]->(hub:HubReference)
      <-[m2:EXACT_MATCH]-(ev:ForgedNode {role:'DmeOptionValue', _source:'EdFi'})
      <-[:HAS_VALUE]-(eos:ForgedNode {role:'DmeOptionSet'})
RETURN eos.name AS sourceOptionSet, ev.name AS sourceValue,
       os.name  AS targetOptionSet, v.name  AS targetValue,
       round(m1.confidence, 3) AS confidence,
       hub.canonicalKey AS cedsKey, hub.name AS cedsName
ORDER BY targetOptionSet, targetValue, sourceOptionSet
```

The file shape is the `crosswalk` input documented in
`components/lif/crosswalk_draft/README.md` (`meta`, `properties`, `values`).

`properties` (68 rows) is **inferred, not authored in EDUcore**: an Ed-Fi property and a LIF
property are proposed as equivalent when their option sets share CEDS value hubs
(`matchType: VALUE_SET_EQUIVALENCE`, `confidence` = mean value-row confidence for the set
pair, `cedsKey` empty). The owners of each option set came from:

```cypher
MATCH (os:ForgedNode {role:'DmeOptionSet', _source:'LIF'})-[:HAS_VALUE]->(v)-[:CLOSE_MATCH]->(hub:HubReference)
      <-[:EXACT_MATCH]-(ev)<-[:HAS_VALUE]-(eos:ForgedNode {role:'DmeOptionSet', _source:'EdFi'})
WITH DISTINCT os, eos
MATCH (lp:ForgedNode {role:'DmeProperty', _source:'LIF'})-[:HAS_OPTION_SET]->(os)
MATCH (ep:ForgedNode {role:'DmeProperty', _source:'EdFi'})-[:HAS_OPTION_SET]->(eos)
RETURN eos.name AS edfiSet, collect(DISTINCT ep.path) AS edfiProps,
       os.name AS lifSet, collect(DISTINCT lp.path) AS lifProps
```

Ed-Fi paths in the graph end in `Descriptor` (`Address.StateAbbreviationDescriptor`); the MDR's
Ed-Fi v5 model names the same attribute `Address.StateAbbreviation` and gives it no enum. The
`crosswalk_draft` brick tolerates that (stem matching on enum-less sources).

## Not committed, but available from the graph

Ed-Fi to PESC property crosswalk (1,214 candidate pairs across 322 Ed-Fi and 294
PESC properties on 94 hubs). Relevant once a community college wants PESC output
instead of LIF JSON. Extract with:

```cypher
MATCH (a:ForgedNode {role:'DmeProperty', _source:'EdFi'})-[m1:EXACT_MATCH]->(hub:HubReference)
      <-[m2:CLOSE_MATCH]-(b:ForgedNode {role:'DmeProperty', _source:'PESC'})
RETURN a.path AS sourcePath, b.path AS targetPath, round(m2.confidence, 3) AS confidence,
       'EXACT_MATCH/CLOSE_MATCH' AS matchType, hub.canonicalKey AS cedsKey, hub.name AS cedsName
ORDER BY confidence DESC, sourcePath, targetPath
```

Note that `_source` values in the graph are `EdFi` and `PESC` (not `Ed-Fi`).
