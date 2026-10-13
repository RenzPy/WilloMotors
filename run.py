#!/usr/bin/env python3
"""WilloMotors — find cars worth buying and sellers worth calling.

    python run.py --source sample --mode buy --dry-run     # test offline
    python run.py --source tutti  --mode buy               # one pass
    python run.py --source tutti  --mode broker --loop     # run forever
    python run.py --source tutti  --dump                   # inspect raw json
    python run.py --stats
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback

import html
import os
import re

import yaml

from radar import db, sources
from radar.notify import Notifier
from radar.scoring import render_alert, score_listing


def load_dotenv(path: str = ".env") -> int:
    """Read .env into the environment if it exists.

    Variables already set win, so `TG_CHAT=x python run.py` still overrides
    the file. Avoids a dependency, and avoids the repeated confusion of a run
    printing "TG_TOKEN not set" purely because a new shell or tmux pane never
    sourced it.
    """
    if not os.path.exists(path):
        return 0
    loaded = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = val
                loaded += 1
    return loaded


def searches_for(cfg: dict, source_name: str) -> list:
    """Each source can have its own search block, because their filter
    vocabularies differ: tutti takes a pasted search URL, AutoScout24 takes
    price/year/zip ranges. Falls back to the shared `searches:` list."""
    return cfg.get(f"searches_{source_name}") or cfg.get("searches") or [{}]


def load_cfg(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def do_dump(cfg: dict, source_name: str):
    """Fetch one page and show the shape of a raw record, so you can fill in
    the field map in config.yaml."""
    search = searches_for(cfg, source_name)[0]
    src = sources.get(source_name)(cfg, search)
    records = list(src.fetch())
    if not records:
        print("No records returned. Check endpoint and items_path.")
        return
    out = f"dump_{source_name}.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(records[0], fh, indent=2, ensure_ascii=False)
    print(f"{len(records)} records. First one written to {out}\n")
    print("Top-level keys:")
    for k, v in records[0].items():
        preview = str(v)
        if len(preview) > 70:
            preview = preview[:70] + "…"
        print(f"  {k:<22} {type(v).__name__:<8} {preview}")


def source_enabled(cfg: dict, source_name: str) -> bool:
    """A source can be switched off in config without deleting its settings.
    Defaults to on, so only sources you explicitly disable are skipped."""
    return cfg.get("sources", {}).get(source_name, {}).get("enabled", True)


def run_once(cfg: dict, source_name: str, mode: str, notifier: Notifier,
             explain: bool = False) -> int:
    if not source_enabled(cfg, source_name):
        print(f"[{source_name}] disabled in config.yaml "
              f"(sources.{source_name}.enabled: false)")
        return 0

    conn = db.connect(cfg.get("database", "radar.db"))
    threshold = cfg.get("alert_threshold", {}).get(mode, 6.0)
    cooloff = cfg.get("alert_cooloff_days", 14)
    alerts = 0

    for search in searches_for(cfg, source_name):
        label = search.get("name", "unnamed")
        src = sources.get(source_name)(cfg, search)
        try:
            listings = src.collect()
        except Exception as exc:
            print(f"[{source_name}/{label}] fetch failed: {exc}")
            continue

        print(f"[{source_name}/{label}] {len(listings)} listings")

        # Optional: one extra request per listing for structured car facts.
        if cfg.get("sources", {}).get(source_name, {}).get("detail"):
            after = int(cfg.get("index", {}).get("reenrich_after_days", 30))
            skipped = 0
            for i, li in enumerate(listings, 1):
                if not db.needs_enrichment(conn, source_name,
                                           li.external_id, after):
                    skipped += 1
                    continue
                src.enrich(li)
                db.upsert(conn, li)
                db.mark_enriched(conn, source_name, li.external_id)
                if i % 10 == 0 or i == len(listings):
                    print(f"  [{source_name}] enriched {i}/{len(listings)}"
                          + (f" ({skipped} cached)" if skipped else ""),
                          flush=True)
                if i < len(listings):
                    src.sleep()
            if skipped:
                print(f"  [{source_name}] skipped {skipped} already enriched "
                      f"within {after} days")

        for li in listings:
            lid, is_new, prev = db.upsert(conn, li)
            if prev is not None and li.price_chf and li.price_chf < prev:
                print(f"  price cut: {li.title[:44]} {prev} -> {li.price_chf}")

            s = score_listing(conn, li, lid, cfg, mode)

            if explain:
                mark = "ALERT" if s.value >= threshold else "  -  "
                if db.already_alerted(conn, lid, mode, cooloff):
                    mark = "seen "
                price = f"{li.price_chf:,}".replace(",", "'") if li.price_chf else "?"
                km = f"{li.km:,}".replace(",", "'") if li.km else "?"
                print(f"{mark} {s.value:6.1f} | CHF {price:>7} | {km:>7} km | "
                      f"{str(li.year or '?'):>4} | {li.seller_type[:7]:<7} | "
                      f"MFK {li.mfk_date or '—':<10} | {li.title[:30]}")
                for r in s.reasons[:4]:
                    print(f"                {r}")
                if s.baseline:
                    print(f"                {s.basis} est. CHF {s.baseline:,}"
                          .replace(",", "'") + f" (n={s.n_comps})"
                          + (f"  |  garage asking CHF {s.retail:,}"
                             .replace(",", "'") if s.retail else ""))
                else:
                    print(f"                no baseline yet for "
                          f"'{li.model_key}' ({s.n_comps} comparables, need 5)")
                print()
                continue

            if s.value < threshold:
                continue
            if db.already_alerted(conn, lid, mode, cooloff):
                continue

            if notifier.send(render_alert(li, s, mode)):
                db.record_alert(conn, lid, mode, s.value, s.reasons)
                alerts += 1

        # No expiry here. Sold-detection belongs only to the index, and only
        # per band that was just re-read in full. A hunt reads a filtered
        # slice of the market; treating "not in this slice" as "sold" would
        # mass-expire indexed cars.

    conn.close()
    return alerts


def _chf(n) -> str:
    return f"{int(n):,}".replace(",", "'")


def _plain(text: str) -> str:
    """Telegram HTML -> readable console text."""
    text = re.sub(r'<a href="[^"]*">([^<]*)</a>', r"\1", text)
    return html.unescape(re.sub(r"</?[a-z]+>", "", text))


def do_index(cfg: dict, source_name: str):
    """Sweep price bands to build the market baseline.

    Not deal flow: nothing is alerted on. This is the reference price every
    score on a car you paste in gets measured against.
    """
    from radar.bands import band_key, coverage, mark_swept, next_bands

    conn = db.connect(cfg.get("database", "radar.db"))
    before = db.baseline_coverage(conn)
    bands = next_bands(conn, cfg)
    pages = int(cfg.get("index", {}).get("pages_per_band", 60))

    seen = added = drops = rises = expired = 0
    drop_total = 0
    biggest: list = []
    swept: list = []

    for lo, hi in bands:
        key = band_key(lo, hi)
        sweep_id = db.next_sweep_id(conn)
        search = {"name": f"band {key}", "price_from": lo, "price_to": hi,
                  "pages": pages}
        src = sources.get(source_name)(cfg, search)
        print(f"[index] band CHF {lo:,}–{hi:,}".replace(",", "'"))

        try:
            for li in src.iter_listings():
                _, is_new, prev = db.upsert(conn, li, band=key,
                                            sweep_id=sweep_id)
                seen += 1
                added += int(is_new)
                if not is_new and prev and li.price_chf:
                    if li.price_chf < prev:
                        drops += 1
                        cut = prev - li.price_chf
                        drop_total += cut
                        biggest.append((cut, li.title[:40], prev,
                                        li.price_chf, li.url, li.year, li.km,
                                        li.seller_type))
                    elif li.price_chf > prev:
                        rises += 1
        except Exception as exc:
            print(f"  band {key} failed: {exc}")
            continue

        # Only a band just re-read in full can say what has gone from it.
        gone = db.expire_band(conn, source_name, key, sweep_id)
        expired += gone
        mark_swept(conn, (lo, hi))
        swept.append(key)
        print(f"  {key}: swept, {gone} listing(s) now gone")

    after = db.baseline_coverage(conn)
    biggest.sort(reverse=True)
    never = sum(1 for _, when in coverage(conn, cfg) if when == "never")

    lines = [
        "<b>📊 Index run complete</b>",
        f"bands swept: {', '.join(swept) if swept else 'none'}",
        f"{seen:,} listings seen · {added:,} new".replace(",", "'"),
    ]
    if drops or rises:
        lines.append(f"{drops} price cuts · {rises} increases")
    if drops:
        lines.append(f"total cut: CHF {drop_total:,}".replace(",", "'"))
    if expired:
        lines.append(f"{expired} gone from listings (likely sold)")

    lines += ["",
              f"models priced: {after['models_priced_any']}"
              + (f" (+{after['models_priced_any'] - before['models_priced_any']})"
                 if after['models_priced_any'] > before['models_priced_any'] else "")
              + f" of {after['models_seen']} seen",
              f"  {after['models_priced_private']} from real private comparables",
              f"bands never swept yet: {never}"]

    if biggest:
        lines += ["", "<b>💸 Biggest cuts this run</b>",
                  "<i>a seller dropping their price is the most actionable "
                  "signal here — tap through</i>", ""]
        for cut, title, was, now, url, year, km, stype in biggest[:5]:
            # Build the numbers first: applying the thousands-separator swap
            # to a whole line containing a URL would corrupt the link.
            c, w, n = _chf(cut), _chf(was), _chf(now)
            name = html.escape(title)
            head = f'<a href="{html.escape(url, quote=True)}">{name}</a>' \
                if url else f"<b>{name}</b>"
            facts = " · ".join(p for p in (
                str(year) if year else "",
                f"{_chf(km)} km" if km else "",
                "dealer" if stype in db.DEALER_TYPES else "private",
            ) if p)
            lines.append(f"▼ <b>CHF {c}</b> — {head}")
            lines.append(f"    {w} → {n}" + (f" · {facts}" if facts else ""))

    report = "\n".join(lines)
    print("\n" + _plain(report))

    notifier = Notifier()
    if not notifier.dry_run:
        notifier.send(report)
    conn.close()


def do_probe(cfg: dict, source_name: str):
    """One request, minimum page size. Answers 'does this work at all, and am I
    being challenged' without starting a crawl."""
    if source_name in ("tutti", "anibis"):
        from radar.sources.tutti import describe_refusal, _NEXT_DATA, \
            find_search_payload
        import json as _json

        search = searches_for(cfg, source_name)[0]
        src = sources.get(source_name)(cfg, search)
        url = search.get("url")
        if not url:
            print("No 'url' set for this search in config.yaml.")
            return

        print(f"GET {url}\n")
        r = src.session.get(url, timeout=30)
        print(f"HTTP {r.status_code}  ({len(r.content)} bytes)")
        if r.status_code != 200:
            print("\n  " + describe_refusal(r, url))
            return

        m = _NEXT_DATA.search(r.text)
        if not m:
            print("200 OK but no __NEXT_DATA__ block — page layout changed, "
                  "or this is not a search results page.")
            print("first 300 chars:", r.text[:300].replace("\n", " "))
            return

        listings = find_search_payload(_json.loads(m.group(1)))
        if listings is None:
            print("__NEXT_DATA__ found but no search payload inside it.")
            return
        edges = listings.get("edges") or []
        print(f"totalCount: {listings.get('totalCount')}   "
              f"on this page: {len(edges)}\n")
        for e in edges[:5]:
            li = src.parse(e["node"])
            if li:
                print(f"  {li.title[:34]:<34} CHF {str(li.price_chf):>7} | "
                      f"{li.seller_type:<7} | tier={li.seller_tier or '-':<4} | "
                      f"{li.region}")
        print("\nLooks right? Then run the real thing.")
        return

    if source_name != "autoscout24":
        print(f"--probe not implemented for {source_name}")
        return

    import json as _json
    from radar.sources.autoscout24 import API_BASE, AutoScout24

    src = AutoScout24(cfg, searches_for(cfg, "autoscout24")[0])
    src.session.headers.update({
        "Content-Type": "application/json", "Accept": "application/json"})

    query = src._build_query()
    body = {"query": query, "sort": [{"type": "PRICE", "order": "ASC"}],
            "pagination": {"page": 0, "size": 3}}

    print(f"POST {API_BASE}/listings/search")
    print("body:", _json.dumps(body), "\n")

    r = src.session.post(f"{API_BASE}/listings/search", json=body, timeout=30)
    print(f"HTTP {r.status_code}  ({len(r.content)} bytes)")
    server = r.headers.get("server", "")
    if server:
        print(f"server: {server}")
    if r.headers.get("cf-ray"):
        print("Cloudflare is in front of this host (cf-ray present).")

    if r.status_code != 200:
        print("\nBody (first 400 chars):\n" + r.text[:400])
        if r.status_code in (401, 403, 429):
            if "allowlist" in r.text.lower() or "egress" in r.text.lower():
                print("\nThat came from your own network/proxy, not from "
                      "AutoScout24. Allow the host and probe again.")
            else:
                print("\nThat is the site refusing. Do not try to work around "
                      "it — set sources.autoscout24.enabled: false and stop.")
        elif r.status_code == 400:
            print("\nBad request. Most likely the API wants makeModelVersions: "
                  "set make/model in searches_autoscout24 and probe again.")
        return

    data = r.json()
    print(f"\ntotalElements: {data.get('totalElements')}   "
          f"totalPages: {data.get('totalPages')}")
    content = data.get("content", [])
    print(f"returned {len(content)} listings\n")
    for item in content:
        li = src.parse(item)
        if li:
            print(f"  {li.title[:34]:<34} CHF {str(li.price_chf):>7} | "
                  f"{str(li.km):>7} km | {li.year} | {li.seller_type:<7} | "
                  f"{li.region:<18} | MFK {li.mfk_date or '-'}")
    print("\nIf that looks right, set sources.autoscout24.enabled: true")


def do_paste(cfg: dict):
    """Read a pasted listing from stdin and append it to manual.yaml."""
    import re as _re
    from datetime import date as _date
    from radar.paste import parse_pasted, to_yaml_entry

    path = cfg.get("sources", {}).get("manual", {}).get("path", "manual.yaml")
    print("Paste the listing, then press Ctrl-D:\n")
    text = sys.stdin.read()
    if not text.strip():
        print("Nothing pasted.")
        return

    fields = parse_pasted(text)
    slug = _re.sub(r"[^a-z0-9]+", "-",
                   fields.get("title", "listing").lower()).strip("-")[:32]
    entry_id = f"{slug}-{_date.today():%m%d}"
    block = to_yaml_entry(fields, entry_id)

    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n" + block)

    print(f"\nAppended to {path}:\n")
    print(block)
    missing = [k for k in ("price", "km", "year", "region", "published")
               if k not in fields]
    if missing:
        print(f"Not found, fill in by hand: {', '.join(missing)}")
    print("Any TODO fields need your input too. Then:  "
          "python run.py --source manual --mode buy")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="store_true",
                    help="print the build and exit")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--source", default="sample",
                    help=f"one of: {', '.join(sources.known())}")
    ap.add_argument("--mode", default="buy", choices=["buy", "broker"])
    ap.add_argument("--loop", action="store_true", help="poll forever")
    ap.add_argument("--dry-run", action="store_true", help="print, do not send")
    ap.add_argument("--dump", action="store_true", help="inspect raw records")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--paste", action="store_true",
                    help="paste a listing on stdin; drafts a manual.yaml entry")
    ap.add_argument("--probe", action="store_true",
                    help="send ONE request to a source and report what came back")
    ap.add_argument("--listen", action="store_true",
                    help="receive listings pasted to your Telegram bot")
    ap.add_argument("--index", action="store_true",
                    help="ingest a source purely to build the price baseline: "
                         "no scoring, no alerts, no detail fetch")
    ap.add_argument("--explain", action="store_true",
                    help="show every listing found and why it did or didn't "
                         "alert; sends nothing")
    args = ap.parse_args()

    if args.version:
        from radar import BUILD, __version__
        print(f"WilloMotors {__version__} · {BUILD}")
        return

    load_dotenv()
    cfg = load_cfg(args.config)

    # Settings changed from Telegram live in the database and layer on top of
    # config.yaml, so the file keeps its comments and /reset really resets.
    from radar.settings import apply_overrides
    _c = db.connect(cfg.get("database", "radar.db"))
    apply_overrides(cfg, _c)
    _c.close()

    if args.index:
        do_index(cfg, args.source)
        return

    if args.listen:
        from radar.intake import listen
        listen(cfg)
        return

    if args.probe:
        do_probe(cfg, args.source)
        return

    if args.paste:
        do_paste(cfg)
        return

    if args.stats:
        conn = db.connect(cfg.get("database", "radar.db"))
        for k, v in db.stats(conn).items():
            print(f"{k:<20} {v}")
        conn.close()
        return

    if args.dump:
        do_dump(cfg, args.source)
        return

    notifier = Notifier(dry_run=args.dry_run or args.explain)

    if not args.loop:
        n = run_once(cfg, args.source, args.mode, notifier, args.explain)
        if not args.explain:
            print(f"\n{n} alert(s) sent.")
        else:
            print(f"threshold for {args.mode} mode: "
                  f"{cfg.get('alert_threshold', {}).get(args.mode)}")
        return

    interval = cfg.get("poll_minutes", 12) * 60
    print(f"Polling {args.source} every {cfg.get('poll_minutes', 12)} min "
          f"in {args.mode} mode. Ctrl-C to stop.")
    while True:
        try:
            n = run_once(cfg, args.source, args.mode, notifier)
            if n:
                print(f"  -> {n} alert(s)")
        except KeyboardInterrupt:
            print("\nstopped.")
            sys.exit(0)
        except Exception:
            traceback.print_exc()
        time.sleep(interval)


if __name__ == "__main__":
    main()
