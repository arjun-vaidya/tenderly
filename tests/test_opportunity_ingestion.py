from app.ingestion_job import INGESTION_POLICY_VERSION, import_content_hash
from app.opportunity_ingestion import _PageTextExtractor, _parser_user_prompt, enabled_sources, normalize_candidate


def _parser_config() -> dict:
    return {
        "allowed_categories": ["education", "community"],
        "promotion_policy": {"minimum_confidence": 0.9},
    }


def _source() -> dict:
    return {
        "id": "example",
        "url": "https://volunteer.example.org/roles",
        "allowed_domains": ["volunteer.example.org"],
        "defaults": {
            "category": "education",
            "neighborhood": "Mission",
            "lat": 37.76,
            "lng": -122.42,
            "causes": ["education"],
            "base_urgency": 0.5,
        },
    }


def test_enabled_sources_requires_explicit_true() -> None:
    sources = enabled_sources({"sources": [{"id": "on", "enabled": True}, {"id": "off", "enabled": False}]})
    assert [source["id"] for source in sources] == ["on"]


def test_normalize_candidate_applies_safe_defaults_and_allowlist() -> None:
    record, error = normalize_candidate(
        {
            "external_id": "tutor-1",
            "org_name": "Example Org",
            "title": "Writing Tutor",
            "description": "Support students with writing practice.",
            "category": "education",
            "commitment": "2 hours each week",
            "availability": ["weekday afternoons"],
            "needed_skills": ["writing", "teaching"],
            "causes": ["education"],
            "source_url": "https://untrusted.example.net/role/1",
            "confidence": 0.96,
        },
        _source(),
        _parser_config(),
    )

    assert error is None
    assert record is not None
    assert record["source_url"] == "https://volunteer.example.org/roles"
    assert record["neighborhood"] == "Mission"
    assert record["confidence"] == 0.96
    assert record["source_key"].startswith("example:")


def test_html_text_extractor_ignores_scripts() -> None:
    parser = _PageTextExtractor()
    parser.feed("<h1>Volunteer today</h1><script>secret()</script><p>Help students.</p>")
    assert parser.text() == "Volunteer today\nHelp students."


def test_normalize_candidate_rejects_evidence_not_in_source() -> None:
    record, error = normalize_candidate(
        {
            "external_id": "tutor-1",
            "org_name": "Example Org",
            "title": "Writing Tutor",
            "description": "Support students with writing practice.",
            "category": "education",
            "commitment": "2 hours each week",
            "evidence": "A role that does not appear on the page.",
            "confidence": 0.96,
        },
        _source(),
        _parser_config(),
        source_content="Volunteer tutors support students with writing practice.",
    )

    assert record is None
    assert error == "source evidence was not found verbatim in the fetched content"


def test_normalize_candidate_keeps_verified_evidence_in_audit_metadata() -> None:
    source_content = "Volunteer tutors support students with writing practice every week."
    record, error = normalize_candidate(
        {
            "external_id": "tutor-1",
            "org_name": "Example Org",
            "title": "Writing Tutor",
            "description": "Support students with writing practice.",
            "category": "education",
            "commitment": "2 hours each week",
            "evidence": "Volunteer tutors support students with writing practice every week.",
            "confidence": 0.96,
        },
        _source(),
        _parser_config(),
        source_content=source_content,
    )

    assert error is None
    assert record is not None
    assert record["source_metadata"]["other_requirements"] == source_content


def test_normalize_candidate_uses_verified_evidence_when_description_is_omitted() -> None:
    source_content = "Volunteer tutors support students with writing practice every week."
    record, error = normalize_candidate(
        {
            "external_id": "tutor-1",
            "org_name": "Example Org",
            "title": "Writing Tutor",
            "category": "education",
            "evidence": source_content,
            "confidence": 0.96,
        },
        _source(),
        _parser_config(),
        source_content=source_content,
    )

    assert error is None
    assert record is not None
    assert record["description"] == source_content
    assert record["commitment"] == "See official source for schedule"


def test_import_hash_changes_when_the_parser_policy_changes() -> None:
    content = "A public volunteer page"
    source = _source()
    parser_a = {"required_output_fields": ["title"]}
    parser_b = {"required_output_fields": ["title", "evidence"]}

    assert import_content_hash(content, source, parser_a) != import_content_hash(content, source, parser_b)


def test_ingestion_policy_version_is_explicit() -> None:
    assert INGESTION_POLICY_VERSION.startswith("evidence-")


def test_parser_prompt_uses_source_record_limit() -> None:
    source = {**_source(), "name": "Example source", "parser_settings": {"maximum_records_per_source": 3}}
    prompt = _parser_user_prompt(source, "Volunteer tutor role.", {"model": {"input_character_limit": 100, "maximum_records_per_source": 12}})

    assert "Maximum records to return: 3" in prompt
