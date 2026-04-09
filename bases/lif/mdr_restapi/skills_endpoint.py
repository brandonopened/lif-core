from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()

SKILLS_CONTEXT = {
    "@context": [
        "https://purl.imsglobal.org/spec/ob/v3p0/context-3.0.3.json",
        {
            "@protected": True,
            "schema": "https://schema.org/",
            "id": "@id",
            "type": "@type",
            "identifier": {"@id": "@id"},
            "targetType": {"@id": "schema:additionalType"},
            "proficiencyScale": {"@id": "schema:isPartOf", "@type": "@id"},
            "assertions": {"@id": "schema:hasPart"},
            "SkillAssertionCollection": {
                "@id": "schema:Collection",
                "@context": {
                    "@protected": True,
                    "identifier": "@id",
                    "targetType": "schema:additionalType",
                    "proficiencyScale": {"@id": "schema:isPartOf", "@type": "@id"},
                    "assertions": {"@id": "schema:hasPart"},
                },
            },
            "SkillAssertion": {
                "@id": "schema:SkillAssertion",
                "@context": {
                    "@protected": True,
                    "skill": {"@id": "schema:about", "@type": "@id"},
                    "proficiencyLevel": {"@id": "schema:hasDefinedTerm", "@type": "@id"},
                    "source": {"@id": "schema:provider", "@type": "@id"},
                },
            },
            "Skill": {
                "@id": "schema:Skill",
                "@context": {
                    "@protected": True,
                    "id": "@id",
                    "type": "@type",
                    "name": "schema:name",
                    "description": "schema:description",
                    "codedNotation": "schema:identifier",
                },
            },
            "ProficiencyLevel": {
                "@id": "schema:DefinedTerm",
                "@context": {
                    "@protected": True,
                    "id": "@id",
                    "type": "@type",
                    "name": "schema:name",
                    "description": "schema:description",
                    "rank": "schema:position",
                },
            },
            "source": {
                "@id": "schema:Organization",
                "@context": {
                    "@protected": True,
                    "id": "@id",
                    "type": "@type",
                    "name": "schema:name",
                },
            },
        },
    ]
}


@router.get("/context")
async def get_skills_context():
    return JSONResponse(
        content=SKILLS_CONTEXT,
        media_type="application/ld+json",
    )
