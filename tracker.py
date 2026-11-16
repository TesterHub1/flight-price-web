#!/usr/bin/env python3
"""
Flight Price Tracker - the part that runs on GitHub.

Keeps the tracked flights and every price reading in data.json, and gets
prices from Google Flights through SerpApi. GitHub Actions runs it once an
hour ("run-due") and whenever the web page asks for something.

Commands:
    python tracker.py run-due
    python tracker.py request      (reads the request from REQ_* environment variables)

The SerpApi key comes from the SERPAPI_KEY environment variable (a repository
secret). It is never written to data.json or printed.
"""

import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.environ.get("TRACKER_DATA", os.path.join(HERE, "data.json"))
API_URL = os.environ.get("TRACKER_API_URL", "https://serpapi.com/search.json")
SOURCE_NAME = "Google Flights (via SerpApi)"
RETRY_AFTER_ERROR_SECONDS = 15 * 60
MAX_TRACKERS = 20


def utcnow():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def api_key():
    return os.environ.get("SERPAPI_KEY", "").strip()


# --------------------------------------------------------------------------- #
# data.json
# --------------------------------------------------------------------------- #


def load():
    try:
        with open(DATA_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data.setdefault("version", 1)
    data.setdefault("trackers", [])
    data.setdefault("usage", {})
    data.setdefault("next_id", 1)
    data.setdefault("notice", None)
    return data


def save(data):
    data["updated_at"] = iso(utcnow())
    data["has_key"] = bool(api_key())
    data["source"] = SOURCE_NAME
    tmp = DATA_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=True)
        f.write("\n")
    os.replace(tmp, DATA_PATH)


def notice(data, message):
    """A message for the page to show (for example why a request was refused)."""
    data["notice"] = {"at": iso(utcnow()), "message": message}
    print("NOTICE:", message)


def tracker_by_id(data, tid):
    for t in data["trackers"]:
        if t["id"] == tid:
            return t
    return None


# --------------------------------------------------------------------------- #
# Flight numbers
# --------------------------------------------------------------------------- #

_FLIGHT_RE = re.compile(r"(?<![A-Z0-9])([A-Z][A-Z0-9]|[0-9][A-Z])\s*-?\s*0*(\d{1,4})(?!\d)")


def parse_flights(text):
    """'6E 731, 6e-120' -> ['6E731', '6E120'] (order kept)."""
    return [a + n for a, n in _FLIGHT_RE.findall((text or "").upper())]


def norm_flight(text):
    found = parse_flights(text)
    return found[0] if found else re.sub(r"[^A-Z0-9]", "", (text or "").upper())


# --------------------------------------------------------------------------- #
# SerpApi (Google Flights)
# --------------------------------------------------------------------------- #


class ApiError(Exception):
    pass


def serpapi(data, params):
    key = api_key()
    query = dict(params)
    query.update({"engine": "google_flights", "api_key": key})
    url = API_URL + "?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url, headers={"User-Agent": "flight-price-tracker-web/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8", "replace")).get("error") or "no details given"
        except Exception:
            msg = "no details given"
        raise ApiError("SerpApi refused the request (HTTP %s): %s" % (e.code, msg))
    except (urllib.error.URLError, OSError) as e:
        # Deliberately not including the URL: it contains the key.
        raise ApiError("Could not reach SerpApi (%s)." % type(getattr(e, "reason", e)).__name__)
    month = utcnow().strftime("%Y-%m")
    data["usage"][month] = data["usage"].get(month, 0) + 1
    try:
        reply = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        raise ApiError("SerpApi sent a reply that could not be read.")
    err = reply.get("error")
    has_results = reply.get("best_flights") or reply.get("other_flights") or reply.get("booking_options")
    if err and not has_results:
        if "any results" in err.lower() or "no results" in err.lower():
            return reply  # a valid answer: Google Flights listed nothing
        raise ApiError("SerpApi said: %s" % err)
    return reply


def base_params(t):
    return {
        "departure_id": t["origin"],
        "arrival_id": t["destination"],
        "outbound_date": t["travel_date"],
        "type": "2",  # one way
        "currency": t["currency"],
        "hl": "en",
        "gl": "in",
    }


def itineraries(reply):
    return (reply.get("best_flights") or []) + (reply.get("other_flights") or [])


def find_itinerary(reply, wanted):
    """Return (cheapest matching itinerary or None, list of (sequence, price) seen)."""
    matches, seen = [], []
    for itin in itineraries(reply):
        seq = [norm_flight(f.get("flight_number", "")) for f in (itin.get("flights") or [])]
        seen.append((seq, itin.get("price")))
        if seq == wanted:
            matches.append(itin)
    priced = [m for m in matches if isinstance(m.get("price"), (int, float))]
    if priced:
        return min(priced, key=lambda m: m["price"]), seen
    return (matches[0] if matches else None), seen


def read_sellers(reply):
    out = []
    for opt in reply.get("booking_options") or []:
        part = opt.get("together") or {}
        name, price = part.get("book_with"), part.get("price")
        if name and isinstance(price, (int, float)):
            out.append({"name": str(name)[:80], "price": price})
    out.sort(key=lambda s: s["price"])
    return out[:12]


def lookup_price(data, t, allow_deep=True):
    """One price check. Returns a reading dict (not yet stored)."""
    wanted = list(t["flights"])
    params = base_params(t)
    params["show_hidden"] = "true"
    reply = serpapi(data, params)
    itin, seen = find_itinerary(reply, wanted)
    if itin is None and allow_deep:
        # Second attempt: the slower search that mirrors the Google Flights page.
        params["deep_search"] = "true"
        reply = serpapi(data, params)
        itin, seen = find_itinerary(reply, wanted)

    r = {"status": "ok", "price": None, "currency": t["currency"], "seller": None, "sellers": [],
         "dep_time": None, "arr_time": None, "via": None, "detail": None}
    google_url = (reply.get("search_metadata") or {}).get("google_flights_url")

    if itin is None:
        examples = []
        for seq, price in seen[:6]:
            label = "+".join(seq)
            if isinstance(price, (int, float)):
                label += " (%s %s)" % (t["currency"], format(int(price), ","))
            examples.append(label)
        r["status"] = "not_found"
        r["detail"] = ("Google Flights did not list %s on %s (%d options returned)."
                       % ("+".join(wanted), t["travel_date"], len(seen))
                       + (" Listed instead: " + "; ".join(examples) + "." if examples else
                          " It may be sold out, cancelled, or not on sale for this date."))
        return r, google_url

    legs = itin.get("flights") or []
    if legs:
        r["dep_time"] = (legs[0].get("departure_airport") or {}).get("time")
        r["arr_time"] = (legs[-1].get("arrival_airport") or {}).get("time")
    r["via"] = ", ".join(str(l.get("id") or l.get("name") or "")
                         for l in (itin.get("layovers") or [])) or None

    price = itin.get("price")
    if not isinstance(price, (int, float)):
        r["status"] = "not_found"
        r["detail"] = "Google Flights listed this flight without a price."
        return r, google_url
    r["price"] = price

    token = itin.get("booking_token")
    if t.get("with_sellers") and token:
        try:
            bp = base_params(t)
            bp["booking_token"] = token
            r["sellers"] = read_sellers(serpapi(data, bp))
            if r["sellers"]:
                r["seller"] = r["sellers"][0]["name"]
        except ApiError as e:
            r["detail"] = "Price recorded; seller list unavailable (%s)" % e
    return r, google_url


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #


def expire_if_done(t, now):
    travel = dt.date.fromisoformat(t["travel_date"])
    # A day of slack so the travel day itself is still checked in every time zone.
    if now >= parse_iso(t["ends_at"]) or now.date() > travel + dt.timedelta(days=1):
        t["status"] = "finished"
        t["next_check_at"] = None
        return True
    return False


def run_check(data, t, forced=False):
    """Check one tracker. Returns a short message."""
    now = utcnow()
    if not forced:
        if t["status"] != "active":
            return "not active"
        if t.get("next_check_at") and now < parse_iso(t["next_check_at"]):
            return "not due"
    if t["status"] != "finished" and expire_if_done(t, now):
        return "tracking period is over"
    if now.date() > dt.date.fromisoformat(t["travel_date"]) + dt.timedelta(days=1):
        return "travel date has passed"
    if not api_key():
        return "no SerpApi key"

    readings = t.setdefault("readings", [])
    last = readings[-1] if readings else None
    try:
        # Skip the second (deep) search if the flight was already missing last time,
        # so a mistyped flight number does not use up the monthly allowance.
        r, google_url = lookup_price(data, t, allow_deep=not (last and last["status"] == "not_found"))
    except ApiError as e:
        msg = str(e)
        if not (last and last["status"] == "error" and last.get("detail") == msg):
            readings.append({"checked_at": iso(now), "status": "error", "price": None,
                             "currency": t["currency"], "seller": None, "sellers": [],
                             "dep_time": None, "arr_time": None, "via": None, "detail": msg})
        t["next_check_at"] = iso(now + dt.timedelta(seconds=RETRY_AFTER_ERROR_SECONDS))
        return "failed: " + msg

    r["checked_at"] = iso(now)
    readings.append(r)
    t["last_checked_at"] = iso(now)
    # Aim a little before the exact interval so an hourly run that starts a few
    # minutes late still counts as "due".
    interval = 86400.0 / max(1, int(t["checks_per_day"]))
    t["next_check_at"] = iso(now + dt.timedelta(seconds=interval - 20 * 60))
    if google_url:
        t["google_url"] = google_url
    return "price recorded" if r["status"] == "ok" else (r.get("detail") or "flight not found")


def run_due(data):
    now = utcnow()
    for t in data["trackers"]:
        if t["status"] != "active":
            continue
        if expire_if_done(t, now):
            print("tracker %s: finished" % t["id"])
            continue
        if not t.get("next_check_at") or now >= parse_iso(t["next_check_at"]):
            print("tracker %s: %s" % (t["id"], run_check(data, t)))


# --------------------------------------------------------------------------- #
# Requests from the page (or from GitHub's "Run workflow" form)
# --------------------------------------------------------------------------- #


def add_tracker(data, req):
    origin = req.get("origin", "").strip().upper()
    dest = req.get("destination", "").strip().upper()
    for label, code in (("From", origin), ("To", dest)):
        if not re.fullmatch(r"[A-Z]{3}", code):
            raise ValueError("%s must be a 3-letter airport code, for example VTZ." % label)
    if origin == dest:
        raise ValueError("From and To cannot be the same airport.")
    try:
        travel = dt.date.fromisoformat(req.get("travel_date", "").strip())
    except ValueError:
        raise ValueError("The travel date must look like 2026-12-16.")
    if travel < utcnow().date():
        raise ValueError("The travel date is in the past.")
    flights = parse_flights(req.get("flights", ""))
    if not flights:
        raise ValueError("Enter the flight number, for example 6E731. For a connecting "
                         "flight enter each flight in order: 6E731, 6E120.")
    if len(flights) > 4:
        raise ValueError("At most 4 connecting flights are supported.")
    try:
        period = int(req.get("period_days") or 30)
        per_day = int(req.get("checks_per_day") or 2)
    except ValueError:
        raise ValueError("Tracking period and checks per day must be numbers.")
    if not 1 <= period <= 120:
        raise ValueError("Tracking period must be between 1 and 120 days.")
    if per_day not in (1, 2, 4):
        raise ValueError("Checks per day must be 1, 2 or 4.")
    currency = (req.get("currency") or "INR").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("Currency must be a 3-letter code such as INR.")
    if len(data["trackers"]) >= MAX_TRACKERS:
        raise ValueError("There are already %d tracked flights. Delete one first." % MAX_TRACKERS)
    now = utcnow()
    t = {
        "id": data["next_id"], "origin": origin, "destination": dest,
        "travel_date": travel.isoformat(), "flights": flights, "period_days": period,
        "checks_per_day": per_day, "currency": currency,
        "with_sellers": str(req.get("with_sellers", "true")).strip().lower() not in ("false", "0", "no", ""),
        "status": "active", "created_at": iso(now),
        "ends_at": iso(now + dt.timedelta(days=period)),
        "last_checked_at": None, "next_check_at": None, "google_url": None, "readings": [],
    }
    data["next_id"] += 1
    data["trackers"].append(t)
    return t


def handle_request(data):
    req = {k[4:].lower(): v for k, v in os.environ.items() if k.startswith("REQ_")}
    action = req.get("action", "").strip().lower()
    data["notice"] = None
    if action in ("", "run-due"):
        run_due(data)
        return
    if action == "add":
        try:
            t = add_tracker(data, req)
        except ValueError as e:
            notice(data, "The flight was not added: %s" % e)
            return
        print("added tracker %s: %s" % (t["id"], run_check(data, t, forced=True)))
        return
    try:
        tid = int(req.get("tracker_id", "").strip())
    except ValueError:
        notice(data, "That request needs a tracker number.")
        return
    t = tracker_by_id(data, tid)
    if not t:
        notice(data, "Tracker %s was not found. It may already have been deleted." % tid)
        return
    if action == "check":
        print("tracker %s: %s" % (tid, run_check(data, t, forced=True)))
    elif action == "pause":
        if t["status"] == "active":
            t["status"] = "paused"
    elif action == "resume":
        if t["status"] == "paused":
            t["status"] = "active"
            t["next_check_at"] = None
            print("tracker %s: %s" % (tid, run_check(data, t)))
    elif action == "delete":
        data["trackers"] = [x for x in data["trackers"] if x["id"] != tid]
    else:
        notice(data, "Unknown request: %s" % action[:30])


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "run-due"
    data = load()
    before = json.dumps(data, sort_keys=True)
    had_key = data.get("has_key")
    if command == "request":
        handle_request(data)
    else:
        run_due(data)
    if not api_key():
        print("SERPAPI_KEY is not set: no prices can be fetched.")
    # Only rewrite the file (and so only commit) when something changed.
    if json.dumps(data, sort_keys=True) != before or had_key != bool(api_key()):
        save(data)
        print("data.json updated")
    else:
        print("nothing to update")


if __name__ == "__main__":
    main()
