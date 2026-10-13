"""Choose a Gemini model per task, from what this account can actually use.

HOW WE GOT HERE
---------------
Both projects shipped with "gemini-2.0-flash" hardcoded. Google retired it,
every call returned 404, and because AI features fail soft nothing visibly
broke. The first fix picked "the newest stable flash" from the live model
list. Real data from the account then showed three things that rule changed:

  * The model list includes models this account can't use. gemini-2.5-flash
    is listed, even has a quota on the rate-limit page, and returns
    "no longer available to new users". Listed means exists, not usable.
  * Free-tier daily limits differ 25x by family: every flash model allows 20
    requests a day, every flash-lite model 500. "Newest" was the wrong goal.
  * Google Search grounding has no free quota on any model this account can
    use, so nothing here relies on it.

THE RULE
--------
Each call names a task, and each task has its own order of families:

  bulk     field extraction, naming an employer    flash-lite, then flash
  quality  drafts, photo checks, verdicts           flash, then flash-lite

Within a family the newest stable versioned model wins. Stable means
gemini-<version>-flash or gemini-<version>-flash-lite with nothing after it,
which excludes tts, image, preview, transcribe, Omni, Gemma and the rest by
construction. Versions compare as numbers, so 3.10 beats 3.8. Google's
moving aliases are a last resort only: a switch behind an alias can't be
seen, so it can't be announced.

GEMINI_MODEL pins the quality task and GEMINI_MODEL_BULK the bulk task.
"auto" (the default) means choose. A pinned model that can't be used falls
back to auto, and you're told.

WHAT IS LEARNED AT RUNTIME
--------------------------
  404 from a generation call  -> that model is unusable for this account.
                                 Avoided for 7 days, then tried again in
                                 case access changes.
  429 daily quota             -> that model is avoided until the free-tier
                                 reset (midnight Pacific time), and the task
                                 falls back to its next family meanwhile.
  429 per-minute limit        -> no switch; the call just fails this time.

NEVER SILENT
------------
Every change of model for a task is announced on Telegram with the reason,
including the switch back after a quota reset. A different model extracts
and writes differently, and a change you weren't told about is the kind of
silent failure this module exists to prevent.

State lives in gemini_model.json next to the project, shared by the
listener and the nightly jobs. The model list is fetched at most once a
day; listing doesn't use generation quota.

This file is identical in job-scout and WilloMotors. Keep it that way.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from datetime import datetime, timedelta

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:                       # pragma: no cover
    ZoneInfo = None

LIST_URL = "https://generativelanguage.googleapis.com/v1beta/models"
GENERATE_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
                "{model}:generateContent")
CACHE_PATH = os.environ.get(
    "GEMINI_MODEL_CACHE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                 "gemini_model.json"))
RECHECK_SECONDS = 24 * 3600
RETRY_AFTER_FAILURE = 300
UNUSABLE_SECONDS = 7 * 24 * 3600

_FLASH = re.compile(r"^gemini-(\d+(?:\.\d+)*)-flash$")
_LITE = re.compile(r"^gemini-(\d+(?:\.\d+)*)-flash-lite$")
ALIASES = {"flash": "gemini-flash-latest", "flash-lite": "gemini-flash-lite-latest"}

TASKS = {
    "quality": (("flash", _FLASH), ("flash-lite", _LITE)),
    "bulk": (("flash-lite", _LITE), ("flash", _FLASH)),
}
# Whether a task may move to its second family when the first is out of
# daily quota. Bulk may not: extraction is optional, and 500 flash-lite calls
# in one day means something ran away -- far better to pause extraction than
# to spend the 20 flash calls a day that drafts and photo checks depend on.
# (Bulk still uses flash if no flash-lite model is usable at all.)
QUOTA_FALLBACK = {"quality": True, "bulk": False}
TASK_ENV = {"quality": "GEMINI_MODEL", "bulk": "GEMINI_MODEL_BULK"}
TASK_LABEL = {"quality": "drafts, photo checks and verdicts",
              "bulk": "field extraction"}

_memo: dict = {}
_failure = {"at": 0.0, "problem": ""}


# ------------------------------------------------------------------ helpers

def _norm(name) -> str:
    name = (name or "").strip()
    name = name[len("models/"):] if name.startswith("models/") else name
    return name or "auto"


def _version(v: str) -> tuple:
    return tuple(int(p) for p in v.split("."))


def preferred_for(task: str) -> str:
    return _norm(os.environ.get(TASK_ENV[task], "auto"))


def next_reset(now: float | None = None) -> float:
    """Epoch of the next free-tier daily reset: midnight Pacific time."""
    now = time.time() if now is None else now
    if ZoneInfo:
        pt = datetime.fromtimestamp(now, ZoneInfo("America/Los_Angeles"))
        midnight = (pt + timedelta(days=1)).replace(hour=0, minute=0,
                                                    second=0, microsecond=0)
        return midnight.timestamp()
    return now + (24 * 3600 - (now - 8 * 3600) % (24 * 3600))    # UTC-8 guess


def _local(epoch: float) -> str:
    """HH:MM Swiss time, for messages."""
    if ZoneInfo:
        return datetime.fromtimestamp(epoch, ZoneInfo("Europe/Zurich")).strftime("%H:%M")
    return time.strftime("%H:%M UTC", time.gmtime(epoch))


# ---------------------------------------------------------------- selection

def family_of(model: str) -> str | None:
    for family, rx in (("flash", _FLASH), ("flash-lite", _LITE)):
        if rx.match(model):
            return family
    return None


def pick(available: list[str], task: str = "quality", preferred: str = "auto",
         avoid=(), exhausted=()) -> tuple[str | None, str]:
    """(model, reason). Pure function over a list of model names.

    `avoid`: models this account can't use -- skip just those.
    `exhausted`: models whose daily quota is used up -- skip their whole
    family for the day. Every flash version has its own separate 20-a-day
    allowance, and stepping 3.8 -> 3.7 -> 3.6 would both stretch one
    family's budget across five models and change the drafting model
    several times a day. The agreed behaviour is to drop to the next
    family until the reset.
    """
    preferred = _norm(preferred)
    exhausted = set(exhausted)
    avoid = set(avoid) | exhausted
    spent = {family_of(m) for m in exhausted} - {None}
    usable = [n for n in available if n not in avoid]
    if preferred != "auto" and preferred in usable and family_of(preferred) not in spent:
        return preferred, "your setting"
    order = TASKS[task]
    if order[0][0] in spent and not QUOTA_FALLBACK[task]:
        return None, (f"{order[0][0]} is out of quota today; paused until the "
                      f"reset so the rest of the quota stays free for other tasks")
    for i, (family, rx) in enumerate(order):
        if family in spent:
            continue
        found = [(_version(m.group(1)), n) for n in usable if (m := rx.match(n))]
        if found:
            why = f"newest usable {family}"
            if i:
                why += (f" — {order[0][0]} is out of quota today" if order[0][0] in spent
                        else f" — no usable {order[0][0]} right now")
            return max(found)[1], why
    for family, _ in order:
        if family not in spent and ALIASES[family] in usable:
            return ALIASES[family], f"only Google's moving {family} alias left"
    return None, "no usable model"


# ------------------------------------------------------------------ listing

def list_models(key: str) -> tuple[list[str] | None, str]:
    """(names, problem). Only models that support generateContent.

    Errors report the exception TYPE only: requests puts URLs into exception
    messages, and earlier versions of these projects leaked secrets that way.
    """
    names: list[str] = []
    token = None
    for _ in range(10):                       # pagination safety cap
        params = {"pageSize": 200}
        if token:
            params["pageToken"] = token
        try:
            r = requests.get(LIST_URL, headers={"x-goog-api-key": key},
                             params=params, timeout=30)
        except requests.RequestException as exc:
            return None, f"couldn't reach the Gemini API ({type(exc).__name__})"
        if r.status_code in (400, 401, 403):
            return None, f"the Gemini API rejected the key (HTTP {r.status_code})"
        if not r.ok:
            return None, f"the model list returned HTTP {r.status_code}"
        try:
            data = r.json()
        except ValueError:
            return None, "the model list wasn't valid JSON"
        for m in data.get("models", []) or []:
            if "generateContent" in (m.get("supportedGenerationMethods") or []):
                names.append(_norm(m.get("name")))
        token = data.get("nextPageToken")
        if not token:
            break
    return names, ""


# -------------------------------------------------------------------- state

def _load() -> dict:
    """The shared state, always with its sections present -- including on a
    first run with no file yet. (Returning a bare {} there crashed every call
    on a fresh install; the tests caught it.)"""
    try:
        with open(CACHE_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        d = {}
    if not isinstance(d, dict):
        d = {}
    if "tasks" not in d and d.get("model"):
        # Single-model cache from the first version: it described what is
        # now the quality task. Keep that, so the first run of this version
        # reports a real change rather than a first-time pick.
        d = {"tasks": {"quality": {"model": d["model"], "pinned": d.get("pinned", "auto"),
                                   "reason": d.get("reason", "")}}}
    d.setdefault("tasks", {})
    d.setdefault("unusable", {})
    d.setdefault("exhausted", {})
    return d


def _save(d: dict) -> None:
    """Atomic write: the listener and the nightly job are separate processes
    and may both update the state; neither may read a half-written file."""
    folder = os.path.dirname(os.path.abspath(CACHE_PATH))
    try:
        fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
        os.replace(tmp, CACHE_PATH)
    except OSError as exc:
        print(f"[ai] couldn't save model state ({type(exc).__name__})")


def _active(state: dict, kind: str, now: float) -> dict:
    """{model: until} for blocks of one kind that are still in effect."""
    return {m: float(u) for m, u in state.get(kind, {}).items() if float(u) > now}


def _blocked(state: dict, now: float) -> dict:
    """{model: until} for every model currently unusable or out of quota."""
    out = dict(_active(state, "unusable", now))
    for m, u in _active(state, "exhausted", now).items():
        out[m] = max(u, out.get(m, 0.0))
    return out


# ------------------------------------------------------------------- notify

def telegram_notify(text: str) -> None:
    """Plain-text Telegram message from whichever process made the change.

    Self-contained so a nightly job can announce a switch as well as the
    listener. Plain text avoids HTML-parsing failures; errors are logged by
    type only because the token is part of the URL.
    """
    token = os.environ.get("TG_TOKEN", "").strip()
    chat = os.environ.get("TG_CHAT", "").strip()
    print(f"[ai] {text}")
    if not (token and chat):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text}, timeout=20)
    except requests.RequestException as exc:
        print(f"[ai] couldn't send model notice ({type(exc).__name__})")


# ------------------------------------------------------------------ resolve

def resolve(key: str, task: str = "quality", preferred: str | None = None,
            notify=telegram_notify, note: str = "") -> str | None:
    """The model to use for `task` right now.

    Uses an in-process memo, then the shared state file, and lists models
    from the API at most once a day. `note` is appended to the change message
    when a caller already knows why the choice is changing.
    """
    preferred = _norm(preferred_for(task) if preferred is None else preferred)
    now = time.time()

    memo = _memo.get(task)
    if memo and memo["pinned"] == preferred and now < memo["until"]:
        return memo["model"]

    state = _load()
    blocked = _blocked(state, now)
    names = state.get("listed")
    if not names or now - float(state.get("listed_at", 0)) >= RECHECK_SECONDS:
        if now - _failure["at"] < RETRY_AFTER_FAILURE:
            names = None
        else:
            fresh, problem = list_models(key)
            if fresh is None:
                # Can't list right now. The last choice is far more likely to
                # work than nothing, so keep using it; retry in 5 minutes.
                _failure.update(at=now, problem=problem)
                print(f"[ai] model list unavailable: {problem}")
            else:
                _failure.update(at=0.0, problem="")
                state["listed"], state["listed_at"] = fresh, now
                names = fresh
    if names is None:
        prev = state["tasks"].get(task, {}).get("model")
        return prev if prev and prev not in blocked else None

    chosen, reason = pick(names, task, preferred,
                          avoid=_active(state, "unusable", now),
                          exhausted=_active(state, "exhausted", now))
    prev = state["tasks"].get(task, {})
    state["tasks"][task] = {"model": chosen, "reason": reason, "pinned": preferred}
    _save(state)

    # Remember the choice until the daily recheck, or until a block that
    # affects this choice expires -- so the switch back after a quota reset
    # happens (and is announced) without waiting a whole day.
    until = now + RECHECK_SECONDS
    for model, b_until in blocked.items():
        until = min(until, b_until)
    _memo[task] = {"model": chosen, "pinned": preferred, "until": until}

    changed = prev.get("model") != chosen or prev.get("pinned") != preferred
    if changed and notify:
        _announce(task, prev.get("model"), chosen, reason, preferred, note, notify)
    return chosen


def _announce(task, was, now_model, reason, preferred, note, notify) -> None:
    label = TASK_LABEL[task]
    tail = f" {note}" if note else ""
    if now_model is None:
        hint = (" Check GEMINI_API_KEY and your quota at ai.dev/rate-limit."
                if reason == "no usable model" else "")
        notify(f"Gemini: {label} is paused — {reason}.{tail}{hint}")
    elif preferred != "auto" and now_model != preferred:
        notify(f"Gemini: your {TASK_ENV[task]} ({preferred}) can't be used, so "
               f"{label} will use {now_model} ({reason}).{tail} Set "
               f"{TASK_ENV[task]}=auto in .env to stop pinning.")
    elif was:
        notify(f"Gemini model for {label} changed: {was} → {now_model} "
               f"({reason}).{tail}")
    else:
        notify(f"Gemini model for {label}: {now_model} ({reason}).{tail}")


def _block(kind: str, model: str, until: float) -> None:
    state = _load()
    state[kind][_norm(model)] = until
    _save(state)
    _memo.clear()                  # every task re-checks against the block


def mark_unusable(key: str, task: str, model: str, preferred: str | None = None,
                  notify=telegram_notify) -> str | None:
    """A generation call returned 404: this account can't use `model`."""
    _block("unusable", model, time.time() + UNUSABLE_SECONDS)
    return resolve(key, task, preferred, notify,
                   note=f"{model} isn't available to this account "
                        f"(will re-check in 7 days).")


def mark_exhausted(key: str, task: str, model: str, until: float | None = None,
                   preferred: str | None = None,
                   notify=telegram_notify) -> str | None:
    """A generation call hit the daily free quota for `model`."""
    until = next_reset() if until is None else until
    _block("exhausted", model, until)
    return resolve(key, task, preferred, notify,
                   note=f"Today's free limit for {model} is used up; back "
                        f"after the reset around {_local(until)} Swiss time.")


def _seconds(delay) -> float | None:
    m = re.match(r"^\s*(\d+(?:\.\d+)?)s\s*$", str(delay or ""))
    return float(m.group(1)) if m else None


def classify_429(response) -> tuple[str, float]:
    """("minute", seconds_to_wait) or ("day", reset_epoch).

    Google's 429 body may carry a QuotaFailure naming the exact quota
    (…PerMinute… / …PerDay…) and a RetryInfo delay. When neither says, it is
    treated as the daily quota: switching models a little too eagerly for
    one day costs less than hammering an exhausted one all day.
    """
    try:
        details = response.json().get("error", {}).get("details", []) or []
    except (ValueError, AttributeError):
        details = []
    ids, retry = "", None
    for d in details:
        t = str(d.get("@type", ""))
        if t.endswith("QuotaFailure"):
            for v in d.get("violations", []) or []:
                ids += f" {v.get('quotaId', '')} {v.get('quotaMetric', '')}"
        elif t.endswith("RetryInfo"):
            retry = _seconds(d.get("retryDelay"))
    ids = ids.lower()
    if "perminute" in ids or "per_minute" in ids:
        return "minute", retry or 60.0
    if "perday" in ids or "per_day" in ids:
        return "day", next_reset()
    if retry is not None and retry <= 120:
        return "minute", retry
    return "day", next_reset()


def _learn(key, task, model, response, preferred) -> str | None:
    """Record what a failed response says about `model`. Returns the model
    the task should use instead, if there is one."""
    if response.status_code == 404:
        return mark_unusable(key, task, model, preferred)
    if response.status_code == 429:
        kind, when = classify_429(response)
        if kind == "day":
            return mark_exhausted(key, task, model, when, preferred)
        print(f"[ai] per-minute limit on {model}; try again in about {int(when)}s")
    return None


def generate(key: str, task: str, body: dict, timeout: float,
             preferred: str | None = None):
    """POST generateContent for `task`. Returns (response, model), or
    (None, None) when no model is usable for the task right now.

    A 404 or a daily-quota 429 is learned from and the call retried ONCE on
    the replacement model. If the retry fails too, that is learned as well
    but not retried again -- two attempts per call, never a loop. Network
    errors propagate to the caller's existing error handling.
    """
    model = resolve(key, task, preferred)
    if not model:
        return None, None
    headers = {"x-goog-api-key": key}
    r = requests.post(GENERATE_URL.format(model=model), headers=headers,
                      json=body, timeout=timeout)
    if r.status_code in (404, 429):
        new = _learn(key, task, model, r, preferred)
        if new and new != model:
            model = new
            r = requests.post(GENERATE_URL.format(model=model), headers=headers,
                              json=body, timeout=timeout)
            if r.status_code in (404, 429):
                _learn(key, task, model, r, preferred)
    return r, model


def status() -> dict:
    """For /stats: per task, the model in use and why; plus blocks and any
    current listing problem."""
    state = _load()
    now = time.time()
    tasks = {}
    for task in TASKS:
        t = state["tasks"].get(task, {})
        memo = _memo.get(task)
        tasks[task] = {"model": (memo or {}).get("model") or t.get("model"),
                       "reason": t.get("reason", "")}
    blocked = {m: _local(u) if u - now < 2 * 86400 else "in a few days"
               for m, u in _blocked(state, now).items()}
    checked = state.get("listed_at")
    return {"tasks": tasks, "blocked": blocked, "problem": _failure["problem"],
            "checked": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(checked))
                       if checked else "never"}


def _reset_for_tests() -> None:
    _memo.clear()
    _failure.update(at=0.0, problem="")
