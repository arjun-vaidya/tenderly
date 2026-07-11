"""Fetch, parse, validate, and safely promote nonprofit opportunity listings.

This module deliberately has no HTTP endpoint. It is run by a DigitalOcean
App Platform scheduled job, which keeps import authority away from the public
API surface.
"""

import hashlib
import json
import logging
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import (
    OPPORTUNITY_IMPORT_TIMEOUT_SECONDS,
    OPPORTUNITY_IMPORT_USER_AGENT,
    OPPORTUNITY_PARSER_PATH,
    OPPORTUNITY_SOURCES_PATH,
)
from app.gradient_client import call_llm_json
from app.opportunity_repository import (
    create_import,
    finish_import,
    reconcile_source,
    save_import_records,
)

logger = logging.getLogger("tenderly.opportunity_ingestion")


class _PageTextExtractor(HTMLParser):
    """Small dependency-free HTML-to-text converter for static public pages."""

    BLOCK_TAGS = {"article", "br", "div", "h1", "h2", "h3", "h4", "li", "p", "section", "tr"}
    SKIP_TAGS = {"script", "style", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        if tag in self.BLOCK_TAGS and not self._skip_depth:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        if tag in self.BLOCK_TAGS and not self._skip_depth:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        return "\n".join(line.strip() for line in "".join(self._parts).splitlines() if line.strip())


def load_json_file(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def enabled_sources(source_config: dict[str, Any]) -> list[dict[str, Any]]:
    """Only sources explicitly enabled in version-controlled config can fetch."""
    return [source for source in source_config.get("sources", []) if source.get("enabled") is True]


def _host_is_allowed(url: str, allowed_domains: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == domain.lower() or host.endswith(f".{domain.lower()}") for domain in allowed_domains)


def fetch_source(source: dict[str, Any]) -> str:
    """Fetch static HTML or JSON only after enforcing the configured allowlist."""
    url = source.get("url", "")
    allowed_domains = source.get("allowed_domains", [])
    if not url or not _host_is_allowed(url, allowed_domains):
        raise ValueError(f"source {source.get('id')} has an empty or unapproved URL")

    with httpx.Client(timeout=OPPORTUNITY_IMPORT_TIMEOUT_SECONDS, follow_redirects=True) as client:
        response = client.get(url, headers={"User-Agent": OPPORTUNITY_IMPORT_USER_AGENT})
        response.raise_for_status()

    if source.get("type") == "json_api":
        return json.dumps(response.json(), ensure_ascii=False)
    if source.get("type") == "html_page":
        parser = _PageTextExtractor()
        parser.feed(response.text)
        text = parser.text()
        if not text:
            raise ValueError(f"source {source.get('id')} returned no readable page text")
        return text
    raise ValueError(f"source {source.get('id')} has unsupported type {source.get('type')!r}")


def _parser_system_prompt(parser_config: dict[str, Any]) -> str:
    allowed = ", ".join(parser_config["allowed_categories"])
    required = ", ".join(parser_config["required_output_fields"])
    rules = "\n".join(f"- {rule}" for rule in parser_config["parser_rules"])
    return (
        "You are Tenderly's opportunity-catalog extraction service. Return ONLY a valid JSON object, "
        "with no markdown and no prose.\n\n"
        "The object must be {\"opportunities\": [ ... ]}. Each opportunity must contain these fields: "
        f"{required}.\n"
        f"category must be one of: {allowed}.\n\nRules:\n{rules}"
    )


def _parser_user_prompt(source: dict[str, Any], content: str, parser_config: dict[str, Any]) -> str:
    # Cap the document before the model call so one unusually large listing
    # page cannot consume the entire scheduled job's token budget.
    parser_settings = source.get("parser_settings", {})
    # This override makes it possible to tighten a particularly noisy source
    # without changing the global parser policy.
    limit = int(parser_settings.get("input_character_limit", parser_config["model"]["input_character_limit"]))
    maximum_records = int(parser_settings.get("maximum_records_per_source", parser_config["model"]["maximum_records_per_source"]))
    excerpt = content[:limit]
    return (
        f"Approved source: {source['name']}\n"
        f"Canonical source URL: {source['url']}\n"
        f"Maximum records to return: {maximum_records}\n"
        f"Source defaults (use only when a field is absent): {json.dumps(source.get('defaults', {}))}\n\n"
        f"Source content:\n{excerpt}"
    )


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return sorted({item.strip() for item in value if isinstance(item, str) and item.strip()})


def _bounded_number(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(number, 0.0), 1.0)


def _safe_source_url(record_url: Any, source: dict[str, Any]) -> str:
    """Keep a link only when it belongs to the source's public allowlist."""
    url = record_url.strip() if isinstance(record_url, str) else ""
    allowed = source.get("allowed_domains", [])
    return url if url and _host_is_allowed(url, allowed) else source["url"]


def _normalized_evidence(value: str) -> str:
    """Normalize punctuation and whitespace for a resilient quote check."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.lower()).split())


def normalize_candidate(
    raw: Any,
    source: dict[str, Any],
    parser_config: dict[str, Any],
    source_content: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Convert untrusted model output into the exact database/matching shape."""
    if not isinstance(raw, dict):
        return None, "parser returned a non-object record"

    defaults = source.get("defaults", {})
    allowed_categories = set(parser_config["allowed_categories"])
    org_name = str(raw.get("org_name", "")).strip()
    title = str(raw.get("title", "")).strip()
    evidence = str(raw.get("evidence", "")).strip()
    # A source quote is a safe, useful fallback when an event card has a
    # title plus an exact role sentence but no separate description field.
    description = str(raw.get("description") or evidence).strip()
    commitment = str(raw.get("commitment") or defaults.get("commitment") or "See official source for schedule").strip()

    missing = [name for name, value in (("org_name", org_name), ("title", title), ("description", description), ("commitment", commitment)) if not value]
    if missing:
        return None, f"missing required field(s): {', '.join(missing)}"
    if source_content is not None:
        normalized_evidence = _normalized_evidence(evidence)
        if len(normalized_evidence) < 12:
            return None, "missing or too-short source evidence"
        if normalized_evidence not in _normalized_evidence(source_content):
            return None, "source evidence was not found verbatim in the fetched content"

    category = str(raw.get("category") or defaults.get("category") or "community").strip().lower()
    if category not in allowed_categories:
        return None, f"unsupported category: {category}"

    external_id = str(raw.get("external_id") or "").strip()
    source_url = _safe_source_url(raw.get("source_url"), source)
    identity = external_id or f"{org_name}|{title}|{source_url}"
    source_key = f"{source['id']}:{hashlib.sha256(identity.lower().encode('utf-8')).hexdigest()[:20]}"

    neighborhood = str(raw.get("neighborhood") or defaults.get("neighborhood") or "Citywide").strip()
    try:
        lat = float(raw.get("lat", defaults.get("lat", 37.7749)))
        lng = float(raw.get("lng", defaults.get("lng", -122.4194)))
    except (TypeError, ValueError):
        return None, "invalid latitude or longitude"
    if not (37.6 <= lat <= 38.0 and -122.6 <= lng <= -122.2):
        return None, "location is outside San Francisco"

    confidence = _bounded_number(raw.get("confidence"), 0.0)
    record = {
        "source_id": source["id"],
        "source_key": source_key,
        "org_name": org_name[:200],
        "title": title[:200],
        "description": description[:4_000],
        "category": category,
        "neighborhood": neighborhood[:120],
        "lat": lat,
        "lng": lng,
        "commitment": commitment[:240],
        "availability": _as_string_list(raw.get("availability")),
        "needed_skills": _as_string_list(raw.get("needed_skills")),
        "causes": _as_string_list(raw.get("causes") or defaults.get("causes")),
        "base_urgency": _bounded_number(raw.get("base_urgency"), float(defaults.get("base_urgency", 0.5))),
        "source_url": source_url,
        "confidence": confidence,
        "source_metadata": {
            "raw_description": description[:4_000],
            "source_payload": {"parser_record": raw, "evidence": evidence},
            "location_type": "onsite",
            "application_url": source_url,
            "application_mode": "external_link",
            "provider_functions": _as_string_list(raw.get("needed_skills")),
            "areas_of_focus": _as_string_list(raw.get("causes") or defaults.get("causes")),
            "times_of_day": _as_string_list(raw.get("availability")),
            "other_requirements": evidence or None,
        },
    }
    return record, None


def parse_and_classify(content: str, source: dict[str, Any], parser_config: dict[str, Any]) -> tuple[list[dict], list[tuple[dict, str]], list[dict]]:
    """Return auto-approved records, review records, and all valid parsed records."""
    fallback = {"opportunities": []}
    model_config = parser_config["model"]
    parser_settings = source.get("parser_settings", {})
    max_tokens = int(parser_settings.get("max_tokens", model_config["max_tokens"]))
    raw_response = call_llm_json(
        _parser_system_prompt(parser_config),
        _parser_user_prompt(source, content, parser_config),
        fallback,
        max_tokens=max_tokens,
        temperature=float(model_config["temperature"]),
        timeout_seconds=float(model_config["timeout_seconds"]),
    )
    raw_records = raw_response.get("opportunities", []) if isinstance(raw_response, dict) else []
    if not isinstance(raw_records, list):
        raw_records = []

    # A verbatim evidence match is a stricter guard than the model's
    # self-reported confidence. Keep source configuration from silently
    # becoming stricter than the documented catalog-wide promotion policy.
    threshold = min(
        float(source.get("auto_promote_confidence", parser_config["promotion_policy"]["minimum_confidence"])),
        float(parser_config["promotion_policy"]["minimum_confidence"]),
    )
    approved: list[dict] = []
    pending: list[tuple[dict, str]] = []
    valid: list[dict] = []
    maximum_records = int(source.get("parser_settings", {}).get("maximum_records_per_source", model_config["maximum_records_per_source"]))
    for raw in raw_records[:maximum_records]:
        record, error = normalize_candidate(raw, source, parser_config, source_content=content)
        if record is None:
            logger.info("rejected parser record source=%s reason=%s", source["id"], error)
            continue
        valid.append(record)
        if record["confidence"] >= threshold:
            approved.append(record)
        else:
            pending.append((record, f"confidence {record['confidence']:.2f} is below auto-promote threshold {threshold:.2f}"))
    return approved, pending, valid
