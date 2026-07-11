import os
import pathlib

from dotenv import load_dotenv

load_dotenv()

GRADIENT_API_KEY = os.environ.get("GRADIENT_API_KEY", "")
GRADIENT_MODEL = os.environ.get("GRADIENT_MODEL", "llama-4-maverick")
GRADIENT_BASE_URL = os.environ.get("GRADIENT_BASE_URL", "https://inference.do-ai.run/v1")

# Opportunity catalog / ingestion settings. The public API still uses the
# committed seed catalog locally; production switches to Managed PostgreSQL
# when DATABASE_URL is configured by DigitalOcean App Platform.
DATABASE_URL = os.environ.get("DATABASE_URL", "")
OPPORTUNITY_INGESTION_ENABLED = os.environ.get("OPPORTUNITY_INGESTION_ENABLED", "false").lower() == "true"
OPPORTUNITY_INGESTION_SOURCE_IDS = {
    source_id.strip()
    for source_id in os.environ.get("OPPORTUNITY_INGESTION_SOURCE_IDS", "").split(",")
    if source_id.strip()
}
OPPORTUNITY_IMPORT_TIMEOUT_SECONDS = float(os.environ.get("OPPORTUNITY_IMPORT_TIMEOUT_SECONDS", "20"))
OPPORTUNITY_IMPORT_USER_AGENT = os.environ.get(
    "OPPORTUNITY_IMPORT_USER_AGENT", "TenderlyOpportunityBot/1.0 (contact: hello@tenderly.app)"
)

SOCRATA_BASE_URL = os.environ.get(
    "SOCRATA_BASE_URL", "https://data.sfgov.org/resource/vw6y-z8j6.json"
)
NEEDS_CACHE_TTL_SECONDS = int(os.environ.get("NEEDS_CACHE_TTL_SECONDS", "600"))

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
OPPORTUNITIES_PATH = DATA_DIR / "opportunities.json"
NEEDS_SNAPSHOT_PATH = DATA_DIR / "needs_snapshot.json"
OPPORTUNITY_SOURCES_PATH = DATA_DIR / "opportunity_sources.json"
OPPORTUNITY_PARSER_PATH = DATA_DIR / "opportunity_parser.json"
OPPORTUNITY_MIGRATIONS_DIR = BASE_DIR / "db" / "migrations"

PORT = int(os.environ.get("PORT", "8080"))
