# Experiment: grants in the same Postgres (design note)

Status: proposal only. Nothing here is implemented, and `find_grants.py` and the
local `b4p` database were only read.

## What exists today

`/Users/vishnavramesh/blueprints_grants/find_grants.py` is a standalone script:

1. Searches the Grants.gov Search2 API for 4 default keywords ("medical
   supplies", "medical surplus", "global health nonprofit", "humanitarian aid
   supply chain"), restricted to posted opportunities open to nonprofits.
2. Fetches each opportunity's detail (agency, synopsis, award ceiling).
3. Optionally asks Gemini 2.0 Flash for a relevance verdict (needs
   `GEMINI_API_KEY`), otherwise leaves `relevant` NULL.
4. Upserts into `grant_opportunity` in the **old local** `b4p` database with
   psycopg2 and prints a digest (new items flagged, soonest deadline first).

Shape of `grant_opportunity` (read-only inspection): `opp_number` (PK), `opp_id`,
`title`, `agency`, `close_date`, `award_ceiling`, `synopsis`, `matched_query`,
`relevant` (NULL = not screened), `relevance_reason`, `first_seen`, `last_seen`.
At the time of writing it holds 138 rows, none screened yet, 82 with a close date
today or later (one placeholder deadline of 2099-01-01).

Gaps: it only runs on one laptop, the upsert never updates title/synopsis or the
relevance verdict, nothing records what we decided about a grant, and the data is
not visible to anyone else.

## Proposal: a `grants` schema next to the inventory tables

Same database (local `b4p_v2`, Supabase in production), separate schema so the
dashboard contract in `public` is untouched and grants can be dropped or
permissioned independently.

```sql
CREATE SCHEMA IF NOT EXISTS grants;

CREATE TABLE grants.opportunity (          -- what find_grants.py writes today
    opp_number TEXT PRIMARY KEY, opp_id BIGINT NOT NULL, title TEXT NOT NULL,
    agency TEXT, close_date DATE, award_ceiling NUMERIC, synopsis TEXT,
    matched_query TEXT, relevant BOOLEAN, relevance_reason TEXT,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE grants.application (          -- what WE do with it (new)
    application_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    opp_number TEXT NOT NULL REFERENCES grants.opportunity(opp_number),
    status TEXT NOT NULL DEFAULT 'Considering'
           CHECK (status IN ('Considering','Drafting','Submitted','Awarded','Declined','Passed')),
    owner TEXT, amount_requested NUMERIC, amount_awarded NUMERIC,
    submitted_at DATE, notes TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE VIEW grants.v_open AS               -- the digest, as a view
SELECT o.*, a.status FROM grants.opportunity o
LEFT JOIN grants.application a USING (opp_number)
WHERE (o.close_date IS NULL OR o.close_date >= CURRENT_DATE)
  AND COALESCE(o.relevant, true) AND COALESCE(a.status, 'Considering') <> 'Passed'
ORDER BY o.close_date NULLS LAST;
```

Steps, in order of risk:

1. Add `sql/grants.sql` (applied by `python -m b4p init --with-grants`, or a
   separate `python -m b4p grants-init`), so volunteers who do not do grants never
   get the schema.
2. Move the script into the package as `b4p/grants.py` (`python -m b4p grants
   fetch`), switching psycopg2 to the psycopg 3 helper in `b4p/db.py` and the
   upsert to also refresh title/synopsis. Its only external dependency is the
   public Grants.gov API; the Gemini key stays optional and the screen should use
   the cheapest model that works (Flash is already that).
3. One-time copy of the 138 rows from the old `b4p` into `grants.opportunity`.
4. Run it on a weekly GitHub Actions schedule (separate workflow from the 15 minute
   inventory sync) and print the new, relevant grants in the run summary.
5. Supabase: keep the schema unexposed in the API settings, so the dashboards
   (which only read `public.dash_*`) cannot see it. Access only through the
   database owner / a volunteer-facing read-only role.

## Can inventory and impact data help applications?

Yes, mostly through aggregates, and this is where sharing a database pays off.

- **Narrative numbers.** Funders ask "how much, of what, to whom". The
  `public_impact` view (units on hand, distinct products, category mix, units
  shipped, requests fulfilled, partner count) answers it with one query and can
  be stamped with a date, so a proposal's figures are reproducible.
- **Budget justification.** `v_kpis.total_value` plus `supply.unit_value_usd` give
  an in-kind value of goods handled. Only as good as the unit prices volunteers
  enter today (many are 0), so it should be shown as a floor, not a headline.
- **Matching grants to strengths.** Category totals (needles and syringes are about a
  third of units) could be fed to the relevance prompt so Gemini scores grants
  against what we actually stock, e.g. a sharps-safety or wound-care program.
- **Reporting after an award.** `grants.application` rows with a start/end date
  could drive a "units shipped during the award period" query, since shipments are
  timestamped.
- **Do not export per-organization data.** Grant reports should use counts of
  partner organizations, not names or emails, unless a partner has agreed. The
  requester tables stay out of any grants view.

Caveats: units shipped is currently 0 in the local copy because every request is
still "Under Review", so shipped and fulfilled numbers are only meaningful once
statuses are being moved to Shipped/Delivered. "Units shipped" counts requested
quantities, not what physically left the warehouse.

## Open questions

- Who owns grants day to day (the `owner` field and who may write to the schema)?
- Is Supabase the right home, or should grants stay local until someone uses them?
- Do we want Grants.gov only, or also foundation lists (these have no clean API and
  would need manual entry into `grants.opportunity`)?
