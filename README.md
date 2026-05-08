# open-overheid-sru-downloader

Single-file Python tool to harvest publications from
[open.overheid.nl](https://open.overheid.nl/) via KOOP's SRU 2.0 endpoint
(`repository.overheid.nl/sru/`), the canonical source behind the portal.

Targets one gemeente or every gemeente; bound by year/date and document type.
Stdlib only, Python 3.10+.

## Quick start

```bash
python3 download_oo.py gm1680                   # gemeente Aa en Hunze, all years, PDFs + JSON
python3 download_oo.py gm0363 --metadata-only   # Amsterdam, JSON only
```

The TOOI gemeente code (`gm<CBS-code>`) resolves to the gemeente's name via
`https://identifier.overheid.nl/tooi/id/gemeente/gm<code>`. `gm1680` is Aa en Hunze.

## Filtering by document type

`dt.type` carries the rubriek (omgevingsvergunning, beleidsregel, …). Two flags
thread the filter into the SRU CQL query:

| Flag | Effect |
|---|---|
| `--type "A,B"` | whitelist: keep only rubrieken `A` or `B` |
| `--exclude-type "A,B"` | blacklist: drop rubrieken `A` and `B` |

The two are mutually exclusive. Both accept comma-separated values or repeated
flags. Values containing spaces or parentheses must be quoted in the shell:

```bash
# Only verordeningen and beleidsregels
python3 download_oo.py gm1680 \
    --type "beleidsregel,algemeen verbindend voorschrift (verordening)"

# Equivalent, repeated flag form
python3 download_oo.py gm1680 \
    --type "beleidsregel" \
    --type "algemeen verbindend voorschrift (verordening)"

# Everything except permit traffic
python3 download_oo.py gm1680 \
    --exclude-type "omgevingsvergunning,omgevingsmelding,andere vergunning"
```

KOOP normalises legacy rubriek names to their modern equivalents per the
"herindeling rubricering" appendix of the SRU 2.0 manual, so `--type
"beleidsregel"` also matches historical `Beleidsregels` records — you don't
need to enumerate both spellings.

### Finding the right rubriek names

The full canonical list is in the manual's bijlage. To see the actual rubrieken
*and counts* for any scope, ask SRU directly with `facetLimit=100:dt.type`:

```bash
curl -s 'https://repository.overheid.nl/sru/?query=c.product-area=="officielepublicaties" AND dt.creator=="Aa en Hunze"&maximumRecords=0&facetLimit=100:dt.type' | xmllint --format -
```

For reference, the share of common rubrieken across **all gemeenten in 2025**:

| Type | Share |
|---|---|
| omgevingsvergunning | 57% |
| andere vergunning | 11% |
| evenementenvergunning | 6% |
| andere beschikking | 4% |
| verkeersbesluit of -mededeling | 4% |
| omgevingsmelding | 3% |
| algemeen verbindend voorschrift (verordening) | 2% |
| beleidsregel | 1% |
| 25+ smaller rubrieken | <1% each |

## Combining with date and scope filters

All filters AND together. Some practical combinations:

```bash
# All 2025 verordeningen from one gemeente, PDFs included
python3 download_oo.py gm1680 --year 2025 \
    --type "algemeen verbindend voorschrift (verordening)"

# Same thing for a date range
python3 download_oo.py gm1680 \
    --from 2024-01-01 --to 2024-06-30 \
    --type "beleidsregel"

# Policy documents across every gemeente, 2025 (metadata-only required)
python3 download_oo.py --all-gemeenten --year 2025 --metadata-only \
    --type "beleidsregel,algemeen verbindend voorschrift (verordening),delegatie- of mandaatbesluit"

# Drop the noisy permit traffic
python3 download_oo.py gm1680 --year 2025 \
    --exclude-type "omgevingsvergunning,omgevingsmelding,andere vergunning,evenementenvergunning,andere beschikking,andere melding,exploitatievergunning"
```

## Other flags

| Flag | Purpose |
|---|---|
| `--all-gemeenten` | harvest every gemeente (`w.organisatietype=="gemeente"`). Requires `--metadata-only`. |
| `--out PATH` | output directory. Default `./out/<gm_code>` or `./out/all-gemeenten`. |
| `--metadata-only` | write JSON only; `manifestations.pdf` in the JSON still points to the canonical PDF URL. |
| `--year YYYY` | shortcut for `--from YYYY-01-01 --to YYYY-12-31`. |
| `--from YYYY-MM-DD` / `--to YYYY-MM-DD` | bounds on `dt.modified` (inclusive). |
| `--expand` | enable SRU knowledge-model expansion. For gemeenten this broadens the result well beyond predecessor organisations; only useful for fusiegemeenten. |
| `--start-record N` | manual resume override. |
| `--max-docs N` | stop after N documents (smoke testing). |

`python3 download_oo.py --help` prints the canonical list.

## Output

Per document, in `<out>/`:

- `<identifier>.json` — parsed metadata (title, creator, type, modified date,
  subject, publisher, language) plus a `manifestations` map with URLs to every
  format KOOP publishes (`pdf`, `html`, `odt`, `xml`, `metadata`,
  `metadataowms`).
- `<identifier>.pdf` — the PDF, unless `--metadata-only` is set.

`_progress.json` is written between pages while a run is in flight and removed
on success.

## Resuming after a crash

The script writes `_progress.json` after every completed page (1000 records).
Re-running with the same args picks up where it left off:

```
resuming from startRecord=409001 (per ./out/all-gemeenten/_progress.json)
```

For one-time recovery without `_progress.json`, use `--start-record N`.
The progress key incorporates the filter args, so changing `--type`,
`--from`/`--to`, etc. invalidates the previous progress and starts fresh.

## Network behaviour

- Page size: 1000 records per SRU call (the SRU 2.0 maximum).
- Inter-page pause: 1.0 s. KOOP enforces a fair-use policy on the SRU service.
- Retries: up to 6 attempts with exponential backoff (5–120 s), honouring
  `Retry-After` on 503/429. Covers `HTTPError`, `TimeoutError`,
  `ConnectionError`, and other transient `OSError`s.
- Read timeout: 180 s for SRU pages, 300 s for PDF downloads.

For sustained mirrors of all-gemeente publications, the daily eventfile feed
at `repository.officiele-overheidspublicaties.nl/officielepublicaties/_events/YYYY-MM-DD.xml`
is more efficient than re-running SRU harvests.

## References

- [SRU 2.0 handleiding (KOOP)](https://standaarden.overheid.nl/sru) — index
  names, CQL syntax, rubriek-herindeling appendix.
- [TOOI](https://identifier.overheid.nl/tooi/) — gemeente / provincie /
  ministerie identifiers.
- [open.overheid.nl](https://open.overheid.nl/) — the public-facing portal.
