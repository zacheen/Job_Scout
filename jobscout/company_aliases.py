"""Hand-maintained company-name aliases: aggregator spelling -> canonical name.

Multi-employer aggregators (Simplify / SpeedyApply) self-report employer names; a
spelling that differs from the native fetcher's splits one posting into two ledger
rows, because job_key embeds the company name. The aggregator fetchers pass every
parsed employer name through `canonical_company` at Job construction. Native
fetchers never consult this map — their names come from companies.yaml, which IS
the canonical spelling.

No code maintains this file; extend the dict by hand when the dropped-jobs audit
(audit_dropped.py) or a duplicate row reveals a new spelling mismatch. Rules:
- If the company has a companies.yaml entry, the canonical name MUST be that
  entry's `name` (referral grouping and ledger shard files key on it).
- Aggregator-only pairs may standardize on either spelling.
- Matching is case-insensitive and ignores surrounding whitespace; the mapped
  value is used verbatim.
- Short generic aliases ("Plus", "Apex") also catch any OTHER company that spells
  its name exactly that way — keep entries as specific as the data allows.
- Deliberately NOT aliased: Alarm.com/OpenEye and SRA Internships/Samsung Research
  America — those are overlapping NATIVE greenhouse boards of related but distinct
  entities, and this map is never applied to native fetchers anyway.

This map is only half the defence. It fixes spellings BEFORE a row exists, for the case
where the two sources share no URL and job_key is the only thing that could match them.
Once a row does merge, store._merge_company re-picks the employer name from the uids,
preferring whichever spelling a native fetcher reported — so an unaliased variant no
longer makes the row migrate between shards; only the unmergeable duplicates above still
need an entry here. Both mechanisms rely on aggregator names in companies.yaml never
colliding with a real employer's self-reported name.
"""

# Seeded 2026-08-04 from the 16 aggregator-involved cross-name duplicate groups
# found in the cloud ledger.
COMPANY_ALIASES: dict[str, str] = {
    # Companies with a native fetcher: right side = companies.yaml `name`.
    "1X": "1X Technologies",
    "Etched.ai": "Etched",
    "Gritt Robotics Inc": "Gritt Robotics",
    "Nissan Global": "Nissan",
    "Perplexity AI": "Perplexity",
    "Plus": "PlusAI",
    "Rivian and Volkswagen Group Technologies": "Rivian",
    "Saronic Technologies": "Saronic",
    # Aggregator-only spelling variants.
    "Adaptive": "Adaptive Security",
    "Apex": "Apex Technology",
    "Astera": "Astera Institute",
    "Base Power Company": "Base Power",
    "Binance.US": "Binance",
    "Cybernetic Labs": "Netic",
    "Droyd Robotics": "Droyd",
    "Fab2": "Atomic Semi",
    "MyJunior AI": "Junior AI",
    "Solva Technology": "Solva",
}

_LOOKUP = {alias.strip().casefold(): name for alias, name in COMPANY_ALIASES.items()}


def canonical_company(name: str) -> str:
    """`name` mapped through COMPANY_ALIASES (case-insensitive), else unchanged.
    None-safe (returns ""): aggregator JSON can carry explicit nulls, and this runs
    BEFORE Job.__post_init__'s centralized None coercion can catch them."""
    if not name:
        return ""
    return _LOOKUP.get(name.strip().casefold(), name)
