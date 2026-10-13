"""Telegram intake — send the bot a listing, get a verdict back.

Replaces editing manual.yaml over SSH. You're standing in front of a car, you
copy the Facebook advert, you paste it to the bot, and a few seconds later it
replies with the score, the market median, and what to ask the seller.

    python run.py --listen

The listing still lands in manual.yaml as well, so there's a plain-text record
you can edit and re-run. The bot is an input method, not a second database.

WHAT IT ACCEPTS
---------------
Any text message: a pasted advert, or a URL with some text around it.
Commands: /stats, /help.

SECURITY
--------
Only messages from TG_CHAT are processed. Anything from another chat is logged
and dropped — a bot token is guessable enough that this matters, and the bot
writes to your database.

Message text is treated as data throughout: it is extracted from, never
executed, and the AI's output is coerced into typed fields before it touches
the database. See radar/ai.py.

ONE POLLER AT A TIME
--------------------
Telegram allows a single getUpdates consumer per bot token. Running --listen
twice, or alongside anything else polling the same bot, gets you HTTP 409. Use
one instance.
"""
from __future__ import annotations

import html
import os
import re
import threading
import time
from datetime import date

import requests

from . import db
from .redact import redact
from .ai import Gemini, status_line
from .models import Listing
from .paste import parse_pasted, to_yaml_entry
from .scoring import score_listing
from . import settings as st
from .lookup import (handle_models, handle_price, price_candidates,
                     price_report)
from . import ledger

API = "https://api.telegram.org/bot{token}/{method}"

HELP = """<b>🚗 WilloMotors</b>

Paste a car advert and I'll parse it, store it, compare it against the Swiss \
market and tell you what to ask the seller. Send photos and I'll tell you \
what's visible on them.

<b>📊 Market prices</b>

<code>/price audi a3</code>
The spread for a model: what private sellers ask, what garages ask, and what \
has actually disappeared from the listings. A single figure across all years \
isn't a price, so it'll ask you to narrow it.

<code>/price audi a3 2011 150000</code>
A real number. Year and mileage both move the price hard — a 2012 A5 at 90k \
and at 220k are different cars worth thousands apart.

<code>/models</code>
Which models have enough indexed listings to price. The ones with the most \
data give the most trustworthy answers.

<b>💼 Your deals</b>

<code>/deal 3200 seat leon 2007 121000</code>
What-if before you commit: estimated resale, costs, margin, and the most you \
can pay and still make your target. Nothing is saved.

<code>/bought 3200 seat leon 2007 121000</code>
Log a purchase. Snapshots what the tool estimated the car was worth at that \
moment — so later you can see how right it was.

<code>/cost 1 300 quarter glass</code> — add a repair or expense to deal #1
<code>/sold 1 4900</code> — close deal #1 and see the real profit

<code>/ledger</code>
Open positions, capital tied up, total profit, and how far off the estimates \
have been. After four or five sales, this tells you which of the tool's \
numbers to trust.

<b>⚙️ What the hunt does</b>

<code>/budget 5200</code>
The most you'll pay. Anything dearer stops alerting — your working capital, \
not a wish.

<code>/range 2200 5800</code>
The price window searched on AutoScout24. Below the floor is export and scrap \
dealer stock; above your ceiling is someone else's market.

<code>/threshold buy 6</code>
How high a listing must score before it reaches your phone. Lower it if you're \
seeing nothing, raise it if you're seeing noise. Works for <code>broker</code> too.

<code>/stale 40</code>
Days before a listing counts as stale. A seller who has sat unsold this long \
has met the time-wasters and is ready to talk.

<code>/show</code> — every setting, marking which you changed from the file
<code>/reset</code> — undo everything set here, back to config.yaml

<b>ℹ️ Info</b>

<code>/stats</code> — how much is indexed, how many models are priced, which \
build is running
<code>/help</code> — this message

<i>Scores are a sorting hint, not a verdict. The market medians are real data; \
the keyword weights around them are rules of thumb.</i>"""


# Shown in Telegram's ☰ menu. Kept short -- the menu truncates.
COMMAND_MENU = [
    ("price", "What a model is worth — /price audi a3 2011 150000"),
    ("deal", "Margin before you buy — /deal 3200 seat leon 2007 121000"),
    ("bought", "Log a purchase — /bought 3200 seat leon 2007 121000"),
    ("cost", "Add a repair to a deal — /cost 1 300 quarter glass"),
    ("sold", "Close a deal, see real profit — /sold 1 4900"),
    ("ledger", "Your deals, profit and how accurate the estimates were"),
    ("models", "Which models have enough data to price"),
    ("stats", "How much is indexed, and which build is running"),
    ("show", "Current settings"),
    ("budget", "Most you'll pay — /budget 5200"),
    ("range", "Price window the hunt searches — /range 2200 5800"),
    ("threshold", "Score needed to alert — /threshold buy 6"),
    ("stale", "Days before a listing counts as stale — /stale 40"),
    ("reset", "Back to config.yaml defaults"),
    ("help", "What everything does"),
]


def register_commands(token: str) -> None:
    """Publish the command list so Telegram's ☰ menu shows real descriptions
    instead of an empty list."""
    _api(token, "setMyCommands", commands=[
        {"command": c, "description": d} for c, d in COMMAND_MENU])


def handle_command(conn, cfg: dict, text: str) -> str | None:
    """Settings commands. Returns a reply, or None if not a settings command."""
    parts = text.split()
    cmd = parts[0].lower().lstrip("/").split("@")[0]
    args = parts[1:]

    try:
        if cmd == "price":
            q = " ".join(args)
            if not q.strip():
                return handle_price(conn, cfg, q), model_buttons(top_models(conn))
            hits, year, km = price_candidates(conn, q)
            if len(hits) > 1:
                return "🤔 <b>Which one?</b>", model_buttons(hits, year, km)
            return handle_price(conn, cfg, q)

        if cmd == "models":
            return handle_models(conn), model_buttons(top_models(conn))

        if cmd == "deal":
            return ledger.cmd_deal(conn, cfg, args)
        if cmd == "bought":
            return ledger.cmd_bought(conn, cfg, args)
        if cmd == "cost":
            return ledger.cmd_cost(conn, cfg, args)
        if cmd == "sold":
            return ledger.cmd_sold(conn, cfg, args)
        if cmd == "ledger":
            return ledger.cmd_ledger(conn, cfg, args)

        if cmd == "show":
            rows = st.current(conn, cfg)
            out = ["<b>Current settings</b>"]
            for path, val, overridden in rows:
                mark = " ←set here" if overridden else ""
                out.append(f"{path} = {val}{mark}")
            out.append("\nUnmarked values come from config.yaml.")
            return "\n".join(out)

        if cmd == "reset":
            n = st.clear_overrides(conn)
            return (f"Cleared {n} override(s). Back to config.yaml defaults."
                    if n else "Nothing was overridden — already on defaults.")

        if cmd == "budget":
            if not args:
                return "Usage: /budget 5200"
            v = st.set_override(conn, "filters.max_price_chf", args[0])
            return f"Buy budget ceiling set to CHF {v:,}".replace(",", "'")

        if cmd == "range":
            if len(args) != 2:
                return "Usage: /range 2200 5200"
            lo = st.set_override(conn, "search.price_from", args[0])
            hi = st.set_override(conn, "search.price_to", args[1])
            if lo >= hi:
                return f"Low ({lo}) must be under high ({hi}). Both stored — "\
                       f"send /range again to fix."
            return (f"Hunt price window set to CHF {lo:,}–{hi:,}"
                    .replace(",", "'"))

        if cmd == "threshold":
            if len(args) != 2 or args[0] not in ("buy", "broker"):
                return "Usage: /threshold buy 6   (or broker)"
            v = st.set_override(conn, f"alert_threshold.{args[0]}", args[1])
            return f"{args[0]} alert threshold set to {v}"

        if cmd == "stale":
            if not args:
                return "Usage: /stale 40"
            v = st.set_override(conn, "filters.stale_days", args[0])
            return f"Listings count as stale after {v} days"

    except ValueError as exc:
        return "⚠ " + html.escape(str(exc), quote=False)

    return None


def _api(token: str, method: str, **payload):
    """Call the Bot API; None on failure.

    The token is in the URL, and requests' exception messages contain the
    URL, so exceptions are always redacted before printing. On an HTTP error
    Telegram's own description is logged instead ("can't parse entities",
    "chat not found") -- that's the part that says what actually went wrong.
    """
    try:
        r = requests.post(API.format(token=token, method=method),
                          json=payload, timeout=40)
    except requests.RequestException as exc:
        print(f"[listen] telegram {method} failed: {redact(exc)}")
        return None
    if r.status_code == 409:
        raise RuntimeError(
            "Telegram says another getUpdates poller is already running "
            "for this bot. Stop the other one, or use a second bot token.")
    if not r.ok:
        try:
            detail = r.json().get("description", "")
        except ValueError:
            detail = r.text[:200]
        print(f"[listen] telegram {method} HTTP {r.status_code}: {redact(detail)}")
        return None
    try:
        return r.json().get("result")
    except ValueError:
        return None


# Telegram sends each photo at several resolutions, smallest first. The
# largest is often 3-5 MB, which is slow to move and no more informative for
# spotting a rusty sill. Take the biggest one under this.
MAX_PHOTO_BYTES = 1_200_000

# An album arrives as several separate updates sharing a media_group_id, with
# no "that's all" marker. Buffer them and flush once they stop arriving.
ALBUM_SETTLE_SECONDS = 3.0


def pick_photo_size(sizes: list[dict]) -> dict | None:
    """Largest rendition under the size cap, else the smallest available."""
    if not sizes:
        return None
    ok = [s for s in sizes if (s.get("file_size") or 0) <= MAX_PHOTO_BYTES]
    return max(ok or sizes, key=lambda s: s.get("width", 0))


def download_photo(token: str, file_id: str) -> bytes | None:
    info = _api(token, "getFile", file_id=file_id)
    if not info or not info.get("file_path"):
        return None
    try:
        r = requests.get(
            f"https://api.telegram.org/file/bot{token}/{info['file_path']}",
            timeout=60)
        r.raise_for_status()
        return r.content
    except requests.RequestException as exc:
        print(f"[listen] photo download failed: {redact(exc)}")
        return None


def send(token: str, chat: str, text: str, buttons: list | None = None) -> None:
    """Send a message, optionally with tappable buttons.

    Buttons rather than text like "/price audi tt": Telegram auto-links the
    command token only, so tapping such a line sends a bare "/price" and the
    arguments are lost. An inline keyboard carries its own payload.
    """
    payload = {"chat_id": chat, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    _api(token, "sendMessage", **payload)


def top_models(conn, n: int = 6) -> list[str]:
    """Best-covered models, for quick-pick buttons on the usage screens."""
    return [r[0] for r in conn.execute(
        """SELECT model_key FROM listings
           WHERE model_key != '' AND price_chf > 0
           GROUP BY model_key ORDER BY COUNT(*) DESC LIMIT ?""", (n,))]


def model_buttons(keys: list[str], year: int | None = None,
                  km: int | None = None) -> list:
    """One button per candidate model, two to a row.

    callback_data is capped at 64 bytes by Telegram, so the payload is kept
    to "p:<model> <year> <km>" and truncated defensively.
    """
    rows, row = [], []
    for k in keys[:6]:
        # Pipe-delimited: model keys contain spaces ("audi tt"), so splitting
        # the payload on whitespace loses half the name.
        data = f"p:{k}|{year or ''}|{km or ''}"
        row.append({"text": k, "callback_data": data[:64]})
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    return rows


MIN_ADVERT_CHARS = 60


def looks_like_advert(text: str) -> tuple[bool, str]:
    """Is this message actually a car advert?

    Without this, "Hey" and "Ok" get stored as listings, scored, and written
    into the price history. Returns (ok, reason_if_not).
    """
    from .models import MAKES

    stripped = text.strip()
    if len(stripped) < MIN_ADVERT_CHARS:
        return False, (
            f"That's {len(stripped)} characters — too short to be an advert.\n\n"
            "Paste the full listing text, or send photos of the car. "
            "/help for what else I do.")

    low = stripped.lower()
    has_make = any(re.search(rf"\b{re.escape(m)}\b", low) for m in MAKES)
    has_number = bool(re.search(r"\d[\d'’\s.]{2,}\s*(?:km|chf|\.-)", low)
                      or re.search(r"\b(19[89]\d|20[0-3]\d)\b", stripped))

    if not (has_make or has_number):
        return False, ("I couldn't find a car make, a price, a mileage or a "
                       "year in that. Paste the advert text as it appears on "
                       "the listing.")
    return True, ""


def build_listing(text: str, fields: dict, entry_id: str) -> Listing:
    """Turn extracted fields into a Listing. Works for both the AI and the
    regex path — the AI just fills more of them in."""
    url_match = re.search(r"https?://\S+", text)
    return Listing(
        source="manual",
        external_id=entry_id,
        url=url_match.group(0) if url_match else "",
        title=fields.get("title") or text.strip().splitlines()[0][:120],
        body=fields.get("body") or text[:1500],
        price_chf=fields.get("price"),
        km=fields.get("km"),
        year=fields.get("year"),
        make=fields.get("make", ""),
        model=fields.get("model", ""),
        fuel=fields.get("fuel", ""),
        gearbox=fields.get("gearbox", ""),
        seller_type=fields.get("seller_type") or "private",
        region=fields.get("region", ""),
        published_at=fields.get("published", ""),
        mfk_date=fields.get("mfk_date", ""),
        previous_price=fields.get("previous_price"),
        external_source="Telegram paste",
    )


def handle_listing(conn, cfg: dict, ai: Gemini, text: str) -> str:
    """Parse -> store -> score -> render a reply."""
    fields = ai.extract(text) or {}
    used_ai = bool(fields)
    if not used_ai:
        fields = parse_pasted(text)          # regex fallback
    else:
        regex = parse_pasted(text)           # keep what the AI left null
        for k in ("published", "region", "price", "km", "year"):
            if not fields.get(k) and regex.get(k):
                fields[k] = regex[k]

    slug = re.sub(r"[^a-z0-9]+", "-",
                  (fields.get("title") or "listing").lower()).strip("-")[:32]
    entry_id = f"{slug}-{date.today():%m%d}"

    li = build_listing(text, fields, entry_id)
    lid, is_new, prev = db.upsert(conn, li)
    baseline, n, basis = db.market_estimate(conn, li, cfg)

    # Durable plain-text copy, same as --paste writes.
    path = cfg.get("sources", {}).get("manual", {}).get("path", "manual.yaml")
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n" + to_yaml_entry(fields, entry_id))
    except OSError as exc:
        print(f"[listen] could not append to {path}: {exc}")

    buy = score_listing(conn, li, lid, cfg, "buy")
    broker = score_listing(conn, li, lid, cfg, "broker")

    out = [f"<b>{li.title}</b>"]
    facts = []
    if li.price_chf:
        facts.append(f"CHF {li.price_chf:,}".replace(",", "'"))
    if li.km:
        facts.append(f"{li.km:,} km".replace(",", "'"))
    if li.year:
        facts.append(str(li.year))
    if li.region:
        facts.append(li.region)
    if facts:
        out.append(" · ".join(facts))
    if li.mfk_date:
        out.append(f"Last MFK: {li.mfk_date}")
    if not is_new and prev and li.price_chf and li.price_chf < prev:
        out.append(f"⬇ price cut since last seen: {prev} → {li.price_chf}")

    out.append("")
    out.append(f"<b>buy {buy.value:.1f}</b> · broker {broker.value:.1f}")
    if baseline:
        label = ("Private median" if basis == "private"
                 else "Est. private value")
        out.append(f"{label} CHF {baseline:,}".replace(",", "'")
                   + f" (n={n})")
    elif li.model_key:
        out.append(f"No baseline yet for {li.model_key} "
                   f"({n} comparables, need 5)")
    else:
        out.append("Couldn't identify the make/model, so no price comparison.")

    missing = [name for name, val in (("price", li.price_chf), ("mileage", li.km),
                                      ("year", li.year)) if not val]
    if missing:
        out.append(f"<i>Not stated in the advert: {', '.join(missing)}</i>")

    reasons = [r for r in buy.reasons if not r.startswith("above budget")]
    if reasons:
        out.append("")
        out += [f"• {r}" for r in reasons[:6]]

    flags = fields.get("red_flags") or []
    if flags:
        out.append("")
        out.append("<b>Stated in the advert:</b>")
        out += [f"⚠ {f}" for f in flags]

    verdict = ai.assess(li, baseline, n)
    if verdict:
        out.append("")
        out.append(verdict)

    if not used_ai and ai.available is False:
        out.append("")
        out.append("<i>parsed without AI — set GEMINI_API_KEY for better "
                   "extraction</i>")

    return "\n".join(out)


def handle_photos(conn, cfg: dict, ai: Gemini, token: str,
                  images: list[bytes], caption: str) -> str:
    """Photos, with or without advert text alongside them."""
    if not ai.available:
        return ("I can look at photos, but that needs GEMINI_API_KEY set.\n"
                "Paste the advert text instead and I'll score it.")

    context_bits = [f"{len(images)} photo(s) of a used car for sale in "
                    f"Switzerland."]
    listing_reply = ""

    # A caption long enough to be an advert gets the full listing treatment,
    # and the photos are then judged with the seller's own claims in hand.
    if len(caption.strip()) > 60:
        listing_reply = handle_listing(conn, cfg, ai, caption)
        context_bits.append("The seller's advert says:\n" + caption[:1500])
        context_bits.append("Check whether the photos support those claims.")
    elif caption.strip():
        context_bits.append("The sender added: " + caption.strip()[:300])

    verdict = ai.assess_photos(images, "\n\n".join(context_bits))
    if not verdict:
        return listing_reply or "Couldn't read those photos — try again?"

    out = []
    if listing_reply:
        out += [listing_reply, "", "—" * 20, ""]
    out += [f"<b>📷 What I can see ({len(images)} photo(s))</b>", "", verdict]
    if not listing_reply:
        out += ["", "<i>Paste the advert text too and I'll score it against "
                "the market median.</i>"]
    return "\n".join(out)


def _photo_worker(cfg: dict, token: str, chat: str,
                  file_ids: list[str], caption: str) -> None:
    """Analyse photos off the main loop.

    Gemini gets up to 90 seconds per album. Done inline, every /price sent in
    the meantime queued behind it -- which feels exactly like the bot being
    broken when you're standing next to a car. Runs in its own thread with its
    own database connection, since sqlite connections can't be shared across
    threads.
    """
    conn = db.connect(cfg.get("database", "radar.db"))
    try:
        imgs = [b for b in (download_photo(token, f) for f in file_ids) if b]
        send(token, chat, handle_photos(conn, cfg, Gemini(), token, imgs,
                                        caption))
    except Exception as exc:
        print(f"[listen] photo worker failed: {redact(exc)}")
        send(token, chat, "Couldn't process those photos: "
                          + html.escape(redact(exc), quote=False))
    finally:
        conn.close()


def _start_photos(cfg, token, chat, file_ids, caption) -> None:
    n = len(file_ids)
    send(token, chat, f"📷 Looking at {n} photo{'s' if n != 1 else ''}… "
                      f"<i>you can keep using the bot meanwhile</i>")
    threading.Thread(target=_photo_worker,
                     args=(cfg, token, chat, list(file_ids), caption),
                     daemon=True).start()


def listen(cfg: dict) -> None:
    token = os.environ.get("TG_TOKEN", "").strip()
    chat = os.environ.get("TG_CHAT", "").strip()
    if not (token and chat):
        print("TG_TOKEN and TG_CHAT must be set to use --listen.")
        return

    conn = db.connect(cfg.get("database", "radar.db"))
    st.apply_overrides(cfg, conn)
    ai = Gemini()
    offset = int(db.get_state(conn, "tg_offset", 0) or 0)

    register_commands(token)
    # Resolving the model here, at startup, means a retired or pinned-away
    # model is noticed and announced on Telegram immediately -- not hours
    # later when the first listing happens to be pasted.
    print(f"Listening. AI: {status_line()}. Ctrl-C to stop.")
    send(token, chat, "🚗 WilloMotors is listening. Paste a listing any time.")

    albums: dict = {}          # media_group_id -> {photos, caption, ts}

    while True:
        # Flush any album that has stopped growing.
        for gid in [g for g, a in albums.items()
                    if time.time() - a["ts"] > ALBUM_SETTLE_SECONDS]:
            a = albums.pop(gid)
            print(f"[listen] album {gid}: {len(a['files'])} photo(s)")
            _start_photos(cfg, token, chat, a["files"], a["caption"])

        updates = _api(token, "getUpdates", offset=offset, timeout=30)
        if not updates:
            time.sleep(1)
            continue

        for u in updates:
            offset = u["update_id"] + 1
            db.set_state(conn, "tg_offset", offset)

            cq = u.get("callback_query")
            if cq:
                _api(token, "answerCallbackQuery", callback_query_id=cq["id"])
                if str((cq.get("message", {}).get("chat") or {})
                       .get("id", "")) != chat:
                    continue
                data = cq.get("data", "")
                if data.startswith("p:"):
                    bits = (data[2:].split("|") + ["", ""])[:3]
                    model = bits[0]
                    year = int(bits[1]) if bits[1].isdigit() else None
                    km = int(bits[2]) if bits[2].isdigit() else None
                    print(f"[listen] button: {model} {year or ''} {km or ''}")
                    send(token, chat, price_report(conn, cfg, model, year, km))
                continue

            msg = u.get("message") or u.get("edited_message") or {}
            text = (msg.get("text") or msg.get("caption") or "").strip()
            from_chat = str((msg.get("chat") or {}).get("id", ""))

            if from_chat != chat:
                print(f"[listen] ignoring message from chat {from_chat}")
                continue

            photo = pick_photo_size(msg.get("photo") or [])
            if photo:
                gid = msg.get("media_group_id")
                if gid:                       # part of an album: buffer it
                    a = albums.setdefault(gid, {"files": [], "caption": "",
                                                "ts": time.time()})
                    a["files"].append(photo["file_id"])
                    a["caption"] = a["caption"] or text
                    a["ts"] = time.time()
                else:                         # lone photo
                    print("[listen] 1 photo")
                    _start_photos(cfg, token, chat, [photo["file_id"]], text)
                continue

            if not text:
                send(token, chat,
                     "Send me advert text, or photos of the car.")
                continue

            if text.startswith("/start") or text.startswith("/help"):
                send(token, chat, HELP)
                continue
            if text.startswith("/stats"):
                from . import BUILD, __version__
                stats = {"build": f"{__version__} · {BUILD}"}
                stats.update(db.stats(conn))
                stats.update(db.baseline_coverage(conn))
                stats["AI"] = status_line()
                send(token, chat,
                     "\n".join(f"{k}: {v}" for k, v in stats.items()))
                continue

            if text.startswith("/"):
                reply = handle_command(conn, cfg, text)
                if reply:
                    if isinstance(reply, tuple):
                        send(token, chat, reply[0], reply[1])
                    else:
                        send(token, chat, reply)
                    continue

            ok, why = looks_like_advert(text)
            if not ok:
                send(token, chat, why)
                continue

            print(f"[listen] processing {len(text)} chars")
            try:
                send(token, chat, handle_listing(conn, cfg, ai, text))
            except Exception as exc:
                print(f"[listen] failed: {redact(exc)}")
                send(token, chat, "Couldn't process that: "
                                  + html.escape(redact(exc), quote=False))
