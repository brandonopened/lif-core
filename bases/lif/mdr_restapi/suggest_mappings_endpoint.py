import json
import os
from typing import Literal

import anthropic
from fastapi import APIRouter
from lif.mdr_utils.logger_config import get_logger
from pydantic import BaseModel

router = APIRouter()
logger = get_logger(__name__)


class FieldInfo(BaseModel):
    attribute_id: int
    attribute_name: str
    entity_name: str
    entity_id_path: str | None = None
    description: str | None = None
    data_type: str | None = None
    value_set_values: list[str] | None = None


class ExistingMapping(BaseModel):
    source_name: str
    target_name: str


class SuggestMappingsRequest(BaseModel):
    selected_side: Literal["source", "target"]
    selected_field: FieldInfo
    candidate_fields: list[FieldInfo]
    existing_mappings: list[ExistingMapping] | None = None


class SuggestedMapping(BaseModel):
    candidate_attribute_id: int
    candidate_entity_id_path: str | None = None
    confidence: float
    reason: str


class SuggestMappingsResponse(BaseModel):
    suggestions: list[SuggestedMapping]


def _build_field_description(field: FieldInfo) -> str:
    parts = [f"{field.entity_name}.{field.attribute_name}"]
    if field.data_type:
        parts.append(f"Type: {field.data_type}")
    if field.description:
        parts.append(f"Desc: {field.description}")
    if field.value_set_values:
        values_preview = ", ".join(field.value_set_values[:20])
        parts.append(f"Values: [{values_preview}]")
    return " | ".join(parts)


def _build_prompt(request: SuggestMappingsRequest) -> str:
    opposite_side = "target" if request.selected_side == "source" else "source"

    selected_desc = _build_field_description(request.selected_field)

    candidate_lines = []
    for i, field in enumerate(request.candidate_fields, 1):
        desc = _build_field_description(field)
        candidate_lines.append(
            f"{i}. [id={field.attribute_id}, entity_id_path={field.entity_id_path or 'null'}] {desc}"
        )
    candidates_block = "\n".join(candidate_lines)

    existing_block = ""
    if request.existing_mappings:
        existing_lines = [
            f"- {m.source_name} <-> {m.target_name}" for m in request.existing_mappings
        ]
        existing_block = (
            "\n\nALREADY MAPPED (do not re-suggest these):\n"
            + "\n".join(existing_lines)
        )

    return f"""You are a data model field mapping expert. Given a selected field from a {request.selected_side} data model and a list of candidate fields from the {opposite_side} data model, suggest the best matches.

SELECTED FIELD:
{selected_desc}

CANDIDATE FIELDS:
{candidates_block}{existing_block}

Return a JSON array of up to 3 best matches. Each entry must be:
{{"attribute_id": <int>, "entity_id_path": <string or null>, "confidence": <float 0-1>, "reason": "<brief explanation>"}}

Only include matches with confidence >= 0.50. Consider:
- Semantic similarity of names and descriptions
- Compatible data types
- Overlapping or related value sets
- Entity context and hierarchy (parent entity names indicate the domain)
- Position and role of the field within its entity

Return ONLY the JSON array, no other text."""


@router.post("/", response_model=SuggestMappingsResponse)
async def suggest_mappings(request: SuggestMappingsRequest):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set, returning empty suggestions")
        return SuggestMappingsResponse(suggestions=[])

    logger.info(
        f"Suggest mappings: side={request.selected_side}, "
        f"field={request.selected_field.entity_name}.{request.selected_field.attribute_name}, "
        f"candidates={len(request.candidate_fields)}"
    )

    if not request.candidate_fields:
        logger.warning("No candidate fields provided, returning empty suggestions")
        return SuggestMappingsResponse(suggestions=[])

    prompt = _build_prompt(request)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )

        first_block = message.content[0]
        if not hasattr(first_block, "text"):
            logger.warning("Claude returned non-text block, returning empty suggestions")
            return SuggestMappingsResponse(suggestions=[])
        response_text = first_block.text.strip()
        # Strip markdown code fences if present
        if response_text.startswith("```"):
            lines = response_text.split("\n")
            # Remove first and last lines (```json and ```)
            lines = [l for l in lines if not l.strip().startswith("```")]
            response_text = "\n".join(lines)

        raw_suggestions = json.loads(response_text)

        suggestions = []
        for item in raw_suggestions:
            confidence = float(item.get("confidence", 0))
            if confidence < 0.50:
                continue
            suggestions.append(
                SuggestedMapping(
                    candidate_attribute_id=int(item["attribute_id"]),
                    candidate_entity_id_path=item.get("entity_id_path"),
                    confidence=confidence,
                    reason=str(item.get("reason", "")),
                )
            )

        suggestions.sort(key=lambda s: s.confidence, reverse=True)
        suggestions = suggestions[:3]

        logger.info(f"Returning {len(suggestions)} suggestions: {[(s.candidate_attribute_id, s.confidence) for s in suggestions]}")
        return SuggestMappingsResponse(suggestions=suggestions)

    except Exception:
        logger.exception("Failed to get mapping suggestions from Claude")
        return SuggestMappingsResponse(suggestions=[])
