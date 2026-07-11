# Opportunity ingestion on DigitalOcean

Tenderly's opportunity catalog is designed to update automatically without allowing an AI model to quietly publish unverified roles.

## What runs every six hours

The `opportunity-ingestion` DigitalOcean App Platform job runs at minute zero, every six hours, in the `America/Los_Angeles` time zone. The schedule is defined in [`.do/app.yaml`](../.do/app.yaml).

```text
Approved source → fetch → content hash → DigitalOcean AI parser
                → validation → staging/audit tables → active opportunities
```

- An unchanged source hash skips the model call. The hash includes the parser policy, so a changed extraction or validation policy safely reprocesses the same page.
- Invalid records never enter matching.
- Records below the source confidence threshold remain in `opportunity_staging` for a human to review.
- A valid, high-confidence role is upserted into `opportunities` and is available to the matching API immediately.
- A role disappears from two successful, non-empty source runs before Tenderly pauses it. A temporary source failure cannot remove roles.

The scheduled job currently refreshes Tenderly's hackathon SF-only starter roster of 15 first-party public volunteer sources. Each candidate must pass the evidence, domain, confidence, and SF-location checks before it is visible to matching. Sources that block automated retrieval remain disabled until they offer a feed or permission.

## Source configuration

[`data/opportunity_sources.json`](../data/opportunity_sources.json) is the single, readable source registry. Each entry explains its organization, URL allowlist, default location/category values, confidence threshold, and activation condition.

The current live roster includes SF Rec & Park, Golden Gate National Parks Conservancy, SF-Marin Food Bank, SF Environment, SF.gov, GLIDE, Project Open Hand, St. Anthony Foundation, 826 Valencia, SF SPCA, SF Animal Care & Control, Friends of the Urban Forest, Meals on Wheels SF, Shanti Project, and the SF AIDS Foundation. The source registry is the authoritative, reviewed list.

Representative sources:

| Source | Type | Status | Why it is useful |
| --- | --- | --- | --- |
| SF Rec & Park | Volunteer calendar | Enabled | Dated SF work parties and park shifts |
| Golden Gate Parks Conservancy | Volunteer events | Enabled | SF conservation and stewardship roles |
| SF-Marin Food Bank | Volunteer calendar | Enabled | Food-security shifts with time and capacity |
| 826 Valencia | Static volunteer page | Enabled | Education and youth tutoring roles |
| GLIDE | Static volunteer page | Enabled | Food security and homelessness roles |
| Partner JSON feed template | JSON API | Ready to configure | Preferred path for scalable partner integrations |

SF 311 is not an opportunity source. It remains a separate live community-needs input for ranking and surge simulation.

### VolunteerConnector direct import

`python -m app.volunteerconnector_import` imports the public VolunteerConnector API without an API key or LLM. It preserves provider-supplied role, organization, activity, duration, dates, application URL, and location metadata. Listings outside Tenderly's 50-mile SF service area, as well as regional and remote-only listings, are filtered out before catalog storage rather than assigned a guessed location or presented as SF recommendations.

VolunteerConnector is a national/Canadian supplemental catalog, not a replacement for SF-only verified sources. Its listings retain their true locations and must never be represented as San Francisco opportunities.

### Enable an approved source

1. Get approval from the nonprofit or use its documented API/feed.
2. Confirm the source URL and every hostname in `allowed_domains`.
3. Set that source's `enabled` field to `true`.
4. Commit and deploy. The next scheduled run discovers it automatically.
5. Check App Platform job logs and the staging table after the first run.

For a JSON or Airtable-based partner feed, copy `partner_json_feed_template`, give it a unique `id`, set its real HTTPS URL and allowlist, then enable it. JSON feeds are preferable to HTML because their data is less ambiguous and less likely to change page structure.

## AI parser configuration

[`data/opportunity_parser.json`](../data/opportunity_parser.json) keeps the model choice, temperature, allowed categories, required fields, and promotion policy in plain language.

The ingestion job calls DigitalOcean Serverless Inference through the existing `GRADIENT_MODEL` setting. Its job is strictly extraction and normalization into JSON. It does not score volunteers, choose recommendations, or make live matching decisions.

The parser receives only public source content and source defaults. It must provide a per-record confidence score, a verbatim evidence quote found in the fetched source, and cannot introduce a link outside the source's configured allowlist. Evidence-verified records promote at the catalog-wide 0.90 confidence threshold.

## Database tables

| Table | Purpose |
| --- | --- |
| `organizations` | One row per nonprofit organization |
| `opportunities` | Active catalog read by the matching API |
| `opportunity_imports` | Raw source snapshot, content hash, parser result, and errors |
| `opportunity_staging` | Valid but pending, auto-approved, or rejected records |

The idempotent schema is in [`db/migrations/001_opportunity_catalog.sql`](../db/migrations/001_opportunity_catalog.sql). The `db-migrate` pre-deploy job applies it and seeds the curated local catalog once, so a new production database does not start empty.

## Local verification

Use these commands after configuring a development PostgreSQL `DATABASE_URL`:

```bash
python -m app.migrate
OPPORTUNITY_INGESTION_ENABLED=true python -m app.ingestion_job
```

Without `DATABASE_URL`, the public API safely continues to read [`data/opportunities.json`](../data/opportunities.json), which keeps frontend development and the hackathon demo working.

## DigitalOcean setup

The App Platform spec creates/binds a Managed PostgreSQL component named `tenderly-db` and passes its generated `DATABASE_URL` to the API, migration job, and ingestion job. Do not hardcode a database password.

The scheduled job configuration follows DigitalOcean's documented `SCHEDULED` job format and costs only while it runs. See [DigitalOcean’s scheduled-job guide](https://docs.digitalocean.com/products/app-platform/how-to/manage-jobs/) and [database environment-variable guide](https://docs.digitalocean.com/products/app-platform/how-to/use-environment-variables/).
