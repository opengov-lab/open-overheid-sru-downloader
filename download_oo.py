#!/usr/bin/env python3
"""
Download all officiele-publicaties documents published by a given gemeente.

Uses KOOP's SRU 2.0 endpoint (repository.overheid.nl/sru/), the canonical
source behind open.overheid.nl. Pulls every PDF + metadata sidecar.

Usage:
    python download_oo.py gm1680                          # gemeente Aa en Hunze, all years
    python download_oo.py gm0363 --out amsterdam
    python download_oo.py gm1680 --metadata-only          # JSON only; manifestations.pdf points to PDF
    python download_oo.py gm1680 --year 2025              # all 2025 publications
    python download_oo.py gm1680 --from 2024-01-01 --to 2024-06-30
    python download_oo.py gm1680 --expand                 # broaden via knowledge model
                                                          # (warning: for gemeenten this
                                                          #  pulls in unrelated national
                                                          #  / waterschap content; only
                                                          #  enable for fusiegemeenten)
    python download_oo.py --all-gemeenten --year 2025 --metadata-only
                                                          # all 342 gemeenten, 2025
                                                          # (~560k records, metadata only)

Date filter uses dt.modified (matches the gemeente example in the SRU 2.0 manual).
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from xml.etree import ElementTree as ET

SRU = "https://repository.overheid.nl/sru/"
TOOI = "https://identifier.overheid.nl/tooi/id/gemeente"
UA = "open-overheid-sru-downloader/2.0 (+contact: dpgraus@gmail.com)"
PAGE_SIZE = 1000  # SRU max
PAUSE = 0.05      # between requests; bump up if KOOP throttles

NS = {
    "sru": "http://docs.oasis-open.org/ns/search-ws/sruResponse",
    "gzd": "http://standaarden.overheid.nl/sru",
    "ow":  "http://standaarden.overheid.nl/wetgeving/",
    "dt":  "http://purl.org/dc/terms/",
    "c":   "http://standaarden.overheid.nl/collectie/",
    "overheid": "http://standaarden.overheid.nl/owms/terms/",
    "diag": "http://docs.oasis-open.org/ns/search-ws/diagnostic",
}


def http_get(url: str, *, retries: int = 6, timeout: int = 180) -> bytes:
    """GET with retries. Honors Retry-After on 503/429; longer backoff than http_download.

    OSError covers URLError (subclass), TimeoutError (read/connect timeouts), and
    ConnectionError — all transient network errors we should retry."""
    req = Request(url, headers={"User-Agent": UA})
    last_err = None
    for attempt in range(retries):
        try:
            with urlopen(req, timeout=timeout) as r:
                return r.read()
        except HTTPError as e:
            last_err = e
            if e.code in (429, 503):
                ra = e.headers.get("Retry-After") if e.headers else None
                try:
                    wait = float(ra) if ra else 5 * (2 ** attempt)
                except ValueError:
                    wait = 5 * (2 ** attempt)
                wait = min(wait, 120)
            elif 500 <= e.code < 600:
                wait = min(5 * (2 ** attempt), 120)
            else:
                raise  # 4xx other than 429: don't retry
            print(f"  ! http {e.code} on {url[:90]}... retry in {wait:.0f}s "
                  f"(attempt {attempt+1}/{retries})", file=sys.stderr)
            time.sleep(wait)
        except OSError as e:
            last_err = e
            wait = min(5 * (2 ** attempt), 120)
            print(f"  ! {type(e).__name__}: {e}; retry in {wait:.0f}s "
                  f"(attempt {attempt+1}/{retries})", file=sys.stderr)
            time.sleep(wait)
    raise last_err


def http_download(url: str, dest: str, *, retries: int = 5) -> None:
    req = Request(url, headers={"User-Agent": UA})
    for attempt in range(retries):
        try:
            with urlopen(req, timeout=300) as r, open(dest + ".part", "wb") as f:
                while chunk := r.read(64 * 1024):
                    f.write(chunk)
            os.replace(dest + ".part", dest)
            return
        except (HTTPError, OSError) as e:
            if attempt == retries - 1:
                raise
            wait = min(5 * (2 ** attempt), 120)
            print(f"  ! {type(e).__name__} downloading {url[:80]}; "
                  f"retry in {wait:.0f}s (attempt {attempt+1}/{retries})",
                  file=sys.stderr)
            time.sleep(wait)


def resolve_gemeente(gm_code: str) -> tuple[str, str]:
    """gm1680 -> ('Aa en Hunze', 'gemeente Aa en Hunze')"""
    data = json.loads(http_get(f"{TOOI}/{gm_code}"))
    node = (data[0] if isinstance(data, list) else data)["@graph"][0]
    bare = node["https://identifier.overheid.nl/tooi/def/ont/officieleNaamExclSoort"][0]["@value"]
    full = node["https://identifier.overheid.nl/tooi/def/ont/officieleNaamInclSoort"][0]["@value"]
    return bare, full


def build_query_url(*, creator: str | None, all_gemeenten: bool, start: int,
                    expand: bool, date_from: str | None, date_to: str | None,
                    types: list[str] | None, exclude_types: list[str] | None) -> str:
    cql = 'c.product-area=="officielepublicaties"'
    if all_gemeenten:
        cql += ' AND w.organisatietype=="gemeente"'
    elif creator:
        cql += f' AND dt.creator=="{creator}"'
    if date_from:
        cql += f' AND dt.modified>={date_from}'
    if date_to:
        cql += f' AND dt.modified<={date_to}'
    if types:
        clauses = ' OR '.join(f'dt.type=="{t}"' for t in types)
        cql += f' AND ({clauses})'
    if exclude_types:
        # CQL's NOT is a binary "and-not" operator, not unary. So `A AND NOT B`
        # is a syntax error — write `A NOT B` instead. Multiple exclusions chain
        # left-associatively: `A NOT B NOT C` == `(A NOT B) NOT C`.
        for t in exclude_types:
            cql += f' NOT dt.type=="{t}"'
    qs = (
        f"query={quote(cql, safe='')}"
        f"&maximumRecords={PAGE_SIZE}"
        f"&startRecord={start}"
    )
    if expand:
        qs += "&x-info-1-accept=expand"
    return f"{SRU}?{qs}"


def parse_records(xml_bytes: bytes):
    """Yield (identifier, metadata_dict, pdf_url|None) for each record."""
    root = ET.fromstring(xml_bytes)
    diag = root.find(".//diag:diagnostic/diag:message", NS)
    if diag is not None:
        raise RuntimeError(f"SRU diagnostic: {diag.text}")
    total_el = root.find("sru:numberOfRecords", NS)
    total = int(total_el.text) if total_el is not None else 0
    for rec in root.findall(".//sru:record", NS):
        kern = rec.find(".//ow:owmskern", NS)
        mantel = rec.find(".//ow:owmsmantel", NS)
        if kern is None:
            continue
        ident_el = kern.find("dt:identifier", NS)
        ident = ident_el.text if ident_el is not None else None
        if not ident:
            continue
        meta = {"identifier": ident}
        for tag in ("title", "type", "language", "creator", "modified"):
            el = kern.find(f"dt:{tag}", NS)
            if el is not None and el.text:
                meta[tag] = el.text
        if mantel is not None:
            for tag in ("date", "available", "issued", "subject", "publisher"):
                el = mantel.find(f"dt:{tag}", NS)
                if el is not None and el.text:
                    meta[tag] = el.text
            hv = mantel.find("dt:hasVersion", NS)
            if hv is not None:
                meta["resourceIdentifier"] = hv.get("resourceIdentifier")

        manifestations = {}
        for item in rec.findall(".//gzd:itemUrl", NS):
            m = item.get("manifestation")
            if m and item.text:
                manifestations[m] = item.text.strip()
        meta["manifestations"] = manifestations
        pref = rec.find(".//gzd:preferredUrl", NS)
        if pref is not None and pref.text:
            meta["preferredUrl"] = pref.text.strip()

        yield ident, meta, manifestations.get("pdf"), total


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("gm_code", nargs="?", default=None,
                   help="TOOI gemeente code, e.g. gm1680. Omit when using --all-gemeenten.")
    p.add_argument("--all-gemeenten", action="store_true",
                   help="harvest publications from every gemeente (w.organisatietype==\"gemeente\"). "
                        "Requires --metadata-only; use --year/--from/--to to bound the volume.")
    p.add_argument("--out", default=None,
                   help="output directory (default: ./out/<gm_code> or ./out/all-gemeenten)")
    p.add_argument("--metadata-only", action="store_true",
                   help="write JSON metadata only; the JSON's manifestations.pdf field "
                        "still points to the canonical PDF URL on repository.overheid.nl")
    p.add_argument("--year", type=int, default=None,
                   help="restrict to a calendar year (shortcut for --from YYYY-01-01 --to YYYY-12-31)")
    p.add_argument("--from", dest="date_from", default=None, metavar="YYYY-MM-DD",
                   help="lower bound on dt.modified (inclusive)")
    p.add_argument("--to", dest="date_to", default=None, metavar="YYYY-MM-DD",
                   help="upper bound on dt.modified (inclusive)")
    p.add_argument("--expand", action="store_true",
                   help="enable SRU knowledge-model expansion (x-info-1-accept=expand). "
                        "For gemeenten this broadens the result far beyond predecessor "
                        "organisations and is rarely what you want; off by default.")
    p.add_argument("--type", dest="types", action="append", default=None, metavar="TYPE",
                   help="whitelist dt.type values; comma-separated or repeat the flag. "
                        "Use exact rubriek names from the manual's bijlage, e.g. "
                        "\"beleidsregel,algemeen verbindend voorschrift (verordening)\".")
    p.add_argument("--exclude-type", dest="exclude_types", action="append", default=None, metavar="TYPE",
                   help="blacklist dt.type values; comma-separated or repeat the flag. "
                        "Mutually exclusive with --type. e.g. \"omgevingsvergunning,omgevingsmelding\".")
    p.add_argument("--max-docs", type=int, default=None, help="stop after N documents (smoke testing)")
    p.add_argument("--start-record", type=int, default=None,
                   help="manual resume: SRU startRecord to begin from "
                        "(overrides _progress.json; useful after a crash on a run that "
                        "predates auto-resume)")
    args = p.parse_args()

    if bool(args.gm_code) == bool(args.all_gemeenten):
        print("error: pass exactly one of <gm_code> or --all-gemeenten", file=sys.stderr)
        return 2
    if args.gm_code and (not args.gm_code.startswith("gm") or not args.gm_code[2:].isdigit()):
        print(f"error: gm_code must look like gm1680, got {args.gm_code!r}", file=sys.stderr)
        return 2
    if args.all_gemeenten and not args.metadata_only:
        print("error: --all-gemeenten requires --metadata-only "
              "(downloading PDFs across every gemeente would be terabytes; "
              "use the JSON's manifestations.pdf URLs to fetch selectively).",
              file=sys.stderr)
        return 2
    if args.all_gemeenten and args.expand:
        print("error: --expand is meaningless with --all-gemeenten "
              "(no single creator to expand from)", file=sys.stderr)
        return 2

    if args.year is not None:
        if args.date_from or args.date_to:
            print("error: --year cannot be combined with --from/--to", file=sys.stderr)
            return 2
        args.date_from = f"{args.year}-01-01"
        args.date_to = f"{args.year}-12-31"

    def split_types(values):
        out = []
        for v in values or []:
            out.extend(t.strip() for t in v.split(",") if t.strip())
        return out
    args.types = split_types(args.types)
    args.exclude_types = split_types(args.exclude_types)
    if args.types and args.exclude_types:
        print("error: --type and --exclude-type are mutually exclusive", file=sys.stderr)
        return 2
    for t in (args.types + args.exclude_types):
        if '"' in t:
            print(f"error: type value cannot contain a double quote: {t!r}", file=sys.stderr)
            return 2

    if args.all_gemeenten:
        out_dir = args.out or os.path.join("out", "all-gemeenten")
        bare = None
        print(f"scope: all gemeenten (w.organisatietype==\"gemeente\")")
    else:
        out_dir = args.out or os.path.join("out", args.gm_code)
        bare, full = resolve_gemeente(args.gm_code)
        print(f"{args.gm_code} = {full!r}  (CQL creator={bare!r})")
    os.makedirs(out_dir, exist_ok=True)

    date_filter = ""
    if args.date_from or args.date_to:
        date_filter = f"  modified in [{args.date_from or '-inf'} .. {args.date_to or '+inf'}]"
    print(f"querying SRU (expand={'on' if args.expand else 'off'}){date_filter}...")

    progress_path = os.path.join(out_dir, "_progress.json")
    progress = {}
    if os.path.exists(progress_path):
        progress = json.load(open(progress_path))
    progress_key = json.dumps({
        "creator": bare, "all": args.all_gemeenten,
        "from": args.date_from, "to": args.date_to, "expand": args.expand,
        "types": sorted(args.types), "exclude_types": sorted(args.exclude_types),
    }, sort_keys=True)
    if args.start_record is not None:
        start = args.start_record
        print(f"resuming from startRecord={start} (per --start-record)")
    elif progress.get("key") == progress_key and progress.get("next_start", 1) > 1:
        start = progress["next_start"]
        print(f"resuming from startRecord={start} (per {progress_path})")
    else:
        start = 1

    n_docs = n_pdfs = n_skipped = n_no_pdf = 0
    total = None
    while True:
        # Defensive: the output dir can disappear out from under a long run
        # (external cleanup, sync, etc.). Recreate per page; cheap, idempotent.
        os.makedirs(out_dir, exist_ok=True)

        url = build_query_url(creator=bare, all_gemeenten=args.all_gemeenten,
                              start=start, expand=args.expand,
                              date_from=args.date_from, date_to=args.date_to,
                              types=args.types, exclude_types=args.exclude_types)
        page = http_get(url)
        any_in_page = False
        for ident, meta, pdf_url, page_total in parse_records(page):
            if total is None:
                total = page_total
                print(f"total records: {total}")
            any_in_page = True
            n_docs += 1

            meta_path = os.path.join(out_dir, f"{ident}.json")
            try:
                with open(meta_path, "w") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)
            except FileNotFoundError:
                # Output dir disappeared mid-page; recreate and retry once.
                os.makedirs(out_dir, exist_ok=True)
                print(f"  ! out_dir {out_dir!r} disappeared, recreated", file=sys.stderr)
                with open(meta_path, "w") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)

            if not pdf_url:
                n_no_pdf += 1
                if n_docs <= 20 or n_docs % 500 == 0:
                    print(f"[{n_docs}/{total}] {ident}  (no pdf manifestation)")
            else:
                print(f"[{n_docs}/{total}] {ident}  {meta.get('title','')[:70]}")
                if not args.metadata_only:
                    dest = os.path.join(out_dir, f"{ident}.pdf")
                    if os.path.exists(dest):
                        n_skipped += 1
                    else:
                        try:
                            http_download(pdf_url, dest)
                            n_pdfs += 1
                        except Exception as e:
                            print(f"  ! download failed for {pdf_url}: {e}", file=sys.stderr)
                        time.sleep(PAUSE)

            if args.max_docs and n_docs >= args.max_docs:
                print(f"\nstopping early at --max-docs={args.max_docs}")
                print(f"docs={n_docs} pdfs_downloaded={n_pdfs} pdfs_already_present={n_skipped} no_pdf={n_no_pdf}")
                return 0

        if not any_in_page or (total is not None and start + PAGE_SIZE > total):
            break
        start += PAGE_SIZE
        with open(progress_path, "w") as f:
            json.dump({"key": progress_key, "next_start": start, "total": total}, f)
        time.sleep(1.0)  # polite inter-page pause; KOOP enforces a fair-use policy

    if os.path.exists(progress_path):
        os.remove(progress_path)
    print(f"\ndone. docs={n_docs} pdfs_downloaded={n_pdfs} pdfs_already_present={n_skipped} no_pdf={n_no_pdf}")
    print(f"output: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
