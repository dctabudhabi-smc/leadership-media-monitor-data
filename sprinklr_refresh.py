#!/usr/bin/env python3
"""Leadership Media Monitor - Sprinklr refresh.

Runs the Sprinklr widget payloads saved in widgets/, turns the results into the
dashboard's data.json, and marks each dashboard section Live once its widget
returns data. Sections without a widget keep their illustrative data.

Commands
  python sprinklr_refresh.py token        refresh the access token and show token status
  python sprinklr_refresh.py discover     run the received widgets for the last 30 days, list topic IDs and names
  python sprinklr_refresh.py build        fetch everything, check it, write data.json
  python sprinklr_refresh.py build --dry-run   fetch and check, do not write data.json

Credentials come from environment variables or a .env file next to this script:
  SPRINKLR_CLIENT_ID, SPRINKLR_CLIENT_SECRET, SPRINKLR_REFRESH_TOKEN (first run only)
Use --access-token <token> to run with an access token someone else refreshed (nothing rotates).

Token rule: Sprinklr keeps one token pair per API key and every refresh token works once.
This script saves the new refresh token to .tokens.json before it does anything else.
Do not refresh with the same API key anywhere else.

Standard library only (Python 3.9 or later).
"""
import argparse, copy, datetime as dt, json, os, pathlib, re, sys, time
import urllib.error, urllib.parse, urllib.request
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
REG = json.loads((ROOT / "registry.json").read_text(encoding="utf-8"))["widgets"]
TOKENS = ROOT / ".tokens.json"
RAW = ROOT / "raw"
# comparison sets: "business" (registry.json, leaders.json) and "culture" (registry_culture.json, leaders_culture.json)
PROFILES = {"business": ("registry.json", "leaders.json", "raw"), "culture": ("registry_culture.json", "leaders_culture.json", "raw_culture")}
PROFILE = {"name": "business", "leaders": "leaders.json", "topic_ids": None}
NAME_MAP = {}  # topic name -> leader id, looked up from the topic IDs (culture)
TZ = ZoneInfo(CFG["timezone"])
def base():
    return f'{os.environ.get("SPRINKLR_API_HOST", CFG["api_host"]).rstrip("/")}/{CFG["env"]}'
ACCESS_OVERRIDE = None


# ---------------------------------------------------------------- credentials
def load_env():
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def need(name):
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"Missing {name}. Set it as an environment variable or in .env (see .env.example).")
    return v


def http(method, url, data=None, headers=None, form=False, timeout=90):
    body = None
    headers = dict(headers or {})
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def save_tokens(t):
    TOKENS.write_text(json.dumps(t, indent=2), encoding="utf-8")
    try:
        os.chmod(TOKENS, 0o600)
    except OSError:
        pass
    with open(ROOT / "token_log.txt", "a", encoding="utf-8") as log:
        log.write(f'{dt.datetime.now(TZ).isoformat(timespec="seconds")}  refreshed; new refresh token ends ...{t["refresh_token"][-8:]}\n')


def access_token(force=False):
    if ACCESS_OVERRIDE:
        return ACCESS_OVERRIDE
    t = json.loads(TOKENS.read_text(encoding="utf-8")) if TOKENS.exists() else {}
    if not force and t.get("access_token") and t.get("expires_at", 0) > time.time() + 300:
        return t["access_token"]
    refresh = t.get("refresh_token") or need("SPRINKLR_REFRESH_TOKEN")
    params = {"client_id": need("SPRINKLR_CLIENT_ID"), "client_secret": need("SPRINKLR_CLIENT_SECRET"),
              "grant_type": "refresh_token", "refresh_token": refresh}
    # one attempt only: a refresh token works once, so never retry with the same token
    code, text = http("POST", f"{base()}/oauth/token", params, form=True)
    if code != 200 or "access_token" not in text:
        sys.exit(f"Token refresh failed ({code}): {text[:300]}\n"
                 "The refresh token was probably already used. Get the newest one from the token owner "
                 "or generate a new one (manual Step 2), put it in SPRINKLR_REFRESH_TOKEN and delete .tokens.json.")
    j = json.loads(text)
    t = {"access_token": j["access_token"], "refresh_token": j["refresh_token"],
         "expires_at": int(time.time()) + int(j.get("expires_in", 28799)),
         "refreshed_at": dt.datetime.now(TZ).isoformat(timespec="seconds")}
    save_tokens(t)  # save the new refresh token before anything else
    print(f'Token refreshed. New refresh token saved to .tokens.json (ends ...{t["refresh_token"][-8:]}). '
          "If your team keeps a token log, record it now.")
    return t["access_token"]


# ---------------------------------------------------------------- reporting API
def run_payload(payload, label, max_rows=None):
    """POST the payload, page through every result, return (headings, rows as dicts)."""
    tok = access_token()
    headers = {"Authorization": f"Bearer {tok}", "key": need("SPRINKLR_CLIENT_ID")}
    p = copy.deepcopy(payload)
    p["page"], p["pageSize"] = 0, min(CFG["page_size"], max_rows) if max_rows else CFG["page_size"]
    headings, rows, pages = None, [], []
    while True:
        code, text = http("POST", f"{base()}/api/v2/reports/query", p, headers=headers)
        if code == 401 and not ACCESS_OVERRIDE:
            headers["Authorization"] = f"Bearer {access_token(force=True)}"
            code, text = http("POST", f"{base()}/api/v2/reports/query", p, headers=headers)
        if code == 403 and "Over Rate" in text:
            print("  rate limit, waiting 60 s"); time.sleep(60); continue
        if code != 200:
            sys.exit(f"{label}: report query failed ({code}): {text[:400]}")
        j = json.loads(text)
        pages.append(j)
        data = j.get("data") or {}
        if isinstance(data, list):  # some responses put rows straight under data
            data = {"rows": data}
        headings = headings or data.get("headings") or data.get("headers") or [
            x.get("heading") for x in (payload.get("groupBys") or []) + (payload.get("projections") or [])]
        page_rows = data.get("rows") or []
        # compare dimension values only: a widget that ignores 'page' repeats the same rows, sometimes with
        # counts that moved by one or two between calls, which a full-row compare would miss
        sig = json.dumps([[c for c in (r.values() if isinstance(r, dict) else r) if not isinstance(c, (int, float))]
                          for r in page_rows[:5]], default=str)[:2000]
        if pages[:-1] and sig == getattr(run_payload, "_last_sig", None):  # some chart widgets ignore 'page' and repeat page 0
            break
        run_payload._last_sig = sig
        rows += page_rows
        if not data.get("hasMore") or (max_rows and len(rows) >= max_rows) or p["page"] >= 60:
            break
        p["page"] += 1
        time.sleep(CFG["pause_between_calls_s"])
    RAW.mkdir(exist_ok=True)
    (RAW / f'{label}.json').write_text(json.dumps(pages, ensure_ascii=False)[:5_000_000], encoding="utf-8")
    dicts = []
    for r in rows:
        if isinstance(r, dict):
            dicts.append(r)
        else:
            dicts.append({h: v for h, v in zip(headings, r)})
    return headings, dicts


def with_window(payload, start, end):
    p = copy.deepcopy(payload)
    p["startTime"] = int(start.timestamp() * 1000)
    p["endTime"] = int(end.timestamp() * 1000) - 1
    p["timeZone"] = CFG["timezone"]
    p.setdefault("additional", {})["Timezone"] = CFG["timezone"]
    return p


def windows(today=None):
    """Rolling windows in the dashboard time zone. With include_today (the default, matching Sprinklr's
    "Last N days") a window ends now and includes today; otherwise it ends with yesterday.
    Returns (windows, end) where end is the exclusive end: midnight after the last included day."""
    midnight = today or dt.datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    today = midnight + dt.timedelta(days=1) if CFG.get("include_today", True) else midnight
    last = today - dt.timedelta(days=1)
    out = {}
    for per in CFG["periods"]:
        if per == "YTD":
            s = last.replace(month=1, day=1)
            ps = s.replace(year=s.year - 1)
            pe = today.replace(year=today.year - 1)
        else:
            n = int(per[:-1])
            s = today - dt.timedelta(days=n)
            ps, pe = s - dt.timedelta(days=n), s
        out[per] = {"cur": (s, today), "prev": (ps, pe)}
    return out, today


def heading_for(payload, kind, name):
    """Find the response heading of a groupBy dimension or projection measurement."""
    items = payload.get("groupBys" if kind == "dim" else "projections") or []
    key = "dimensionName" if kind == "dim" else "measurementName"
    for it in items:
        if it.get(key) == name:
            return it.get("heading")
    return None


def num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- leaders
def leaders_file():
    return json.loads((ROOT / PROFILE["leaders"]).read_text(encoding="utf-8"))


def leader_map():
    L = leaders_file()
    m = {}
    for lid, v in L.items():
        if lid.startswith("_"):
            continue
        for t in v.get("topic_ids", []):
            m[str(t)] = lid
        if v.get("topic_id"):
            m[str(v["topic_id"])] = lid
    m.update(NAME_MAP)
    return m


def use_profile(name):
    """Switch widgets, leaders and raw folder between the business and culture comparison sets."""
    global REG, RAW
    reg, leaders, raw = PROFILES[name]
    REG = json.loads((ROOT / reg).read_text(encoding="utf-8"))["widgets"]
    RAW = ROOT / raw
    PROFILE.update({"name": name, "leaders": leaders, "topic_ids": None})
    NAME_MAP.clear()
    if name != "business":  # tag-based widgets read this set's topics by ID, so the topic tag does not matter
        PROFILE["topic_ids"] = [v["topic_id"] for k, v in leaders_file().items() if not k.startswith("_") and v.get("topic_id")]


def resolve_names():
    """Sprinklr returns topic names for TOPIC_IDS: look each topic's name up from its ID (one small call each)."""
    payload = load_widget("topic_totals")
    if not payload:
        return
    wins, _ = windows()
    s, e = wins["YTD"]["cur"]
    th = heading_for(payload, "dim", "TOPIC_IDS")
    for lid, v in leaders_file().items():
        if lid.startswith("_") or not v.get("topic_id"):
            continue
        _, rows = run_payload(with_window(with_topic(payload, v["topic_id"]), s, e), f"names_{lid}")
        for r in rows:
            tid, name = topic_value(r, th)
            for x in (tid, name):
                if x and x != "None":
                    NAME_MAP[str(x)] = lid


def topic_value(row, h):
    v = row.get(h)
    if isinstance(v, dict):  # resolved values can come as {"id":..., "name":...}
        return str(v.get("id") or v.get("value") or v.get("name")), v.get("name") or v.get("displayName")
    return str(v), None


# ---------------------------------------------------------------- widget handlers
def load_widget(role, raw=False):
    w = REG[role]
    f = ROOT / w["file"]
    if not f.exists():
        return None
    p = json.loads(f.read_text(encoding="utf-8"))
    ids = PROFILE.get("topic_ids")
    if not raw and ids and any(x.get("dimensionName") == "TOPIC_TAGS" for x in p.get("filters", [])):
        p["filters"] = [x for x in p["filters"] if x.get("dimensionName") != "TOPIC_TAGS"] + [
            {"dimensionName": "TOPIC_IDS", "filterType": "IN", "values": ids, "details": {"type": "DIMENSION", "OLD_DIM_NAME": "TOPIC", "dF": True}}]
    return p


def per_period(role, payload, measures, extra_dim=None, sides=("cur", "prev")):
    """Run a Topic (+ one dimension) widget for every period (and comparison period).
    Returns {period: {side: {leader: {measure: value} or {dim_value: {measure: value}}}}}"""
    tmap = leader_map()
    th = heading_for(payload, "dim", "TOPIC_IDS")
    dh = heading_for(payload, "dim", extra_dim) if extra_dim else None
    mh = {m: heading_for(payload, "proj", m) for m in measures}
    wins, _ = windows()
    out = {}
    for per, w in wins.items():
        out[per] = {}
        for side in sides:
            s, e = w[side]
            _, rows = run_payload(with_window(payload, s, e), f"{role}_{per}_{side}")
            out[per][side] = aggregate(rows, tmap, th, dh, mh)
    return out


def aggregate(rows, tmap, th, dh, mh):
    agg = {}
    for r in rows:
        lid = tmap.get(topic_value(r, th)[0])
        if not lid:
            continue
        vals = {m: num(r.get(h)) for m, h in mh.items() if h}
        if dh:
            dv, dname = topic_value(r, dh)
            slot = agg.setdefault(lid, {}).setdefault(dname or dv, {m: 0.0 for m in vals})
        else:
            slot = agg.setdefault(lid, {m: 0.0 for m in vals})
        for m, v in vals.items():
            slot[m] = slot.get(m, 0.0) + v
    return agg


def h_topic_totals(payload):
    return per_period("topic_totals", payload, ["MENTIONS_COUNT", "OVERALL_REACH", "EMV", "DISTINCT_DOMAIN_COUNT"])


def h_sentiment(payload):
    return per_period("sentiment_topic", payload, ["MENTIONS_COUNT"], extra_dim="SEM_SENTIMENT")


def with_topic(payload, topic_id):
    p = copy.deepcopy(payload)
    p["filters"] = [f for f in p.get("filters", []) if f.get("dimensionName") not in ("TOPIC_TAGS", "TOPIC_IDS")] + [
        {"dimensionName": "TOPIC_IDS", "filterType": "IN", "values": [topic_id], "details": {"type": "DIMENSION", "OLD_DIM_NAME": "TOPIC", "dF": True}}]
    return p


CHART_CAP = 100  # dimension widgets return at most 100 rows and ignore 'page'
ISO_OVERRIDES = {
    "united arab emirates": "AE", "uae": "AE", "united states": "US", "united kingdom": "GB", "turkey": "TR",
    "ivory coast": "CI", "palestinian territory": "PS", "palestine": "PS", "kosovo": "XK", "russia": "RU",
    "south korea": "KR", "north korea": "KP", "iran": "IR", "vietnam": "VN", "laos": "LA", "syria": "SY",
    "bolivia": "BO", "venezuela": "VE", "tanzania": "TZ", "moldova": "MD", "czech republic": "CZ", "taiwan": "TW",
    "macedonia": "MK", "north macedonia": "MK", "brunei": "BN", "cape verde": "CV", "swaziland": "SZ",
    "micronesia": "FM", "democratic republic of the congo": "CD", "republic of the congo": "CG", "congo": "CG",
    "curacao": "CW", "vatican": "VA", "saint martin": "MF", "u.s. virgin islands": "VI",
    "british virgin islands": "VG", "reunion": "RE", "hong kong": "HK", "macao": "MO", "macau": "MO"}


def iso2(name):
    """Sprinklr country name -> ISO 3166 alpha-2 code, for the COUNTRY filter. None when unsure."""
    key = str(name or "").strip().lower()
    if key in ISO_OVERRIDES:
        return ISO_OVERRIDES[key]
    try:
        import pycountry
        return pycountry.countries.lookup(key).alpha_2
    except Exception:
        return None


def all_countries(payload, s, e, label, dh):
    """Every country row for one window. Each call returns the top 100 countries only, so the next call
    excludes the countries already received (by ISO code) until a call comes back with fewer than 100."""
    seen, rows_all, extra, headings = set(), [], [], None
    for i in range(12):
        p = with_window(payload, s, e)
        for f in p.get("filters", []):
            if f.get("dimensionName") == "COUNTRY" and f.get("filterType") == "NIN":
                f["values"] = list(f.get("values") or []) + extra
                break
        else:
            p["filters"].append({"dimensionName": "COUNTRY", "filterType": "NIN", "values": extra,
                                 "details": {"type": "DIMENSION", "OLD_DIM_NAME": "COUNTRY"}})
        headings, rows = run_payload(p, f"{label}_{i}", max_rows=CHART_CAP)
        new = [r for r in rows if topic_value(r, dh)[0] not in seen]
        rows_all += new
        seen.update(topic_value(r, dh)[0] for r in rows)
        if len(rows) < CHART_CAP:
            return headings, rows_all
        codes = [c for c in (iso2(topic_value(r, dh)[0]) for r in rows) if c and c not in extra]
        if not codes:
            print(f"  {label}: could not map the remaining country names to ISO codes; list may be incomplete")
            return headings, rows_all
        extra += codes
    print(f"  {label}: stopped after 12 calls; list may be incomplete")
    return headings, rows_all


def country_period(role, payload, measures):
    tmap = leader_map()
    th, dh = heading_for(payload, "dim", "TOPIC_IDS"), heading_for(payload, "dim", "COUNTRY")
    mh = {m: heading_for(payload, "proj", m) for m in measures}
    wins, _ = windows()
    out = {}
    for per, w in wins.items():
        s, e = w["cur"]
        _, rows = all_countries(payload, s, e, f"{role}_{per}_cur", dh)
        out[per] = {"cur": aggregate(rows, tmap, th, dh, mh)}
    return out


def h_breakdown(role, dim, measures):
    """Dimension x Topic widgets return at most 100 rows, so run them once per leader to keep each leader's long tail.
    Countries go further: see all_countries."""
    def run(payload):
        L = leaders_file()
        out = {}
        for lid, v in L.items():
            if lid.startswith("_") or not v.get("topic_id"):
                continue
            if dim == "COUNTRY":
                part = country_period(f"{role}_{lid}", with_topic(payload, v["topic_id"]), measures)
            else:
                part = per_period(f"{role}_{lid}", with_topic(payload, v["topic_id"]), measures, extra_dim=dim, sides=("cur",))
            for per, sides in part.items():
                for side, agg in sides.items():
                    out.setdefault(per, {}).setdefault(side, {}).update(agg)
        return out
    return run


def quarter_start(today):
    return today.replace(month=(today.month - 1) // 3 * 3 + 1, day=1)


def h_daily_all(_payload):
    """Daily mentions, reach and EMV per leader from history_start to yesterday (three widgets)."""
    tmap = leader_map()
    _, today = windows()
    start = dt.datetime.fromisoformat(CFG["history_start"]).replace(tzinfo=TZ)
    n = (today - start).days
    fx = CFG["usd_to_aed"] if CFG["emv_currency"] == "USD" else 1.0
    series = {lid: {"mentions": [0] * n, "reach_k": [0] * n, "ave_k": [0] * n} for lid in set(tmap.values())}
    found = []
    for role, key, measure, scale in [("daily_mentions", "mentions", "MENTIONS_COUNT", 1),
                                      ("daily_reach", "reach_k", "OVERALL_REACH", 1 / 1000),
                                      ("daily_emv", "ave_k", "EMV", fx / 1000)]:
        payload = load_widget(role)
        if not payload:
            continue
        found.append(key)
        _, rows = run_payload(with_window(payload, start, today), role)
        th = heading_for(payload, "dim", "TOPIC_IDS")
        dh = next(g["heading"] for g in payload["groupBys"] if g["dimensionName"] != "TOPIC_IDS")
        mh = heading_for(payload, "proj", measure)
        for r in rows:
            lid = tmap.get(topic_value(r, th)[0])
            if not lid:
                continue
            dv = r.get(dh)
            day = dt.datetime.fromtimestamp(int(float(dv)) / 1000, TZ) if str(dv).replace(".", "").isdigit() \
                else dt.datetime.fromisoformat(str(dv)[:10]).replace(tzinfo=TZ)
            i = (day.replace(hour=0, minute=0, second=0, microsecond=0) - start).days
            if 0 <= i < n:
                series[lid][key][i] += round(num(r.get(mh)) * scale)
    return {"start": CFG["history_start"], "series": series, "measures": found}


def leader_of(role):
    return REG[role].get("leader")


def h_leader_list(dim):
    """Per-leader list widget (publications by domain, mentions by theme), run for every period."""
    def run(payload, role):
        wins, _ = windows()
        dh = heading_for(payload, "dim", dim)
        mh = heading_for(payload, "proj", "MENTIONS_COUNT")
        zh = heading_for(payload, "proj", "MOZ_RANK")
        out = {}
        for per, w in wins.items():
            s, e = w["cur"]
            _, rows = run_payload(with_window(payload, s, e), f"{role}_{per}", max_rows=200)
            items = []
            for r in rows:
                name = topic_value(r, dh)[1] or topic_value(r, dh)[0]
                if name and name.startswith("LMN"):  # theme names carry the theme tag prefix (LMN | , LMN Culture | )
                    name = re.sub(r"^LMN[^|]{0,30}\|\s*", "", name)
                if not name or name.lower() in ("none", "null", "unknown"):
                    continue
                items.append({"name": name, "mentions": int(num(r.get(mh))), "moz": round(num(r.get(zh)), 1) if zh else None})
            if not zh:  # themes: largest first; publications keep the widget's own order (Moz rank)
                items.sort(key=lambda x: -x["mentions"])
            out[per] = items[:10]
        return out
    return run


def parse_clip(r, H, M, lid, fx):
    msg = r.get(H.get("ES_MESSAGE_ID")) or {}
    if not isinstance(msg, dict) or not msg.get("permalink"):
        return None
    title = (msg.get("title") or str(r.get(H.get("MESSAGE_CONTENT"), "")).split("\n")[0]).strip().lstrip("\u2022\u00b7 ").strip()
    if "clipped from video" in title.lower() or re.match(r"^video\s+\d+:\d+", title, re.I):  # video page with no headline
        return None
    if not title or sum(title.count(ch) for ch in "ŘŮŰÃâ€") > 3:  # garbled encoding at source
        return None
    outlet = ""
    for sep in (" | ", " - ", " – "):
        if sep in title:
            title, outlet = title.rsplit(sep, 1)[0].strip(), title.rsplit(sep, 1)[1].strip()
            break
    domain = str(r.get(H.get("DOMAINS"), "") or (msg.get("senderProfile") or {}).get("publisherName", ""))
    if not outlet or len(outlet) > 40:
        outlet = domain
    when = dt.datetime.fromtimestamp(int(float(r.get(H.get("SN_CREATED_TIME"), 0) or 0)) / 1000, TZ)
    return {"leader": lid, "date": when.strftime("%Y-%m-%d"), "outlet": outlet, "domain": domain,
            "market": norm_country(r.get(H.get("COUNTRY"))), "lang": norm_lang(r.get(H.get("LST_SUPP_LNG"))),
            "sent": sentiment_bucket(r.get(H.get("SEM_SENTIMENT"))), "topic": title[:180],
            "reach": int(num(r.get(M.get("OVERALL_REACH")))), "ave": round(num(r.get(M.get("EMV"))) * fx),
            "moz": round(num(r.get(M.get("MOZ_RANK"))), 1) if M.get("MOZ_RANK") else None,
            "url": msg["permalink"], "kind": "article"}


def h_clippings(payload, role):
    """Top clippings for one leader in every period, in the widget's own order (Moz rank)."""
    lid = leader_of(role)
    wins, _ = windows()
    H = {g["dimensionName"]: g["heading"] for g in payload["groupBys"]}
    M = {x["measurementName"]: x["heading"] for x in payload["projections"]}
    fx = CFG["usd_to_aed"] if CFG["emv_currency"] == "USD" else 1.0
    out = {}
    for per, w in wins.items():
        s, e = w["cur"]
        _, rows = run_payload(with_window(payload, s, e), f"{role}_{per}", max_rows=CFG.get("clips_scan_rows", 80))
        clips, seen = [], set()
        for r in rows:
            c = parse_clip(r, H, M, lid, fx)
            if not c:
                continue
            if c["url"] in seen:  # keep Sprinklr's order and items; drop only exact repeats of the same article
                continue
            seen.add(c["url"])
            clips.append(c)
            if len(clips) == CFG.get("clips_per_leader", 12):
                break
        out[per] = clips
    return out


def _plain(v):
    """Lower-case letters and digits only (Arabic letters kept), for comparing an author with a domain."""
    return re.sub(r"[^a-z0-9\u0600-\u06ff]", "", str(v or "").lower())


def author_name(v):
    """FROM_SN_USER can come resolved as a profile dict or as a plain string; tidy byline noise."""
    if isinstance(v, dict):
        n = next((str(v[k]) for k in ("name", "displayName", "screenName", "fullName", "userName") if v.get(k)), "")
        n = n or str((v.get("contactInfo") or {}).get("fullName") or "")
    else:
        n = str(v or "")
    n = re.sub(r"^\s*written\s*by\s*", "", n.strip(), flags=re.I)  # "Written byName"
    n = re.sub(r"^\s*(by|\u0645\u0646 \u0642\u0628\u0644|\u0628\u0642\u0644\u0645)\s+", "", n, flags=re.I)  # By, Arabic "by"
    n = re.sub(r"^\u0631\u0633\u0627\u0644\u0629\s+\S+\s+", "", n)  # Arabic "dispatch from <city>" before the name
    parts = n.split(" - ", 1)
    if len(parts) == 2 and parts[1].strip() and len(parts[0].split()) <= 2:  # dateline first: "Abu Dhabi - Name"
        n = parts[1]
    n = re.sub(r",\s*live updates?\s*$", "", n, flags=re.I)
    return n.strip(" |-\u2013\u00bb\u00ab").strip()


NOT_PEOPLE = {"anonymous", "anonymous user", "unknown", "none", "null", "admin", "administrator", "staff", "editor", "web desk", "news desk"}
GENERIC_LABELS = {"www", "com", "co", "net", "org", "gov", "edu", "news", "ae", "uk", "in", "us", "cn", "sa", "qa", "eg", "fr", "de"}


def is_outlet_itself(author, domain):
    """True when the 'author' is just the publication or agency (apnews.com on apnews.com, The Gulf Today on
    gulftoday.ae, Asianet News on newsable.asianetnews.com, ANI on aninews.in), or is on the exclusion list."""
    a = _plain(re.sub(r"^https?://|^www\.", "", str(author).lower().strip()))
    if not a or str(author).strip().lower() in NOT_PEOPLE:
        return True
    if re.search(r"[:\d@/]", str(author)) or len(str(author).split()) > 8:  # headlines or scraping errors, not bylines
        return True
    if any(_plain(x) == a for x in CFG.get("author_exclude", [])):
        return True
    if any(a.startswith(_plain(x)) for x in CFG.get("author_exclude_prefix", []) if _plain(x)):
        return True
    d = re.sub(r"^https?://", "", str(domain or "").lower().strip()).removeprefix("www.").rstrip("/")
    if not d:
        return False
    if a == _plain(d):
        return True
    a2 = a[3:] if a.startswith("the") else a
    for lab in (x for x in d.split(".") if x not in GENERIC_LABELS and len(x) >= 3):
        lab = _plain(lab)
        if a in (lab,) or a2 == lab:
            return True
        if len(lab) >= 6 and lab in a:        # Tech - Economic Times on economictimes.indiatimes.com
            return True
        if len(a) >= 3 and lab.startswith(a2):  # ANI on aninews.in
            return True
        if a2.startswith(lab):  # BBC Monitoring on bbc.co.uk
            return True
    return False


def author_key(name):
    """Comparison key: case, spacing and Arabic letter variants (alif forms, final ya, ta marbuta) ignored."""
    k = " ".join(str(name).lower().split())
    for a, b in (("\u0623", "\u0627"), ("\u0625", "\u0627"), ("\u0622", "\u0627"), ("\u0649", "\u064a"), ("\u0629", "\u0647")):
        k = k.replace(a, b)
    return k


def merge_similar(items):
    """One row per person: merge rows whose key is the same, or a cut-off version of another (6+ letters)."""
    out = []
    for a in items:
        ka = author_key(a["name"])
        hit = next((b for b in out if author_key(b["name"]) == ka or (min(len(ka), len(author_key(b["name"]))) >= 6 and
                    (ka.startswith(author_key(b["name"])) or author_key(b["name"]).startswith(ka)))), None)
        if not hit:
            out.append(a)
            continue
        if len(a["name"]) > len(hit["name"]):
            hit["name"] = a["name"]
        for k in ("mentions", "pos", "neu", "neg"):
            hit[k] += a.get(k, 0)
        o = dict(hit["outlets"]) if isinstance(hit["outlets"], dict) else {x: 0 for x in hit["outlets"]}
        for x, n in (a["outlets"].items() if isinstance(a["outlets"], dict) else ((x, 0) for x in a["outlets"])):
            o[x] = o.get(x, 0) + n
        hit["outlets"] = o
    return out


def h_authors(payload, role):
    """Top authors for one leader in every period: one row per author (no duplicates across sentiment or
    outlets), Sprinklr's order (Moz rank), authors that are only the outlet's own name left out."""
    wins, _ = windows()
    dh = heading_for(payload, "dim", "DOMAINS")
    uh = heading_for(payload, "dim", "FROM_SN_USER")
    sh = heading_for(payload, "dim", "SEM_SENTIMENT")
    mh = heading_for(payload, "proj", "MENTIONS_COUNT")
    zh = heading_for(payload, "proj", "MOZ_RANK")
    out = {}
    for per, w in wins.items():
        s, e = w["cur"]
        _, rows = run_payload(with_window(payload, s, e), f"{role}_{per}", max_rows=CFG.get("authors_scan_rows", 300))
        by, order = {}, []
        for r in rows:
            name = author_name(r.get(uh))
            domain = str(topic_value(r, dh)[1] or topic_value(r, dh)[0] or "").strip()
            if not name or is_outlet_itself(name, domain):
                continue
            key = author_key(name)
            a = by.get(key)
            if not a:
                a = by[key] = {"name": name, "outlets": {}, "mentions": 0, "pos": 0, "neu": 0, "neg": 0,
                               "moz": round(num(r.get(zh)), 1) if zh else None}
                order.append(key)
            n = int(num(r.get(mh)))
            a["mentions"] += n
            a[sentiment_bucket(r.get(sh))] += n
            if domain and domain.lower() not in ("none", "null", "unknown"):
                a["outlets"][domain] = a["outlets"].get(domain, 0) + n
        items = []
        for a in merge_similar([by[k] for k in order])[:CFG.get("authors_per_leader", 10)]:
            outs = sorted(a["outlets"].items(), key=lambda x: -x[1])
            a["outlet"] = outs[0][0] if outs else ""
            a["outlets"] = [o for o, _ in outs]
            items.append(a)
        out[per] = items
    return out


HANDLERS = {
    "topic_totals": h_topic_totals,
    "topic_share": lambda p: per_period("topic_share", p, ["MENTIONS_COUNT"]),
    "sentiment_topic": h_sentiment,
    "daily_mentions": h_daily_all,  # also runs daily_reach and daily_emv
    "country_topic": h_breakdown("country_topic", "COUNTRY", ["MENTIONS_COUNT", "EMV"]),
    "language_topic": h_breakdown("language_topic", "LST_SUPP_LNG", ["MENTIONS_COUNT"]),
    "source_topic": h_breakdown("source_topic", "LISTENING_MEDIA_TYPE", ["MENTIONS_COUNT"]),
}
PER_LEADER = {"publications_": h_leader_list("DOMAINS"), "themes_": h_leader_list("LST_THEME"), "clippings_": h_clippings, "authors_": h_authors}
SKIP = {"daily_reach", "daily_emv"}  # run inside the daily_mentions handler


def handler_for(role):
    if role in HANDLERS:
        return lambda p: HANDLERS[role](p)
    for prefix, fn in PER_LEADER.items():
        if role.startswith(prefix):
            return lambda p, fn=fn: fn(p, role)
    return None


def sentiment_bucket(label):
    l = str(label).lower()
    if l.startswith("pos") or l in ("1", "positive"):
        return "pos"
    if l.startswith("neg") or l in ("-1", "negative"):
        return "neg"
    return "neu"


# Sprinklr labels -> the names the dashboard uses (flags, icons, Arabic/English split)
COUNTRY = {"united arab emirates": "UAE", "uae": "UAE", "ae": "UAE", "saudi arabia": "Saudi Arabia", "sa": "Saudi Arabia",
           "kingdom of saudi arabia": "Saudi Arabia", "united kingdom": "United Kingdom", "gb": "United Kingdom", "uk": "United Kingdom",
           "united states": "United States", "united states of america": "United States", "us": "United States", "usa": "United States",
           "india": "India", "in": "India", "china": "China", "cn": "China", "france": "France", "fr": "France", "germany": "Germany",
           "de": "Germany", "egypt": "Egypt", "eg": "Egypt", "kuwait": "Kuwait", "kw": "Kuwait", "qatar": "Qatar", "qa": "Qatar",
           "italy": "Italy", "it": "Italy", "spain": "Spain", "es": "Spain", "japan": "Japan", "jp": "Japan", "russia": "Russia",
           "russian federation": "Russia", "ru": "Russia"}
LANG = {"ar": "Arabic", "arabic": "Arabic", "en": "English", "english": "English", "fr": "French", "french": "French",
        "zh": "Chinese", "chinese": "Chinese", "es": "Spanish", "spanish": "Spanish", "de": "German", "german": "German",
        "ru": "Russian", "russian": "Russian", "it": "Italian", "italian": "Italian", "pt": "Portuguese", "portuguese": "Portuguese",
        "ja": "Japanese", "japanese": "Japanese", "hi": "Hindi", "hindi": "Hindi"}
CHANNEL = {"news": "Online news", "print": "Print", "newspaper": "Print", "magazine": "Print", "tv": "Broadcast",
           "broadcast": "Broadcast", "radio": "Broadcast", "blogs": "Blogs and forums", "blog": "Blogs and forums",
           "forums": "Blogs and forums", "forum": "Blogs and forums", "message_boards": "Blogs and forums",
           "podcasts": "Podcasts", "podcast": "Podcasts"}
CHANNELS = ["Online news", "Print", "Broadcast", "Social", "Blogs and forums", "Podcasts"]


def norm_country(v):
    key = str(v or "").strip().lower()
    if key in ("", "unknown", "none", "null", "n/a"):
        return "Unknown"
    return COUNTRY.get(key, str(v).strip())


def norm_lang(v):
    key = str(v).split("(")[0].strip().lower()  # Sprinklr returns e.g. "Arabic (العربية)"
    return LANG.get(key, key.title() if key and key not in ("unknown", "none", "null") else "Other")


def norm_channel(v):
    return CHANNEL.get(str(v).strip().lower(), "Social")  # every social network falls under Social


def regroup(agg, fn):
    out = {}
    for lid, d in agg.items():
        for k, v in d.items():
            slot = out.setdefault(lid, {}).setdefault(fn(k), {})
            for m, x in v.items():
                slot[m] = slot.get(m, 0.0) + x
    return out


def merge(data, results, checks):
    fx = CFG["usd_to_aed"] if CFG["emv_currency"] == "USD" else 1.0
    meta = data.setdefault("meta", {})
    sections = meta["sections"] = {}  # rebuilt on every run from what was actually fetched
    leaders = [l["id"] for l in data["leaders"]]
    wins, _ = windows()
    day = lambda x: x.strftime("%Y-%m-%d")

    if "topic_totals" in results:
        totals = {}
        for per, sides in results["topic_totals"].items():
            totals[per] = {}
            for side, agg in sides.items():
                totals[per][side] = {lid: {
                    "mentions": int(agg.get(lid, {}).get("MENTIONS_COUNT", 0)),
                    "reach": int(agg.get(lid, {}).get("OVERALL_REACH", 0)),
                    "ave_aed": round(agg.get(lid, {}).get("EMV", 0) * fx),
                    "domains": int(agg.get(lid, {}).get("DISTINCT_DOMAIN_COUNT", 0))} for lid in leaders}
        data["totals"] = totals
        data["totals_windows"] = {per: {side: [day(x), day(y - dt.timedelta(days=1))] for side, (x, y) in w.items()} for per, w in wins.items()}
        sections["cards"] = sections["sov"] = "live"

    if "sentiment_topic" in results:
        for per, sides in results["sentiment_topic"].items():
            for side, agg in sides.items():
                for lid in leaders:
                    b = {"pos": 0, "neu": 0, "neg": 0}
                    for lab, v in agg.get(lid, {}).items():
                        b[sentiment_bucket(lab)] += int(v["MENTIONS_COUNT"])
                    data.setdefault("totals", {}).setdefault(per, {}).setdefault(side, {}).setdefault(lid, {}).update(b)
        sections["sentiment"] = "live"

    if "daily_mentions" in results and "mentions" in results["daily_mentions"]["measures"]:
        d = results["daily_mentions"]
        meta["start"] = d["start"]
        for lid, s in d["series"].items():
            old = data["series"].get(lid, {})
            om = sum(old.get("mentions", [])) or 1
            ratio = {k: sum(old.get(k, [])) / om for k in ("pos", "neg")}  # daily sentiment is not in the widgets
            st = (results.get("sentiment_topic") or {}).get("YTD", {}).get("cur", {}).get(lid)
            if st:  # split each day by the leader's year-to-date sentiment mix
                b = {"pos": 0, "neu": 0, "neg": 0}
                for lab, v in st.items():
                    b[sentiment_bucket(lab)] += v["MENTIONS_COUNT"]
                tot = sum(b.values()) or 1
                ratio = {"pos": b["pos"] / tot, "neg": b["neg"] / tot}
            s.update({k: [round(m * ratio[k]) for m in s["mentions"]] for k in ("pos", "neg")})
            s["neu"] = [m - p - n for m, p, n in zip(s["mentions"], s["pos"], s["neg"])]
            data["series"][lid] = s
        sections["trend"] = "live"

    def shares(agg, fn=None, top=None, measure="MENTIONS_COUNT", drop=None, counts=False):
        if fn:
            agg = regroup(agg, fn)
        dropped = {}
        if drop:
            for lid in leaders:
                d0 = agg.get(lid, {})
                all_ = sum(v.get(measure, 0) for v in d0.values()) or 1
                dropped[lid] = round(d0.pop(drop, {}).get(measure, 0) / all_, 4)
        names = sorted({k for lid in leaders for k in agg.get(lid, {}) if k != "Other"},
                       key=lambda k: -sum(agg.get(l, {}).get(k, {}).get(measure, 0) for l in leaders))
        if top and len(names) > top:
            for lid in leaders:
                dd = agg.setdefault(lid, {})
                o = dd.setdefault("Other", {})
                for k in names[top:]:
                    for m, x in dd.pop(k, {}).items():
                        o[m] = o.get(m, 0.0) + x
            names = names[:top]
        names.append("Other")
        share = {}
        for lid in leaders:
            tot = sum(agg.get(lid, {}).get(k, {}).get(measure, 0) for k in names) or 1
            share[lid] = [round(agg.get(lid, {}).get(k, {}).get(measure, 0) / tot, 4) for k in names]
        out = {"names": names, "share": share}
        if counts:  # raw mention counts, one per name, same order
            out["counts"] = {lid: [int(round(agg.get(lid, {}).get(k, {}).get(measure, 0))) for k in names] for lid in leaders}
        if drop:
            out["unknown_share"] = dropped
        return out

    bd = {}
    if "country_topic" in results:
        for per, sides in results["country_topic"].items():
            bd.setdefault(per, {})["markets"] = shares(sides["cur"], norm_country, top=None, drop="Unknown", counts=True)
        sections["markets"] = "live"
    if "language_topic" in results:
        for per, sides in results["language_topic"].items():
            bd.setdefault(per, {})["languages"] = shares(sides["cur"], norm_lang, top=11)
        sections["languages"] = "live"
    t1 = set(CFG.get("tier1_domains", []))
    pub_roles = [r for r in results if r.startswith("publications_")]
    for role in pub_roles:
        lid = leader_of(role)
        for per, items in results[role].items():
            total = sum(x["mentions"] for x in items) or 1
            bd.setdefault(per, {}).setdefault("publications", {})[lid] = [
                {"name": x["name"], "share": round(x["mentions"] / total, 4), "mentions": x["mentions"], "moz": x["moz"],
                 "tier": 1 if x["name"].lower().replace("www.", "") in t1 else 2, "market": "", "lang": "", "net": None} for x in items]
    if len(pub_roles) == len(leaders):
        sections["publications"] = "live"
    theme_roles = [r for r in results if r.startswith("themes_")]
    for role in theme_roles:
        lid = leader_of(role)
        for per, items in results[role].items():
            top = items[0]["mentions"] if items else 1
            bd.setdefault(per, {}).setdefault("topics", {})[lid] = [{"t": x["name"], "w": round(x["mentions"] / top * 100) if top else 0, "n": x["mentions"]} for x in items]
    if len(theme_roles) == len(leaders):
        sections["themes"] = "live"
    if "source_topic" in results:
        for per, sides in results["source_topic"].items():
            bd.setdefault(per, {})["channels"] = shares(sides["cur"], None, top=7)  # Sprinklr's own source names
        sections["media"] = "live"
    clip_roles = [r for r in results if r.startswith("clippings_")]
    for role in clip_roles:
        for per, items in results[role].items():
            bd.setdefault(per, {}).setdefault("clips", []).extend(items)
    if len(clip_roles) == len(leaders):
        sections["clippings"] = "live"
    author_roles = [r for r in results if r.startswith("authors_")]
    for role in author_roles:
        lid = leader_of(role)
        for per, items in results[role].items():
            bd.setdefault(per, {}).setdefault("authors", {})[lid] = items
    if len(author_roles) == len(leaders):
        sections["authors"] = "live"
    if bd:
        data["breakdowns"] = bd

    # payloads, so the dashboard's data checkpoint can show each one
    data["payloads"] = {role: {"title": w.get("title", role), "edited": w.get("edited"), "feeds": w.get("feeds", []),
                               "note": w.get("note", ""), "json": load_widget(role, raw=True)} for role, w in REG.items() if load_widget(role, raw=True)}

    meta["generated"] = dt.datetime.now(TZ).isoformat(timespec="seconds")
    meta["widgets_live"] = sorted(results)
    tw = load_widget("topic_totals") or {}
    mt = next((f["values"] for f in tw.get("filters", []) if f.get("dimensionName") == "LISTENING_MEDIA_TYPE"), None)
    meta["source_filter"] = ", ".join(v.title() for v in mt) if mt else "All sources"
    meta["placeholder"] = not all(sections.get(k) == "live" for k in
                                  ["cards", "sov", "trend", "sentiment", "markets", "languages", "media", "publications", "themes", "clippings", "authors"])
    meta["checks"] = checks
    return data


def run_checks(results, lids=("mkm", "kkm", "saj")):
    msgs, ok = [], True
    tt = results.get("topic_totals")
    if tt:
        for per, sides in tt.items():
            m = {lid: v.get("MENTIONS_COUNT", 0) for lid, v in sides["cur"].items()}
            if len(m) < len(lids):
                ok = False
                msgs.append(f"{per}: only {len(m)} of {len(lids)} leaders returned data - check {PROFILE['leaders']} topic IDs")
            if any(v <= 0 for v in m.values()):
                msgs.append(f"{per}: a leader has zero mentions")
        ts = results.get("topic_share")
        if ts and "30D" in ts and "30D" in tt:
            for lid, v in ts["30D"]["cur"].items():
                a, b = v.get("MENTIONS_COUNT", 0), tt["30D"]["cur"].get(lid, {}).get("MENTIONS_COUNT", 0)
                if b and abs(a - b) / b > 0.02:
                    msgs.append(f"30D {lid}: share-of-voice widget {a:.0f} vs totals widget {b:.0f} mentions (over 2% apart)")
        msgs.append("30D mentions: " + ", ".join(f"{lid} {v.get('MENTIONS_COUNT', 0):,.0f}" for lid, v in tt.get("30D", {}).get("cur", {}).items()))
    st = results.get("sentiment_topic")
    if st and "30D" in st:
        parts = []
        for lid, labs in st["30D"]["cur"].items():
            bkt = {"pos": 0, "neu": 0, "neg": 0}
            for lab, v in labs.items():
                bkt[sentiment_bucket(lab)] += v["MENTIONS_COUNT"]
            tot = sum(bkt.values())
            parts.append(f"{lid} {bkt['neg'] / tot * 100:.1f}%" if tot else f"{lid} n/a")
        msgs.append("30D negative sentiment: " + ", ".join(parts))
    dm = results.get("daily_mentions")
    if dm and tt and "30D" in tt:
        wins, today = windows()
        start = dt.datetime.fromisoformat(CFG["history_start"]).replace(tzinfo=TZ)
        i0 = (wins["30D"]["cur"][0] - start).days
        for lid, s in dm["series"].items():
            daily = sum(s["mentions"][i0:])
            tot = tt["30D"]["cur"].get(lid, {}).get("MENTIONS_COUNT", 0)
            flag = "" if tot and abs(daily - tot) / tot <= 0.02 else "  <- differs"
            msgs.append(f"30D {lid}: daily widget sum {daily:,} vs totals {tot:,.0f}{flag}")
    for lid in lids:
        au = (results.get(f"authors_{lid}") or {}).get("30D")
        if au is not None:
            ids = sum(1 for a in au if " " not in a["name"] and len(a["name"]) >= 20)
            msgs.append(f"30D {lid}: {len(au)} top authors" + ("  <- names look like profile IDs, check FROM_SN_USER" if au and ids > len(au) / 2 else ""))
    return ok, msgs


# ---------------------------------------------------------------- commands
def cmd_token(_):
    access_token(force=True)
    t = json.loads(TOKENS.read_text(encoding="utf-8"))
    print(f'Access token valid until {dt.datetime.fromtimestamp(t["expires_at"], TZ):%d %B %Y %H:%M} ({CFG["timezone"]}).')


def cmd_discover(_):
    wins, _ = windows()
    s, e = wins["30D"]["cur"]
    seen = {}
    for role, w in REG.items():
        payload = load_widget(role)
        if not payload:
            continue
        heads, rows = run_payload(with_window(payload, s, e), f"discover_{role}")
        print(f"\n== {role}: {len(rows)} rows, headings {heads}")
        for r in rows[:8]:
            print("  ", json.dumps(r, ensure_ascii=False)[:300])
        th = heading_for(payload, "dim", "TOPIC_IDS")
        for r in rows:
            tid, name = topic_value(r, th)
            seen.setdefault(tid, name)
    L = json.loads((ROOT / "leaders.json").read_text(encoding="utf-8"))
    print("\nTopics returned under the widget filters (30 days):")
    for tid, name in seen.items():
        guess = next((lid for lid, v in L.items() if not lid.startswith("_") and name and any(k in name.lower() for k in v["match"])), "")
        print(f"  {tid}  {name or '(name not resolved - check raw/discover_*.json)'}  {('-> ' + guess) if guess else ''}")
    print("\nPut each leader's topic ID(s) into leaders.json, then run: python sprinklr_refresh.py build --dry-run")


def run_all():
    results = {}
    for role in REG:
        if role in SKIP:
            continue
        payload = load_widget(role)
        h = handler_for(role)
        if not payload or not h:
            print(f"- {role}: {'no payload yet' if not payload else 'no handler'} (section stays sample)")
            continue
        print(f"- {role}: fetching")
        results[role] = h(payload)
    return results


def build_culture(data, msgs):
    """Second, separate run for the Culture comparison. It never blocks the business data: any failure keeps
    the previous culture data and is reported in the checks."""
    if not (ROOT / PROFILES["culture"][0]).exists():
        return
    print("\n== Culture comparison")
    try:
        use_profile("culture")
        lids = [k for k in leaders_file() if not k.startswith("_")]
        resolve_names()
        rc = run_all()
        okc, mc = run_checks(rc, lids=lids)
        msgs.extend("Culture: " + m for m in mc)
        if not okc:
            msgs.append("Culture: checks failed, previous culture data kept")
            return
        prev = data.get("culture") or {}
        sub = {"leaders": [{"id": l} for l in lids], "series": prev.get("series", {}), "meta": {}}
        sub = merge(sub, rc, mc)
        data["culture"] = {"leaders": lids, "totals": sub.get("totals"), "totals_windows": sub.get("totals_windows"),
                           "series": sub.get("series"), "breakdowns": sub.get("breakdowns", {}),
                           "sections": sub["meta"]["sections"], "generated": sub["meta"]["generated"], "start": sub["meta"].get("start")}
        data.setdefault("payloads", {}).update({"culture__" + r: v for r, v in sub.get("payloads", {}).items()})
    except (Exception, SystemExit) as e:
        msgs.append(f"Culture: skipped this run ({str(e)[:200]}); previous culture data kept")
    finally:
        use_profile("business")


def cmd_build(a):
    if not leader_map():
        sys.exit("leaders.json has no topic IDs yet. Run: python sprinklr_refresh.py discover")
    results = run_all()
    ok, msgs = run_checks(results)
    print("\nChecks:\n  " + "\n  ".join(msgs or ["no checks ran"]))
    if not ok:
        sys.exit("Checks failed: data.json not written.")
    path = (ROOT / CFG["data_json"]).resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    data = merge(data, results, msgs)
    build_culture(data, msgs)
    live = [k for k, v in data["meta"]["sections"].items() if v == "live"]
    print(f"\nLive sections: {', '.join(live) or 'none'}")
    if a.dry_run:
        print("Dry run: data.json not written.")
        return
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {path}")


def main():
    global ACCESS_OVERRIDE
    load_env()
    ap = argparse.ArgumentParser(description="Leadership Media Monitor - Sprinklr refresh")
    ap.add_argument("command", choices=["token", "discover", "build"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--access-token", help="use this access token and do not refresh")
    a = ap.parse_args()
    ACCESS_OVERRIDE = a.access_token
    {"token": cmd_token, "discover": cmd_discover, "build": cmd_build}[a.command](a)


if __name__ == "__main__":
    main()
