# `mdr_restapi` — Base

FastAPI base for the LIF **Metadata Repository (MDR)**: the control-plane service that holds the LIF schema(s), transformation definitions, value sets, and per-tenant configuration. Most LIF services load their schema and transformation rules from here at startup.

The base is split into many endpoint modules (one per concern) which `core.py` mounts under stable URL prefixes.

## Endpoint groups

| Prefix | Module | What it does |
|---|---|---|
| `/datamodels` | `datamodel_endpoints` | LIF data models — Base LIF, Org LIF, target transformation models |
| `/entities` | `entity_endpoints` | Entity definitions within a data model |
| `/entity_associations` | `entity_association_endpoints` | Entity-to-entity relationships |
| `/attributes` | `attribute_endpoints` | Scalar attributes within entities |
| `/entity_attribute_associations` | `entity_attribute_association_endpoints` | Which attributes belong to which entities |
| `/inclusions` | `inclusions_endpoints` | Reusable attribute groups (e.g., Contact, Address) |
| `/value_sets` + `/value_set_values` | `valueset_endpoint`, `value_set_values_endpoint` | Strict + extensible enumerations |
| `/transformation_groups` | `transformation_endpoint` | JSONata-based source→target transformations |
| `/value_mappings` | `value_mapping_endpoints` | Code/value crosswalks used during transformation |
| `/search` | `search_endpoint` | MDR-wide full-text search |
| `/datamodel_constraints` | `datamodel_constraints_endpoints` | Constraint rules per model |
| `/import_export` | `import_export_endpoints` | Bulk import/export of MDR content |
| `/generate_jinja` | `generate_jinja_endpoint` | Template generation for derived schemas |
| `/tenants` | `tenant_endpoints` | Self-serve tenant lifecycle (#883/#884): provision, workspace listing/selection, invite tokens |
| `/exchange` | `exchange_endpoints` | Pull-based schema exchange with a peer MDR (see below) |

## Schema exchange (`/exchange`)

A publishing MDR exposes its Published content; a peer MDR pulls a bundle and POSTs it to its own `/exchange/receive`. No partner URLs or credentials are stored (ADR 0002) — the publisher only issues a read-only key (`MDR__AUTH__SERVICE_API_KEY__EXCHANGE_PARTNER`), which `AuthMiddleware` confines to `GET /exchange/*`. `MDR__EXCHANGE__PUBLISHER_NAME` names the publisher in the catalog and bundle manifests.

| Endpoint | What it does |
|---|---|
| `GET /exchange/catalog` | Published data models + transformation groups whose source and target are both Published |
| `GET /exchange/bundles/data-models/{id}?public_only=false` | Data-model bundle (404 missing/deleted, 409 not Published) |
| `GET /exchange/bundles/transformation-groups/{id}` | Transformation-group bundle incl. source/target models (400 if no exportable transformations) |
| `POST /exchange/receive?data_model_type=SourceSchema&contributor_organization=&allowMissingPaths=true` | Apply a bundle of either kind in one transaction; verifies the manifest checksum/formatVersion |

Bundle shape and checksum rules: `components/lif/mdr_dto/exchange_dto.py`.

## Auth
`AuthMiddleware` (from `mdr_auth/core`) supports three principals: API-key (services), Cognito JWT (end users), and legacy HS256 JWT (pre-Cognito callers). The middleware also resolves `request.state.tenant_schema` per request based on Cognito groups + optional workspace-selection cookie — see [`docs/design/cross-cutting/self-serve-tenant-auth.md`](../../../docs/design/cross-cutting/self-serve-tenant-auth.md).

## Composes
- `datatypes` — common payload shapes
- `mdr_auth` — auth middleware + JWT/cookie/invite-token helpers
- `mdr_dto` — wire-format DTOs
- `mdr_services` — business logic (tenant_service, transformation_service, etc.)
- `mdr_utils` — config, DB session factory, logger

## Deployed as
`projects/lif_mdr_api/` (API) + `projects/lif_mdr_database/` (Postgres + Flyway migrations).
Frontend: `frontends/mdr-frontend/`.
