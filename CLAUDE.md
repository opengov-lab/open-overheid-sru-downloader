# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project shape

Single-file Python 3.10+ tool, stdlib only. Everything lives in `download_oo.py`. There is no build system, no dependencies file, no test suite, no linter config. Don't introduce any of those without being asked.

## Running

```bash
python3 download_oo.py gm1680                          # one gemeente, PDFs + JSON
python3 download_oo.py gm0363 --metadata-only          # JSON only
python3 download_oo.py --all-gemeenten --year 2025 --metadata-only
python3 download_oo.py --help                          # canonical flag list
```

Output goes to `./out/<gm_code>/` or `./out/all-gemeenten/` by default.

## Architecture notes

**SRU 2.0 over CQL.** All harvesting is one HTTP endpoint (`repository.overheid.nl/sru/`) queried with a CQL string built in `build_query_url`. The base predicate is `c.product-area=="officielepublicaties"`, AND'd with creator (resolved via TOOI for a single gemeente) or `w.organisatietype=="gemeente"` for `--all-gemeenten`, plus optional `dt.modified` bounds and `dt.type` filters.

**CQL `NOT` is binary, not unary.** `A AND NOT B` is a syntax error. The exclusion path in `build_query_url` chains `A NOT B NOT C` instead — left-associative `(A NOT B) NOT C`. Don't "fix" this to look like normal boolean logic.

**Resume key includes the filter set.** `_progress.json` stores `next_start` keyed by a JSON blob of `(creator, all, from, to, expand, types, exclude_types)`. Changing any filter invalidates the resume and starts from record 1 — this is intentional, not a bug.

**`--all-gemeenten` forces `--metadata-only`.** Downloading PDFs across every gemeente is terabytes; the guard in `main` rejects the combination. Don't relax this.

**Retry policy is asymmetric.** `http_get` (SRU pages) retries up to 6× on 429/503/5xx and any `OSError`, honouring `Retry-After`. `http_download` (PDFs) retries 5× on `HTTPError`/`OSError` only. 4xx other than 429 raise immediately. The 1.0 s inter-page sleep and `PAUSE` between PDF downloads exist because KOOP enforces a fair-use policy — don't drop them.

**Output dir disappearing mid-run is handled.** `os.makedirs(out_dir, exist_ok=True)` runs every page, and the JSON write has a `FileNotFoundError` fallback. Comment in code explains why; preserve it if refactoring the write path.

**XML parsing uses fixed namespaces.** The `NS` dict at module top covers SRU response, `gzd` (KOOP wrapper), `ow` (wetgeving kern/mantel), and Dublin Core terms. Records without `ow:owmskern` or without a `dt:identifier` are skipped silently.

## Conventions

- User-Agent string `UA` includes a contact email — keep it set on every `Request`.
- `PAGE_SIZE = 1000` is the SRU 2.0 maximum; don't raise it.
- Type filter values are exact rubriek names from the SRU manual's bijlage; KOOP normalises legacy spellings server-side, so callers don't enumerate variants.
