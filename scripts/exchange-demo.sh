#!/usr/bin/env bash
set -euo pipefail
#
# exchange-demo.sh — Drive the district <-> community-college schema-exchange demo
# against the local compose stack (docker-compose.yml + docker-compose.exchange.yml).
#
# A sender MDR publishes its LIF data model; the receiver pulls the bundle with a
# read-only partner key, receives it into its own MDR, generates a Draft
# transformation group with the crosswalk_draft CLI, receives that too, and
# translates a sample record through its Translator. No partner URL or credential
# is stored in either MDR (ADR metadata_repository/0002); this script is the "pull".
# Design: docs/design/cross-cutting/schema-exchange.md
#
# Usage:
#   EXCHANGE_PARTNER_KEY=<key> ./scripts/exchange-demo.sh [--reverse] [--dry-run] [--publish]
#
# Flags:
#   --reverse   After the district pulls from the college, also run the flow the
#               other way (college pulls the district's model, drafts, translates).
#   --dry-run   Print the curl commands instead of executing them (ids become
#               <placeholders>).
#   --publish   Accepted for symmetry with the MDR UI; sets nothing else. A human
#               refines the Draft group in the mapping editor afterwards.
#
# Environment (all optional except EXCHANGE_PARTNER_KEY):
#   DISTRICT_MDR_URL         default http://localhost:8012
#   COLLEGE_MDR_URL          default http://localhost:8022
#   DISTRICT_TRANSLATOR_URL  default http://localhost:8007
#   COLLEGE_TRANSLATOR_URL   default http://localhost:8017
#   MDR_ADMIN_KEY            full-privilege MDR service key on BOTH MDRs (default changeme1,
#                            the compose default for MDR__AUTH__SERVICE_API_KEY__GRAPHQL)
#   EXCHANGE_PARTNER_KEY     the read-only key both MDRs register as
#                            MDR__AUTH__SERVICE_API_KEY__EXCHANGE_PARTNER (required)
#   WORKDIR                  where bundles/reports are written (default ./.exchange-demo)
#   SAMPLE_RECORD            LIF record to translate (default: the org1 "State University"
#                            sample projects/mongodb/sample_data/advisor-demo-org1/Matt-validated.json)
#   CROSSWALK                EDUcore crosswalk passed to the CLI when the file exists
#                            (default reference_data/crosswalks/educore/edfi-to-lif.json)
#
# Requires: curl (>= 7.76 for --fail-with-body), jq, uv (for the crosswalk CLI).
#

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

DISTRICT_MDR_URL="${DISTRICT_MDR_URL:-http://localhost:8012}"
COLLEGE_MDR_URL="${COLLEGE_MDR_URL:-http://localhost:8022}"
DISTRICT_TRANSLATOR_URL="${DISTRICT_TRANSLATOR_URL:-http://localhost:8007}"
COLLEGE_TRANSLATOR_URL="${COLLEGE_TRANSLATOR_URL:-http://localhost:8017}"
MDR_ADMIN_KEY="${MDR_ADMIN_KEY:-changeme1}"
EXCHANGE_PARTNER_KEY="${EXCHANGE_PARTNER_KEY:-}"
WORKDIR="${WORKDIR:-./.exchange-demo}"
SAMPLE_RECORD="${SAMPLE_RECORD:-${REPO_ROOT}/projects/mongodb/sample_data/advisor-demo-org1/Matt-validated.json}"
CROSSWALK="${CROSSWALK:-${REPO_ROOT}/reference_data/crosswalks/educore/edfi-to-lif.json}"

DISTRICT_NAME="Lakeside Unified School District"
COLLEGE_NAME="Riverbend Community College"
DISTRICT_MODEL="StateU LIF"          # seeded OrgLIF model (id 17 in projects/lif_mdr_database/backup.sql)
COLLEGE_MODEL="Riverbend CC LIF"     # the college's seeded StateU LIF copy, renamed on first run
MODEL_VERSION="1.0"
DRAFT_VERSION="0.1"
COLLEGE_ORG_KEY="Riverbend"          # ContributorOrganization stamped on the college's own model

DRY_RUN=false
REVERSE=false
STEP=0
DISTRICT_MODEL_ID=""
DISTRICT_ORG_KEY=""
COLLEGE_MODEL_ID=""

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

log_info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $1" >&2; }

banner() {
    STEP=$((STEP + 1))
    echo ""
    echo -e "${BOLD}==================================================================${NC}"
    echo -e "${BOLD}  Step ${STEP}: $1${NC}"
    echo -e "${BOLD}==================================================================${NC}"
}

usage() {
    # Print the header comment block above (everything up to the first non-comment line).
    sed -n '2,/^[^#]/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --reverse)  REVERSE=true; shift ;;
            --dry-run)  DRY_RUN=true; shift ;;
            --publish)  shift ;;   # no-op: humans publish the refined group from the MDR UI
            --help|-h)  usage; exit 0 ;;
            *) log_error "Unknown option: $1"; usage; exit 1 ;;
        esac
    done
}

check_dependencies() {
    local missing=()
    for tool in curl jq uv; do
        command -v "$tool" >/dev/null || missing+=("$tool")
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        log_error "Missing required tools: ${missing[*]}"
        exit 1
    fi
    if [[ -z "$EXCHANGE_PARTNER_KEY" && "$DRY_RUN" != "true" ]]; then
        log_error "EXCHANGE_PARTNER_KEY is required (the value of MDR__AUTH__SERVICE_API_KEY__EXCHANGE_PARTNER in .env)"
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# HTTP helpers. Every MDR call goes through `api` so --dry-run can print instead.
# ---------------------------------------------------------------------------

# api METHOD URL KEY [BODY_FILE]  -> response body on stdout (non-2xx: body + non-zero exit)
api() {
    local method=$1 url=$2 key=$3 body="${4:-}"
    local -a common=(curl -sS --fail-with-body -m 300 -X "$method" -H "Content-Type: application/json")
    [[ -n "$body" ]] && common+=(--data "@${body}")
    if [[ "$DRY_RUN" == "true" ]]; then
        echo "  \$ ${common[*]} -H 'X-API-Key: \$KEY' '${url}'" >&2
        echo '{}'
        return 0
    fi
    "${common[@]}" -H "X-API-Key: ${key}" "$url"
}

# pick JSON JQ_FILTER PLACEHOLDER -> value; "<PLACEHOLDER>" in dry-run; exits when missing otherwise
pick() {
    local json=$1 filter=$2 placeholder=$3 value
    value=$(jq -r "${filter} // empty" <<<"$json")
    if [[ -n "$value" ]]; then
        echo "$value"
    elif [[ "$DRY_RUN" == "true" ]]; then
        echo "<${placeholder}>"
    else
        log_error "Could not extract '${filter}' from response: ${json}"
        exit 1
    fi
}

url_encode() {
    jq -rn --arg v "$1" '$v|@uri'
}

wait_for() {
    local label=$1 url=$2 tries=60
    if [[ "$DRY_RUN" == "true" ]]; then
        echo "  \$ curl -sf ${url}"
        return 0
    fi
    until curl -sf -m 5 "$url" >/dev/null; do
        tries=$((tries - 1))
        if [[ $tries -le 0 ]]; then
            log_error "${label} did not become healthy at ${url}"
            exit 1
        fi
        sleep 2
    done
    log_success "${label} healthy (${url})"
}

# ---------------------------------------------------------------------------
# MDR helpers
# ---------------------------------------------------------------------------

# find_model_id MDR_URL NAME PLACEHOLDER -> id (exact-name match via GET /datamodels/?name=);
# empty when absent in real mode, "<PLACEHOLDER>" in dry-run.
find_model_id() {
    local mdr=$1 name=$2 placeholder=$3 resp id
    resp=$(api GET "${mdr}/datamodels/?name=$(url_encode "$name")&pagination=false" "$MDR_ADMIN_KEY")
    id=$(jq -r '.data[0].Id // empty' <<<"$resp")
    if [[ -z "$id" && "$DRY_RUN" == "true" && -n "$placeholder" ]]; then
        id="<${placeholder}>"
    fi
    echo "$id"
}

# publish_model MDR_URL ID [ORG_KEY]: PUT the identity fields back with State=Published.
# The identity fields are resent because update_datamodel's uniqueness check compares each of them.
publish_model() {
    local mdr=$1 id=$2 org="${3:-}" current body
    current=$(api GET "${mdr}/datamodels/${id}" "$MDR_ADMIN_KEY")
    body="${WORKDIR}/publish-${id//[<>]/}.json"
    jq --arg org "$org" \
       '{Name, DataModelVersion, Type, Description, State: "Published",
         ContributorOrganization: (if $org == "" then .ContributorOrganization else $org end)}' \
       <<<"$current" >"$body"
    api PUT "${mdr}/datamodels/${id}" "$MDR_ADMIN_KEY" "$body" >/dev/null
    log_success "Data model ${id} is Published on ${mdr}"
}

# org_key MDR_URL ID -> ContributorOrganization of a model (the receive-side tag, see run_exchange)
org_key() {
    local mdr=$1 id=$2 resp
    resp=$(api GET "${mdr}/datamodels/${id}" "$MDR_ADMIN_KEY")
    pick "$resp" '.ContributorOrganization' "org-key"
}

# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

step_wait() {
    banner "Wait for both MDRs (and both translators)"
    wait_for "District MDR" "${DISTRICT_MDR_URL}/health-check"
    wait_for "College MDR"  "${COLLEGE_MDR_URL}/health-check"
    wait_for "District Translator" "${DISTRICT_TRANSLATOR_URL}/health"
    wait_for "College Translator"  "${COLLEGE_TRANSLATOR_URL}/health"
}

step_prepare_models() {
    banner "Prepare + publish each side's LIF model"

    log_info "District: locate '${DISTRICT_MODEL}'"
    DISTRICT_MODEL_ID=$(find_model_id "$DISTRICT_MDR_URL" "$DISTRICT_MODEL" "district-model-id")
    if [[ -z "$DISTRICT_MODEL_ID" ]]; then
        log_error "'${DISTRICT_MODEL}' not found on ${DISTRICT_MDR_URL}"
        exit 1
    fi
    log_info "District '${DISTRICT_MODEL}' id=${DISTRICT_MODEL_ID}; ensuring it is Published"
    publish_model "$DISTRICT_MDR_URL" "$DISTRICT_MODEL_ID"
    DISTRICT_ORG_KEY=$(org_key "$DISTRICT_MDR_URL" "$DISTRICT_MODEL_ID")

    log_info "College: locate or adopt '${COLLEGE_MODEL}'"
    # No placeholder on purpose: a dry run should show the adopt commands.
    COLLEGE_MODEL_ID=$(find_model_id "$COLLEGE_MDR_URL" "$COLLEGE_MODEL" "")
    if [[ -z "$COLLEGE_MODEL_ID" ]]; then
        # Both containers start from the same seed, so the college's "own" LIF model is its seeded
        # copy of StateU LIF, renamed. (POST /import_export/clone/ currently 500s — see the gaps list
        # in docs/design/cross-cutting/schema-exchange.md — so the demo adopts rather than clones.)
        local seed_id current adopt_body
        seed_id=$(find_model_id "$COLLEGE_MDR_URL" "$DISTRICT_MODEL" "seed-stateu-id")
        if [[ -z "$seed_id" ]]; then
            log_error "Seed model '${DISTRICT_MODEL}' not found on ${COLLEGE_MDR_URL}"
            exit 1
        fi
        log_info "Adopting seed model ${seed_id} as '${COLLEGE_MODEL}' ${MODEL_VERSION} (org '${COLLEGE_ORG_KEY}') via PUT /datamodels/${seed_id}"
        current=$(api GET "${COLLEGE_MDR_URL}/datamodels/${seed_id}" "$MDR_ADMIN_KEY")
        adopt_body="${WORKDIR}/adopt-college.json"
        jq --arg name "$COLLEGE_MODEL" --arg ver "$MODEL_VERSION" --arg org "$COLLEGE_ORG_KEY"            '{Name: $name, DataModelVersion: $ver, Type, Description, ContributorOrganization: $org, State: "Published"}'            <<<"$current" >"$adopt_body"
        api PUT "${COLLEGE_MDR_URL}/datamodels/${seed_id}" "$MDR_ADMIN_KEY" "$adopt_body" >/dev/null
        COLLEGE_MODEL_ID="$seed_id"
    else
        log_info "'${COLLEGE_MODEL}' already exists (id=${COLLEGE_MODEL_ID})"
    fi
    publish_model "$COLLEGE_MDR_URL" "$COLLEGE_MODEL_ID" "$COLLEGE_ORG_KEY"
    log_success "District model id=${DISTRICT_MODEL_ID} (org key '${DISTRICT_ORG_KEY}'); college model id=${COLLEGE_MODEL_ID} (org key '${COLLEGE_ORG_KEY}')"
}

# run_exchange SENDER_LABEL SENDER_MDR SENDER_MODEL_ID SENDER_MODEL_NAME \
#              RECEIVER_LABEL RECEIVER_MDR RECEIVER_TRANSLATOR RECEIVER_MODEL_ID RECEIVER_MODEL_NAME \
#              RECEIVER_ORG_KEY RECEIVER_PUBLISHER TAG
# Steps 3-6 of the demo, parameterized so --reverse can swap the roles.
run_exchange() {
    local sender=$1 sender_mdr=$2 sender_model_id=$3 sender_model=$4
    local receiver=$5 receiver_mdr=$6 receiver_translator=$7 receiver_model_id=$8 receiver_model=$9
    local receiver_org_key=${10} receiver_publisher=${11} tag=${12}
    local catalog remote_id sender_bundle own_bundle draft_bundle report resp received_id group_id translated

    # --- pull -------------------------------------------------------------
    banner "${receiver} pulls the ${sender} catalog (partner key) and receives its data model"
    catalog=$(api GET "${sender_mdr}/exchange/catalog" "$EXCHANGE_PARTNER_KEY")
    jq '{publisher, dataModels: [.dataModels[]? | {id, name, version, type}],
         transformationGroups: [.transformationGroups[]? | {id, name, version}]}' <<<"$catalog"
    remote_id=$(pick "$catalog" ".dataModels[]? | select(.name == \"${sender_model}\") | .id" "${tag}-remote-model-id")
    if [[ "$DRY_RUN" != "true" && "$remote_id" != "$sender_model_id" ]]; then
        log_warn "Catalog id ${remote_id} differs from the id found by name (${sender_model_id}); using the catalog's"
    fi

    sender_bundle="${WORKDIR}/${tag}-received-datamodel.json"
    log_info "Downloading data-model bundle ${remote_id} -> ${sender_bundle}"
    api GET "${sender_mdr}/exchange/bundles/data-models/${remote_id}" "$EXCHANGE_PARTNER_KEY" >"$sender_bundle"
    if [[ "$DRY_RUN" != "true" ]]; then
        jq '.manifest' "$sender_bundle"
    fi

    # contributor_organization is the receiver's own org key: a transformation-group bundle
    # resolves BOTH of its data models under one ContributorOrganization, so the model received
    # here must carry the same tag as the receiver's own model for step 5 to find it.
    log_info "Receiving into ${receiver} as SourceSchema (contributor_organization=${receiver_org_key})"
    resp=$(api POST "${receiver_mdr}/exchange/receive?data_model_type=SourceSchema&contributor_organization=$(url_encode "$receiver_org_key")&allowMissingPaths=true" "$MDR_ADMIN_KEY" "$sender_bundle")
    jq '.dataModels' <<<"$resp"
    received_id=$(pick "$resp" '.dataModels[0].id' "${tag}-received-model-id")
    log_success "${receiver} now holds '${sender_model}' as data model id=${received_id}"

    # --- draft ------------------------------------------------------------
    banner "Generate a Draft crosswalk: '${receiver_model}' -> '${sender_model}' (crosswalk_draft CLI)"
    own_bundle="${WORKDIR}/${tag}-own-datamodel.json"
    log_info "Downloading ${receiver}'s own bundle for model ${receiver_model_id} -> ${own_bundle}"
    api GET "${receiver_mdr}/exchange/bundles/data-models/${receiver_model_id}" "$MDR_ADMIN_KEY" >"$own_bundle"

    draft_bundle="${WORKDIR}/${tag}-draft-group.json"
    report="${WORKDIR}/${tag}-draft-report.json"
    local -a cli=(uv run python scripts/generate-crosswalk-draft.py
        --source "$own_bundle" --target "$sender_bundle"
        --name "${receiver_model} → ${sender_model} (draft)" --version "$DRAFT_VERSION"
        --publisher "$receiver_publisher" -o "$draft_bundle" --report "$report")
    if [[ -f "$CROSSWALK" ]]; then
        cli+=(--crosswalk "$CROSSWALK")
        log_info "Using EDUcore crosswalk $(basename "$CROSSWALK") for value lookups (properties match by identity)"
    else
        log_warn "No EDUcore crosswalk at ${CROSSWALK}; identity matching only"
    fi
    if [[ "$DRY_RUN" == "true" ]]; then
        echo "  \$ (cd ${REPO_ROOT} && ${cli[*]})"
    else
        (cd "$REPO_ROOT" && "${cli[@]}")
        log_info "Report summary (${report}):"
        # Scalars from the report plus the size of each list/object, whatever the CLI emits.
        jq 'with_entries(.value |= (if type == "array" then "\(length) items"
                                     elif type == "object" then "\(keys | length) keys" else . end))' "$report"
    fi

    # --- receive draft ----------------------------------------------------
    banner "${receiver} receives the Draft transformation group"
    if resp=$(api POST "${receiver_mdr}/exchange/receive?data_model_type=SourceSchema&contributor_organization=$(url_encode "$receiver_org_key")&allowMissingPaths=true" "$MDR_ADMIN_KEY" "$draft_bundle"); then
        jq '{dataModels, transformationGroup: (.transformationGroup
              | {id, version, importedTransformationCount, skippedTransformationCount})}' <<<"$resp"
        group_id=$(pick "$resp" '.transformationGroup.id' "${tag}-group-id")
    else
        # Re-runs get a 409 (a group at DRAFT_VERSION already exists for this pair): reuse it.
        log_warn "Receive was rejected: ${resp}"
        log_warn "Looking for an existing group at version ${DRAFT_VERSION} for ${receiver_model_id} -> ${received_id}"
        resp=$(api GET "${receiver_mdr}/transformation_groups/exists/by-triplet?sourceId=${receiver_model_id}&targetId=${received_id}&version=${DRAFT_VERSION}&include_deleted=false" "$MDR_ADMIN_KEY")
        group_id=$(pick "$resp" '.id' "${tag}-group-id")
    fi
    log_success "Draft group id=${group_id}"
    # A human now refines the Draft (unmatched paths, value maps) in the MDR mapping editor.
    # The district MDR UI (lif-mdr-app, port 5173) is the only UI container in the demo stack.
    log_info "Refine it in the mapping editor: http://localhost:5173/explore/data-mappings/${group_id}"

    # --- translate --------------------------------------------------------
    banner "Translate a sample record through the ${receiver} Translator (${receiver_model_id} -> ${received_id})"
    log_info "Input: ${SAMPLE_RECORD}"
    translated="${WORKDIR}/${tag}-translated.json"
    if [[ "$DRY_RUN" == "true" ]]; then
        echo "  \$ curl -sS -X POST -H 'Content-Type: application/json' --data @${SAMPLE_RECORD} ${receiver_translator}/translate/source/${receiver_model_id}/target/${received_id}"
    else
        curl -sS --fail-with-body -m 300 -X POST -H "Content-Type: application/json" \
            --data "@${SAMPLE_RECORD}" \
            "${receiver_translator}/translate/source/${receiver_model_id}/target/${received_id}" \
            | jq '.' >"$translated"
        head -40 "$translated"
        echo "  ... (first 40 lines; full output in ${translated})"
    fi
}

main() {
    parse_args "$@"
    check_dependencies
    mkdir -p "$WORKDIR"
    WORKDIR="$(cd "$WORKDIR" && pwd)"

    echo -e "${BOLD}LIF schema-exchange demo${NC}"
    echo "  District MDR:  ${DISTRICT_MDR_URL}   translator ${DISTRICT_TRANSLATOR_URL}"
    echo "  College MDR:   ${COLLEGE_MDR_URL}   translator ${COLLEGE_TRANSLATOR_URL}"
    echo "  Workdir:       ${WORKDIR}"
    if [[ "$DRY_RUN" == "true" ]]; then
        log_warn "DRY RUN — printing commands, not executing"
    fi

    step_wait
    step_prepare_models

    run_exchange \
        "College" "$COLLEGE_MDR_URL" "$COLLEGE_MODEL_ID" "$COLLEGE_MODEL" \
        "District" "$DISTRICT_MDR_URL" "$DISTRICT_TRANSLATOR_URL" "$DISTRICT_MODEL_ID" "$DISTRICT_MODEL" \
        "$DISTRICT_ORG_KEY" "$DISTRICT_NAME" "district"

    if [[ "$REVERSE" == "true" ]]; then
        run_exchange \
            "District" "$DISTRICT_MDR_URL" "$DISTRICT_MODEL_ID" "$DISTRICT_MODEL" \
            "College" "$COLLEGE_MDR_URL" "$COLLEGE_TRANSLATOR_URL" "$COLLEGE_MODEL_ID" "$COLLEGE_MODEL" \
            "$COLLEGE_ORG_KEY" "$COLLEGE_NAME" "college"
    fi

    echo ""
    log_success "Done. Bundles and reports are in ${WORKDIR}"
}

main "$@"
