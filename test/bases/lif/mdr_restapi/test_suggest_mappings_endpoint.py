from unittest import mock

import pytest


@pytest.fixture
def mdr_api_headers():
    return {"X-API-Key": "changeme1"}


def _mock_anthropic_response(json_text: str):
    """Create a mock Anthropic message response."""
    block = mock.MagicMock()
    block.text = json_text
    message = mock.MagicMock()
    message.content = [block]
    return message


def _make_request(selected_side="source", selected_name="expirationDate", entity="Credential", candidates=None):
    """Build a standard suggest mappings request body."""
    if candidates is None:
        candidates = [
            {
                "attribute_id": 100,
                "attribute_name": "EndDate",
                "entity_name": "Course",
                "description": "The date the course ends",
                "data_type": "date",
            },
            {
                "attribute_id": 101,
                "attribute_name": "CourseNumber",
                "entity_name": "Course",
                "description": "The course catalog number",
                "data_type": "string",
            },
            {
                "attribute_id": 102,
                "attribute_name": "ExpiryDate",
                "entity_name": "Certificate",
                "description": "When the certificate expires",
                "data_type": "date",
                "value_set_values": None,
            },
        ]
    return {
        "selected_side": selected_side,
        "selected_field": {
            "attribute_id": 1,
            "attribute_name": selected_name,
            "entity_name": entity,
            "description": "The date the credential expires",
            "data_type": "date",
        },
        "candidate_fields": candidates,
    }


@pytest.mark.asyncio
async def test_suggest_mappings_returns_suggestions(async_client_mdr, mdr_api_headers):
    """Claude returns 3 suggestions, all above threshold."""
    claude_response = _mock_anthropic_response(
        '[{"attribute_id": 102, "entity_id_path": null, "confidence": 0.92, "reason": "Both represent expiration dates"},'
        ' {"attribute_id": 100, "entity_id_path": null, "confidence": 0.75, "reason": "EndDate is semantically similar"},'
        ' {"attribute_id": 101, "entity_id_path": null, "confidence": 0.55, "reason": "Weak match on course context"}]'
    )

    with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
        with mock.patch("lif.mdr_restapi.suggest_mappings_endpoint.anthropic") as mock_anthropic:
            mock_client = mock.MagicMock()
            mock_client.messages.create.return_value = claude_response
            mock_anthropic.Anthropic.return_value = mock_client

            response = await async_client_mdr.post(
                "/suggest_mappings/",
                headers=mdr_api_headers,
                json=_make_request(),
            )

    assert response.status_code == 200
    data = response.json()
    suggestions = data["suggestions"]
    assert len(suggestions) == 3
    # Sorted by confidence descending
    assert suggestions[0]["confidence"] == 0.92
    assert suggestions[0]["candidate_attribute_id"] == 102
    assert suggestions[1]["confidence"] == 0.75
    assert suggestions[2]["confidence"] == 0.55
    # Each has a reason
    assert all(s["reason"] for s in suggestions)


@pytest.mark.asyncio
async def test_suggest_mappings_filters_below_threshold(async_client_mdr, mdr_api_headers):
    """Suggestions below 0.50 confidence are filtered out."""
    claude_response = _mock_anthropic_response(
        '[{"attribute_id": 102, "confidence": 0.85, "reason": "Strong match"},'
        ' {"attribute_id": 100, "confidence": 0.30, "reason": "Weak match"},'
        ' {"attribute_id": 101, "confidence": 0.10, "reason": "No real match"}]'
    )

    with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
        with mock.patch("lif.mdr_restapi.suggest_mappings_endpoint.anthropic") as mock_anthropic:
            mock_client = mock.MagicMock()
            mock_client.messages.create.return_value = claude_response
            mock_anthropic.Anthropic.return_value = mock_client

            response = await async_client_mdr.post(
                "/suggest_mappings/",
                headers=mdr_api_headers,
                json=_make_request(),
            )

    assert response.status_code == 200
    suggestions = response.json()["suggestions"]
    assert len(suggestions) == 1
    assert suggestions[0]["candidate_attribute_id"] == 102
    assert suggestions[0]["confidence"] == 0.85


@pytest.mark.asyncio
async def test_suggest_mappings_truncates_to_three(async_client_mdr, mdr_api_headers):
    """Even if Claude returns more than 3 above threshold, only top 3 are returned."""
    claude_response = _mock_anthropic_response(
        '[{"attribute_id": 1, "confidence": 0.95, "reason": "A"},'
        ' {"attribute_id": 2, "confidence": 0.90, "reason": "B"},'
        ' {"attribute_id": 3, "confidence": 0.85, "reason": "C"},'
        ' {"attribute_id": 4, "confidence": 0.80, "reason": "D"},'
        ' {"attribute_id": 5, "confidence": 0.75, "reason": "E"}]'
    )

    with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
        with mock.patch("lif.mdr_restapi.suggest_mappings_endpoint.anthropic") as mock_anthropic:
            mock_client = mock.MagicMock()
            mock_client.messages.create.return_value = claude_response
            mock_anthropic.Anthropic.return_value = mock_client

            response = await async_client_mdr.post(
                "/suggest_mappings/",
                headers=mdr_api_headers,
                json=_make_request(),
            )

    suggestions = response.json()["suggestions"]
    assert len(suggestions) == 3
    assert [s["candidate_attribute_id"] for s in suggestions] == [1, 2, 3]


@pytest.mark.asyncio
async def test_suggest_mappings_no_api_key(async_client_mdr, mdr_api_headers):
    """Returns empty suggestions when ANTHROPIC_API_KEY is not set."""
    with mock.patch.dict("os.environ", {}, clear=False):
        # Ensure key is removed
        import os
        orig = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            response = await async_client_mdr.post(
                "/suggest_mappings/",
                headers=mdr_api_headers,
                json=_make_request(),
            )
        finally:
            if orig is not None:
                os.environ["ANTHROPIC_API_KEY"] = orig

    assert response.status_code == 200
    assert response.json()["suggestions"] == []


@pytest.mark.asyncio
async def test_suggest_mappings_empty_candidates(async_client_mdr, mdr_api_headers):
    """Returns empty suggestions when no candidate fields provided."""
    with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
        response = await async_client_mdr.post(
            "/suggest_mappings/",
            headers=mdr_api_headers,
            json=_make_request(candidates=[]),
        )

    assert response.status_code == 200
    assert response.json()["suggestions"] == []


@pytest.mark.asyncio
async def test_suggest_mappings_claude_api_error(async_client_mdr, mdr_api_headers):
    """Returns empty suggestions on Claude API failure (graceful degradation)."""
    with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
        with mock.patch("lif.mdr_restapi.suggest_mappings_endpoint.anthropic") as mock_anthropic:
            mock_client = mock.MagicMock()
            mock_client.messages.create.side_effect = Exception("API timeout")
            mock_anthropic.Anthropic.return_value = mock_client

            response = await async_client_mdr.post(
                "/suggest_mappings/",
                headers=mdr_api_headers,
                json=_make_request(),
            )

    assert response.status_code == 200
    assert response.json()["suggestions"] == []


@pytest.mark.asyncio
async def test_suggest_mappings_claude_returns_invalid_json(async_client_mdr, mdr_api_headers):
    """Returns empty suggestions when Claude returns unparsable response."""
    claude_response = _mock_anthropic_response("This is not valid JSON at all")

    with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
        with mock.patch("lif.mdr_restapi.suggest_mappings_endpoint.anthropic") as mock_anthropic:
            mock_client = mock.MagicMock()
            mock_client.messages.create.return_value = claude_response
            mock_anthropic.Anthropic.return_value = mock_client

            response = await async_client_mdr.post(
                "/suggest_mappings/",
                headers=mdr_api_headers,
                json=_make_request(),
            )

    assert response.status_code == 200
    assert response.json()["suggestions"] == []


@pytest.mark.asyncio
async def test_suggest_mappings_claude_returns_markdown_fenced_json(async_client_mdr, mdr_api_headers):
    """Handles Claude wrapping JSON in markdown code fences."""
    claude_response = _mock_anthropic_response(
        '```json\n[{"attribute_id": 102, "confidence": 0.88, "reason": "Match"}]\n```'
    )

    with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
        with mock.patch("lif.mdr_restapi.suggest_mappings_endpoint.anthropic") as mock_anthropic:
            mock_client = mock.MagicMock()
            mock_client.messages.create.return_value = claude_response
            mock_anthropic.Anthropic.return_value = mock_client

            response = await async_client_mdr.post(
                "/suggest_mappings/",
                headers=mdr_api_headers,
                json=_make_request(),
            )

    suggestions = response.json()["suggestions"]
    assert len(suggestions) == 1
    assert suggestions[0]["candidate_attribute_id"] == 102


@pytest.mark.asyncio
async def test_suggest_mappings_includes_entity_id_path(async_client_mdr, mdr_api_headers):
    """entity_id_path from Claude response is passed through."""
    claude_response = _mock_anthropic_response(
        '[{"attribute_id": 102, "entity_id_path": "654,22,6", "confidence": 0.90, "reason": "Match with path"}]'
    )

    with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
        with mock.patch("lif.mdr_restapi.suggest_mappings_endpoint.anthropic") as mock_anthropic:
            mock_client = mock.MagicMock()
            mock_client.messages.create.return_value = claude_response
            mock_anthropic.Anthropic.return_value = mock_client

            response = await async_client_mdr.post(
                "/suggest_mappings/",
                headers=mdr_api_headers,
                json=_make_request(),
            )

    suggestions = response.json()["suggestions"]
    assert suggestions[0]["candidate_entity_id_path"] == "654,22,6"


@pytest.mark.asyncio
async def test_suggest_mappings_passes_correct_model(async_client_mdr, mdr_api_headers):
    """Verifies Claude is called with the correct model name."""
    claude_response = _mock_anthropic_response("[]")

    with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
        with mock.patch("lif.mdr_restapi.suggest_mappings_endpoint.anthropic") as mock_anthropic:
            mock_client = mock.MagicMock()
            mock_client.messages.create.return_value = claude_response
            mock_anthropic.Anthropic.return_value = mock_client

            await async_client_mdr.post(
                "/suggest_mappings/",
                headers=mdr_api_headers,
                json=_make_request(),
            )

            call_kwargs = mock_client.messages.create.call_args
            assert call_kwargs.kwargs["model"] == "claude-sonnet-4-20250514"
            assert call_kwargs.kwargs["max_tokens"] == 1024


@pytest.mark.asyncio
async def test_suggest_mappings_prompt_includes_field_context(async_client_mdr, mdr_api_headers):
    """Verifies the prompt sent to Claude includes field metadata."""
    claude_response = _mock_anthropic_response("[]")

    request_body = _make_request()
    request_body["selected_field"]["value_set_values"] = ["Active", "Expired", "Revoked"]
    request_body["existing_mappings"] = [
        {"source_name": "Credential.name", "target_name": "Course.CourseName"}
    ]

    with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
        with mock.patch("lif.mdr_restapi.suggest_mappings_endpoint.anthropic") as mock_anthropic:
            mock_client = mock.MagicMock()
            mock_client.messages.create.return_value = claude_response
            mock_anthropic.Anthropic.return_value = mock_client

            await async_client_mdr.post(
                "/suggest_mappings/",
                headers=mdr_api_headers,
                json=request_body,
            )

            prompt = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
            # Selected field details appear in prompt
            assert "expirationDate" in prompt
            assert "Credential" in prompt
            assert "date" in prompt
            # Value sets appear
            assert "Active" in prompt
            assert "Expired" in prompt
            # Candidate fields appear
            assert "EndDate" in prompt
            assert "CourseNumber" in prompt
            # Existing mappings appear
            assert "ALREADY MAPPED" in prompt
            assert "Credential.name" in prompt
            assert "Course.CourseName" in prompt


@pytest.mark.asyncio
async def test_suggest_mappings_target_side(async_client_mdr, mdr_api_headers):
    """Works when selecting from target side too."""
    claude_response = _mock_anthropic_response(
        '[{"attribute_id": 1, "confidence": 0.80, "reason": "Reverse match"}]'
    )

    with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
        with mock.patch("lif.mdr_restapi.suggest_mappings_endpoint.anthropic") as mock_anthropic:
            mock_client = mock.MagicMock()
            mock_client.messages.create.return_value = claude_response
            mock_anthropic.Anthropic.return_value = mock_client

            response = await async_client_mdr.post(
                "/suggest_mappings/",
                headers=mdr_api_headers,
                json=_make_request(selected_side="target"),
            )

            prompt = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
            assert "target data model" in prompt

    assert response.status_code == 200
    assert len(response.json()["suggestions"]) == 1
