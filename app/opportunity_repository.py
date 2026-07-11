"""Database access for Tenderly's auditable opportunity catalog.

Local development intentionally keeps using data/opportunities.json when no
DATABASE_URL exists. On App Platform, Managed PostgreSQL is the source of
truth and the API reads only opportunities whose status is ``active``.
"""

import hashlib
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from app.config import OPPORTUNITIES_PATH, OPPORTUNITY_MIGRATIONS_DIR

logger = logging.getLogger("tenderly.opportunity_repository")

try:  # Keeps the mock/local JSON-only experience lightweight.
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - installed in the production image
    psycopg = None
    dict_row = None


def database_url() -> str:
    """Read at call time so commands and tests can set DATABASE_URL normally."""
    return os.environ.get("DATABASE_URL", "").strip()


def _require_database() -> str:
    url = database_url()
    if not url:
        raise RuntimeError("DATABASE_URL is required for the opportunity ingestion pipeline")
    if psycopg is None:
        raise RuntimeError("psycopg is not installed; run pip install -r requirements.txt")
    return url


def _seed_catalog() -> list[dict[str, Any]]:
    with open(OPPORTUNITIES_PATH, "r", encoding="utf-8") as file:
        return json.load(file)


def stable_opportunity_id(source_key: str) -> str:
    return f"opp_{hashlib.sha256(source_key.encode('utf-8')).hexdigest()[:16]}"


def ensure_schema() -> None:
    """Apply every idempotent catalog migration in order."""
    url = _require_database()
    with psycopg.connect(url) as connection:
        with connection.cursor() as cursor:
            for migration_path in sorted(Path(OPPORTUNITY_MIGRATIONS_DIR).glob("*.sql")):
                cursor.execute(migration_path.read_text(encoding="utf-8"))


def load_active_opportunities() -> list[dict[str, Any]]:
    """Return active database rows, or the seed catalog when no database is attached."""
    if not database_url():
        return _seed_catalog()

    try:
        with psycopg.connect(_require_database(), row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        o.opportunity_id,
                        org.name AS org_name,
                        o.title,
                        o.category,
                        o.neighborhood,
                        o.lat,
                        o.lng,
                        o.commitment,
                        o.availability,
                        o.needed_skills,
                        o.causes,
                        o.base_urgency
                    FROM opportunities AS o
                    JOIN organizations AS org ON org.id = o.organization_id
                    WHERE o.status = 'active'
                    ORDER BY o.updated_at DESC, o.title ASC
                    """
                )
                return list(cursor.fetchall())
    except Exception:
        # The public API still needs a friendly frontend error state during a
        # database outage; falling back to curated seed roles keeps the demo
        # useful while the failure is visible in App Platform logs.
        logger.exception("could not load database opportunities; using seed catalog")
        return _seed_catalog()


def seed_catalog_if_empty() -> int:
    """Copy the curated hackathon roles into PostgreSQL once, without overwriting imports."""
    _require_database()
    with psycopg.connect(database_url(), row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS count FROM opportunities")
            if cursor.fetchone()["count"]:
                return 0

        seeded = 0
        for raw in _seed_catalog():
            record = {
                **raw,
                "description": raw.get("description") or f"Volunteer as a {raw['title']} with {raw['org_name']}.",
                "source_id": "tenderly_seed_catalog",
                "source_key": f"tenderly_seed_catalog:{raw['opportunity_id']}",
                "source_url": "https://github.com/arjun-vaidya/tenderly/tree/main/data/opportunities.json",
            }
            upsert_opportunity(connection, record)
            seeded += 1
        return seeded


def create_import(source_id: str, source_url: str, content_hash: str, raw_content: str) -> str | None:
    """Create an import audit row; None means an identical source was already processed."""
    import_id = str(uuid.uuid4())
    with psycopg.connect(_require_database()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO opportunity_imports (id, source_id, source_url, content_hash, raw_content, status)
                VALUES (%s, %s, %s, %s, %s, 'started')
                ON CONFLICT (source_id, content_hash) DO UPDATE SET
                    status = 'started',
                    error_message = NULL
                WHERE opportunity_imports.status IN ('started', 'failed')
                RETURNING id
                """,
                (import_id, source_id, source_url, content_hash, raw_content),
            )
            row = cursor.fetchone()
            return str(row[0]) if row else None


def latest_import_content(source_id: str) -> str | None:
    """Return the latest raw snapshot for a source, for safe re-normalization."""
    with psycopg.connect(_require_database(), row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT raw_content
                FROM opportunity_imports
                WHERE source_id = %s
                ORDER BY imported_at DESC
                LIMIT 1
                """,
                (source_id,),
            )
            row = cursor.fetchone()
            return row["raw_content"] if row else None


def finish_import(import_id: str, status: str, parsed_records: list[dict] | None = None, error: str | None = None) -> None:
    with psycopg.connect(_require_database()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE opportunity_imports
                SET status = %s, parsed_records = %s::jsonb, error_message = %s
                WHERE id = %s
                """,
                (status, json.dumps(parsed_records or []), error, import_id),
            )


def upsert_opportunity(connection: Any, record: dict[str, Any]) -> str:
    """Upsert a vetted role and its organization in one database transaction."""
    source_key = record["source_key"]
    opportunity_id = record.get("opportunity_id") or stable_opportunity_id(source_key)
    organization_id = str(uuid.uuid4())
    status = record.get("status", "active")
    if status not in {"draft", "active", "paused", "closed"}:
        raise ValueError(f"invalid opportunity status: {status!r}")

    organization_metadata = record.get("organization_metadata", {})
    metadata = record.get("source_metadata", {})

    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO organizations (
                id, name, website, source_id, external_id, source_url, logo_url,
                areas_of_focus, organization_type, address
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (name) DO UPDATE SET
                website = COALESCE(EXCLUDED.website, organizations.website),
                source_id = COALESCE(EXCLUDED.source_id, organizations.source_id),
                external_id = COALESCE(EXCLUDED.external_id, organizations.external_id),
                source_url = COALESCE(EXCLUDED.source_url, organizations.source_url),
                logo_url = COALESCE(EXCLUDED.logo_url, organizations.logo_url),
                areas_of_focus = CASE
                    WHEN cardinality(EXCLUDED.areas_of_focus) > 0 THEN EXCLUDED.areas_of_focus
                    ELSE organizations.areas_of_focus
                END,
                organization_type = COALESCE(EXCLUDED.organization_type, organizations.organization_type),
                address = CASE WHEN EXCLUDED.address = '{}'::jsonb THEN organizations.address ELSE EXCLUDED.address END,
                updated_at = now()
            RETURNING id
            """,
            (
                organization_id,
                record["org_name"],
                record.get("organization_website"),
                organization_metadata.get("source_id"),
                organization_metadata.get("external_id"),
                organization_metadata.get("source_url"),
                organization_metadata.get("logo_url"),
                organization_metadata.get("areas_of_focus", []),
                organization_metadata.get("organization_type"),
                json.dumps(organization_metadata.get("address", {})),
            ),
        )
        org_row = cursor.fetchone()
        org_id = org_row["id"] if isinstance(org_row, dict) else org_row[0]
        cursor.execute(
            """
            INSERT INTO opportunities (
                opportunity_id, source_key, source_id, organization_id, title, description, category,
                neighborhood, lat, lng, commitment, availability, needed_skills, causes, base_urgency,
                source_url, source_last_seen_at, status, missed_source_runs
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s, 0
            )
            ON CONFLICT (source_key) DO UPDATE SET
                organization_id = EXCLUDED.organization_id,
                title = EXCLUDED.title,
                description = EXCLUDED.description,
                category = EXCLUDED.category,
                neighborhood = EXCLUDED.neighborhood,
                lat = EXCLUDED.lat,
                lng = EXCLUDED.lng,
                commitment = EXCLUDED.commitment,
                availability = EXCLUDED.availability,
                needed_skills = EXCLUDED.needed_skills,
                causes = EXCLUDED.causes,
                base_urgency = EXCLUDED.base_urgency,
                source_url = EXCLUDED.source_url,
                source_last_seen_at = now(),
                status = EXCLUDED.status,
                missed_source_runs = 0,
                updated_at = now()
            RETURNING opportunity_id
            """,
            (
                opportunity_id,
                source_key,
                record["source_id"],
                org_id,
                record["title"],
                record["description"],
                record["category"],
                record["neighborhood"],
                record["lat"],
                record["lng"],
                record["commitment"],
                record["availability"],
                record["needed_skills"],
                record["causes"],
                record["base_urgency"],
                record["source_url"],
                status,
            ),
        )
        opportunity_row = cursor.fetchone()
        opportunity_id = opportunity_row["opportunity_id"] if isinstance(opportunity_row, dict) else opportunity_row[0]
        if metadata:
            cursor.execute(
                """
                UPDATE opportunities
                SET
                    external_id = %s,
                    source_updated_at = %s,
                    source_first_published_at = %s,
                    expires_at = %s,
                    raw_description = %s,
                    source_payload = %s::jsonb,
                    address = %s::jsonb,
                    location_type = %s,
                    remote_zone = %s,
                    remote_country = %s,
                    directions = %s,
                    application_email = %s,
                    application_url = %s,
                    application_text = %s,
                    application_mode = %s,
                    provider_functions = %s,
                    areas_of_focus = %s,
                    times_of_day = %s,
                    expected_time = %s,
                    is_recurring = %s,
                    start_date = %s,
                    end_date = %s,
                    start_time = %s,
                    end_time = %s,
                    time_zone = %s,
                    welcome_groups = %s,
                    welcome_families = %s,
                    welcome_teens = %s,
                    welcome_international = %s,
                    welcome_age_55_plus = %s,
                    welcome_corporate_groups = %s,
                    minimum_age = %s,
                    orientation_required = %s,
                    background_check_required = %s,
                    drivers_license_required = %s,
                    wheelchair_accessible = %s,
                    training_provided = %s,
                    stipend_provided = %s,
                    fee_required = %s,
                    fee_amount = %s,
                    other_requirements = %s,
                    image_url = %s,
                    language = %s,
                    is_posted_anonymously = %s,
                    updated_at = now()
                WHERE opportunity_id = %s
                """,
                (
                    metadata.get("external_id"),
                    metadata.get("source_updated_at"),
                    metadata.get("source_first_published_at"),
                    metadata.get("expires_at"),
                    metadata.get("raw_description"),
                    json.dumps(metadata.get("source_payload", {})),
                    json.dumps(metadata.get("address", {})),
                    metadata.get("location_type"),
                    metadata.get("remote_zone"),
                    metadata.get("remote_country"),
                    metadata.get("directions"),
                    metadata.get("application_email"),
                    metadata.get("application_url"),
                    metadata.get("application_text"),
                    metadata.get("application_mode"),
                    metadata.get("provider_functions", []),
                    metadata.get("areas_of_focus", []),
                    metadata.get("times_of_day", []),
                    metadata.get("expected_time"),
                    metadata.get("is_recurring"),
                    metadata.get("start_date"),
                    metadata.get("end_date"),
                    metadata.get("start_time"),
                    metadata.get("end_time"),
                    metadata.get("time_zone"),
                    metadata.get("welcome_groups"),
                    metadata.get("welcome_families"),
                    metadata.get("welcome_teens"),
                    metadata.get("welcome_international"),
                    metadata.get("welcome_age_55_plus"),
                    metadata.get("welcome_corporate_groups"),
                    metadata.get("minimum_age"),
                    metadata.get("orientation_required"),
                    metadata.get("background_check_required"),
                    metadata.get("drivers_license_required"),
                    metadata.get("wheelchair_accessible"),
                    metadata.get("training_provided"),
                    metadata.get("stipend_provided"),
                    metadata.get("fee_required"),
                    metadata.get("fee_amount"),
                    metadata.get("other_requirements"),
                    metadata.get("image_url"),
                    metadata.get("language"),
                    metadata.get("is_posted_anonymously"),
                    opportunity_id,
                ),
            )
        return str(opportunity_id)


def get_sync_cursor(source_id: str) -> str | None:
    with psycopg.connect(_require_database(), row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT cursor_value FROM source_sync_state WHERE source_id = %s", (source_id,))
            row = cursor.fetchone()
            return row["cursor_value"] if row else None


def mark_sync_started(source_id: str) -> None:
    with psycopg.connect(_require_database()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO source_sync_state (source_id, last_started_at, last_status)
                VALUES (%s, now(), 'running')
                ON CONFLICT (source_id) DO UPDATE SET
                    last_started_at = now(), last_status = 'running', last_error = NULL, updated_at = now()
                """,
                (source_id,),
            )


def mark_sync_finished(source_id: str, cursor_value: str | None, seen: int, upserted: int) -> None:
    with psycopg.connect(_require_database()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO source_sync_state (
                    source_id, cursor_value, last_started_at, last_completed_at, last_status, records_seen, records_upserted
                ) VALUES (%s, %s, now(), now(), 'succeeded', %s, %s)
                ON CONFLICT (source_id) DO UPDATE SET
                    cursor_value = EXCLUDED.cursor_value,
                    last_completed_at = now(), last_status = 'succeeded', last_error = NULL,
                    records_seen = EXCLUDED.records_seen, records_upserted = EXCLUDED.records_upserted, updated_at = now()
                """,
                (source_id, cursor_value, seen, upserted),
            )


def mark_sync_failed(source_id: str, error: str) -> None:
    with psycopg.connect(_require_database()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO source_sync_state (source_id, last_started_at, last_completed_at, last_status, last_error)
                VALUES (%s, now(), now(), 'failed', %s)
                ON CONFLICT (source_id) DO UPDATE SET
                    last_completed_at = now(), last_status = 'failed', last_error = EXCLUDED.last_error, updated_at = now()
                """,
                (source_id, error[:1_000]),
            )


def set_opportunity_status(source_key: str, status: str) -> None:
    with psycopg.connect(_require_database()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE opportunities SET status = %s, updated_at = now() WHERE source_key = %s",
                (status, source_key),
            )


def purge_source_catalog(source_id: str) -> int:
    """Remove a source's catalog rows while retaining its import audit history."""
    with psycopg.connect(_require_database()) as connection:
        with connection.cursor() as cursor:
            # Staging records are retained as an audit trail, but their link to
            # a promoted catalog row must be cleared before that row is removed.
            cursor.execute(
                """
                UPDATE opportunity_staging
                SET promoted_opportunity_id = NULL
                WHERE promoted_opportunity_id IN (
                    SELECT opportunity_id FROM opportunities WHERE source_id = %s
                )
                """,
                (source_id,),
            )
            cursor.execute("DELETE FROM opportunities WHERE source_id = %s", (source_id,))
            removed = cursor.rowcount
            cursor.execute(
                """
                DELETE FROM organizations AS org
                WHERE org.source_id = %s
                  AND NOT EXISTS (SELECT 1 FROM opportunities AS opp WHERE opp.organization_id = org.id)
                """,
                (source_id,),
            )
    return removed


def save_staged_record(
    connection: Any,
    import_id: str,
    record: dict[str, Any],
    review_status: str,
    rejection_reason: str | None = None,
    promoted_opportunity_id: str | None = None,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO opportunity_staging (
                id, import_id, source_record_key, candidate, confidence, review_status,
                rejection_reason, promoted_opportunity_id, reviewed_at
            ) VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, CASE WHEN %s = 'pending' THEN NULL ELSE now() END)
            ON CONFLICT (import_id, source_record_key) DO UPDATE SET
                candidate = EXCLUDED.candidate,
                confidence = EXCLUDED.confidence,
                review_status = EXCLUDED.review_status,
                rejection_reason = EXCLUDED.rejection_reason,
                promoted_opportunity_id = EXCLUDED.promoted_opportunity_id,
                reviewed_at = EXCLUDED.reviewed_at
            """,
            (
                str(uuid.uuid4()),
                import_id,
                record["source_key"],
                json.dumps(record),
                record["confidence"],
                review_status,
                rejection_reason,
                promoted_opportunity_id,
                review_status,
            ),
        )


def save_import_records(
    import_id: str,
    approved_records: list[dict[str, Any]],
    pending_records: list[tuple[dict[str, Any], str]],
) -> int:
    """Stage every parsed record and promote only parser-approved records."""
    _require_database()
    promoted = 0
    with psycopg.connect(database_url()) as connection:
        for record in approved_records:
            opportunity_id = upsert_opportunity(connection, record)
            save_staged_record(connection, import_id, record, "auto_approved", promoted_opportunity_id=opportunity_id)
            promoted += 1
        for record, reason in pending_records:
            save_staged_record(connection, import_id, record, "pending", rejection_reason=reason)
    return promoted


def reconcile_source(source_id: str, seen_source_keys: list[str]) -> int:
    """Pause stale roles only after two successful, non-empty source runs."""
    if not seen_source_keys:
        return 0
    with psycopg.connect(_require_database()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE opportunities
                SET
                    missed_source_runs = missed_source_runs + 1,
                    status = CASE WHEN missed_source_runs + 1 >= 2 THEN 'paused' ELSE status END,
                    updated_at = now()
                WHERE source_id = %s
                  AND status = 'active'
                  AND NOT (source_key = ANY(%s))
                """,
                (source_id, seen_source_keys),
            )
            return cursor.rowcount
