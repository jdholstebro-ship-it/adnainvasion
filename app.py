"""
Contact Enrichment Pipeline  (enrich_contacts.py)  — v2
=======================================================
Turns a prospect CSV/Excel exported from the Hearing Aid Clinic Prospect
Finder app into a flat CONTACT LIST: one row per named person, grouped
under the organization:

    Company Name | DBA | Contact Name | Contact Title |
    Contact Email | Contact Phone | URL | Match_Flag

v2 changes (from the 10-org test findings)
------------------------------------------
1. TRUST GATE (fixes wrong-company domains like OwnIt->hearohio):
   A resolved domain is TRUSTED only if EITHER
     (a) Google's phone matches the NPPES phone, OR
     (b) the registrable domain strongly resembles the org/DBA name.
   If neither, the domain is NOT trusted: no Hunter, no name-scrape, the
   URL is withheld and the org is emitted names-only, flagged for review.
2. REGISTRABLE DOMAIN (fixes hearohio.com/location/columbus/ returning no
   Hunter emails): every Hunter call and the output URL use the root
   registrable domain, never a deep storefront path or tracking params.
3. NATIONAL-BRAND FLAG: if the trusted domain is a known national brand
   whose name doesn't match the org (e.g. AudioNova, Miracle-Ear), the
   contacts are kept but flagged "National brand - HQ contacts" so you can
   judge per-lead.
4. ABOUT/TEAM NAME-SCRAPE: when contact slots are still unfilled on a
   TRUSTED domain, scrape about/team/staff pages for person names (anchored
   to clinical credentials to stay precise) and run them through Hunter
   Email Finder to gap-fill. Junk email-localpart name guessing removed.

Contact cap:  <=3 group locations -> 3 ;  >=4 -> 5.
Ranking:      NPPES officials first, then Domain-Search people by
              confidence, then credential-scraped names. Generic role
              email only if no named contact carries an email.

Setup / usage identical to v1. Cache file: enrich_contacts_cache.json
(keyed by org; re-runs resume and never re-spend quota).
"""

import argparse
import html as html_lib
import json
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests

CONFIG_FILE = Path(__file__).parent / "enrich_config.json"
CACHE_FILE = Path(__file__).parent / "enrich_contacts_cache.json"

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
HUNTER_DOMAIN_URL = "https://api.hunter.io/v2/domain-search"
HUNTER_FINDER_URL = "https://api.hunter.io/v2/email-finder"
HUNTER_VERIFY_URL = "https://api.hunter.io/v2/email-verifier"

# Bump this whenever the cached entry shape or resolve/trust logic changes.
# On load, any cache with a different version is discarded so stale entries
# from an older script version can never silently poison the output.
CACHE_SCHEMA_VERSION = 3

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
JUNK_EMAIL_RE = re.compile(
    r"(\.png|\.jpg|\.jpeg|\.gif|\.webp|\.svg)$|"
    r"(sentry|wixpress|example\.|domain\.com|yourdomain|email\.com|"
    r"godaddy|squarespace|typefoundry|@2x|noreply|no-reply)",
    re.IGNORECASE,
)
GENERIC_LOCALPART_RE = re.compile(
    r"^(info|office|contact|admin|hello|help|support|sales|reception|"
    r"appointments|frontdesk|front\.desk|team|mail|enquiries|inquiries|"
    r"booking|bookings|scheduling|billing|care|clinic)@",
    re.IGNORECASE,
)
# Pages to scrape, in priority order (team/about pages first for names).
NAME_PATHS = ["/about", "/about-us", "/team", "/our-team", "/staff", "/meet-the-team",
              "/providers", "/our-providers", "/doctors", ""]
EMAIL_PATHS = ["", "/contact", "/contact-us", "/about", "/about-us"]

# Known national/franchise hearing brands, matched against the DOMAIN.
# A location can legitimately belong to these, but the contacts Hunter
# returns are corporate HQ, so we flag rather than treat as the clinic.
NATIONAL_BRAND_DOMAINS = {
    "miracle-ear", "miracleear", "beltone", "audibel", "amplifon", "audionova",
    "hearinglife", "connecthearing", "costco", "hearusa", "nuear", "starkey",
    "yourhearinglink", "livelyhearing", "eargo", "soundhearing",
}

# Tokens stripped when comparing an org name to a domain (generic to the trade).
_FILLER_TOKENS = {
    "HEARING", "AUDIOLOGY", "AUDIOLOGISTS", "AUDIOLOGIST", "CENTER", "CENTERS",
    "CENTRE", "CLINIC", "CLINICS", "CARE", "AID", "AIDS", "INSTRUMENT",
    "INSTRUMENTS", "SERVICES", "SERVICE", "HEALTH", "SOLUTIONS", "ASSOCIATES",
    "GROUP", "THE", "AND", "OF",
}
# US state names/abbrevs — geographic tokens must NOT count as name resemblance
# (prevents 'OHIO' in 'OwnIt Audiology Ohio' matching hearOHIO.com).
_GEO_TOKENS = {
    "ALABAMA", "ALASKA", "ARIZONA", "ARKANSAS", "CALIFORNIA", "COLORADO",
    "CONNECTICUT", "DELAWARE", "FLORIDA", "GEORGIA", "HAWAII", "IDAHO",
    "ILLINOIS", "INDIANA", "IOWA", "KANSAS", "KENTUCKY", "LOUISIANA", "MAINE",
    "MARYLAND", "MASSACHUSETTS", "MICHIGAN", "MINNESOTA", "MISSISSIPPI",
    "MISSOURI", "MONTANA", "NEBRASKA", "NEVADA", "HAMPSHIRE", "JERSEY",
    "MEXICO", "YORK", "CAROLINA", "DAKOTA", "OHIO", "OKLAHOMA", "OREGON",
    "PENNSYLVANIA", "RHODE", "TENNESSEE", "TEXAS", "UTAH", "VERMONT",
    "VIRGINIA", "WASHINGTON", "WISCONSIN", "WYOMING", "NORTH", "SOUTH",
    "EAST", "WEST", "NEW", "NORTHERN", "SOUTHERN", "EASTERN", "WESTERN",
    "CENTRAL", "GREATER", "TRI", "VALLEY", "COAST", "COASTAL",
}
_LEGAL_SUFFIX = re.compile(
    r"\b(LLC|L\.L\.C\.|INC|INCORPORATED|PLLC|P\.?C\.?|LTD|LLP|CORP(ORATION)?|CO|PA|P\.A\.)\b\.?",
    re.IGNORECASE,
)
# Credential anchors used to spot real staff names on team/about pages.
CREDENTIAL_RE = re.compile(
    r"\b(Au\.?D\.?|Ph\.?D\.?|M\.?A\.?|M\.?S\.?|B\.?C\.?-?HIS|BC-HIS|HIS|"
    r"Audiologist|Doctor of Audiology|Hearing Instrument Specialist|CCC-A)\b",
    re.IGNORECASE,
)
COMPANY_WORD_RE = re.compile(
    r"\b(HEARING|AUDIOLOGY|AUDIO|CENTER|CENTRE|CLINIC|CARE|AID|AIDS|HEALTH|"
    r"AMERICA|SOLUTIONS|GROUP|ASSOCIATES|SERVICES|LLC|INC|PLLC|CORP|COMPANY)\b",
    re.IGNORECASE,
)

SLEEP = 0.3
MAX_SCRAPED_NAMES = 3   # cap Hunter Email-Finder calls from scraped names, per org
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ContactEnrichment/1.0"}

OUTPUT_COLUMNS = [
    "Company Name", "DBA", "Contact Name", "Contact Title",
    "Contact Email", "Contact Phone", "URL", "Match_Flag",
]


# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------

def digits(phone):
    d = re.sub(r"\D", "", str(phone or ""))
    return d[-10:] if len(d) >= 10 else d


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    return default


def load_cache(path):
    """
    Load the org cache, but only if its schema version matches this script.
    An older/incompatible cache is discarded (returns empty) so stale entries
    can never silently produce wrong output. The actual per-org entries live
    under the 'orgs' key; the version lives under '_schema'.
    """
    raw = load_json(path, {})
    if isinstance(raw, dict) and raw.get("_schema") == CACHE_SCHEMA_VERSION \
            and isinstance(raw.get("orgs"), dict):
        return raw["orgs"]
    if raw:
        print(f"(Ignoring incompatible cache at {path.name} — will rebuild; "
              "your API quota is re-spent only for orgs processed this run.)")
    return {}


def save_cache(path, cache):
    path.write_text(json.dumps({"_schema": CACHE_SCHEMA_VERSION, "orgs": cache}))


def blank(v):
    if v is None:
        return True
    s = str(v).strip()
    return s == "" or s.lower() == "nan"


def registrable_domain(url):
    """
    Reduce any URL or bare host to its registrable domain (label + TLD),
    stripping scheme, www, path, query, and multi-label subdomains.
    'https://www.hearohio.com/location/columbus/?x=1' -> 'hearohio.com'
    """
    if blank(url):
        return None
    s = str(url).strip()
    s = re.sub(r"^https?://", "", s, flags=re.IGNORECASE)
    s = s.split("/")[0].split("?")[0].split("#")[0]
    s = re.sub(r"^www\.", "", s, flags=re.IGNORECASE)
    s = s.lower().strip(".")
    if "." not in s:
        return None
    # keep last two labels for common TLDs; last three for co.uk-style
    parts = s.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov", "ac") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def domain_label(url):
    """The registrable domain without its TLD: 'hearohio.com' -> 'hearohio'."""
    d = registrable_domain(url)
    if not d:
        return None
    return d.split(".")[0]


def root_url(url):
    """Clean https root URL for output: 'https://hearohio.com'."""
    d = registrable_domain(url)
    return f"https://{d}" if d else None


def is_generic(email):
    return bool(GENERIC_LOCALPART_RE.match(str(email or "").strip()))


# Personal / free inbox providers — their domain is NOT the clinic's domain,
# so we never feed these to Hunter Domain Search as if they were the business.
PERSONAL_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
    "icloud.com", "me.com", "msn.com", "live.com", "comcast.net",
    "verizon.net", "att.net", "sbcglobal.net", "cox.net", "protonmail.com",
    "ymail.com", "mail.com", "gmx.com", "bellsouth.net",
}


def email_domain(email):
    """Registrable domain of an email address, or None."""
    e = str(email or "").strip().lower()
    if "@" not in e:
        return None
    return registrable_domain(e.split("@", 1)[1])


def is_business_email_domain(email):
    """True if the email is on a real (non-personal, non-blank) domain."""
    d = email_domain(email)
    return bool(d and d not in PERSONAL_EMAIL_DOMAINS)


def valid_email(email):
    e = str(email or "").strip()
    return bool(EMAIL_RE.fullmatch(e)) and not JUNK_EMAIL_RE.search(e.lower())


def resolve_redirect_domain(url):
    """
    Follow a website's redirects and return the FINAL registrable domain,
    so Hunter is queried against where the site actually lives (e.g. a
    clinic domain that 301s to its live host). Returns None on failure.
    Cheap: one GET with redirects, short timeout, body ignored.
    """
    if blank(url):
        return None
    start = str(url).strip()
    if not start.lower().startswith("http"):
        start = "https://" + start
    try:
        resp = requests.get(start, headers=UA, timeout=10,
                            allow_redirects=True, stream=True)
        final = resp.url or start
        resp.close()
        return registrable_domain(final)
    except requests.RequestException:
        return None


def title_case_name(name):
    return " ".join(w.capitalize() for w in str(name or "").split())


def norm_person(name):
    n = re.sub(r"[^A-Za-z ]", " ", str(name or "")).upper()
    return re.sub(r"\s+", " ", n).strip()


def _significant_tokens(name):
    n = _LEGAL_SUFFIX.sub("", str(name or "").upper())
    n = re.sub(r"[^A-Z0-9 ]", " ", n)
    toks = [t for t in n.split() if t and t not in _FILLER_TOKENS and len(t) > 1]
    return toks


def domain_resembles_name(url, *names):
    """
    True if the registrable domain is clearly built from the business name.
    Compares the domain label against significant (non-filler) name tokens
    and their initialism. Designed to accept wchearingclinic for 'Western
    Colorado Hearing Clinic' while rejecting hearohio for 'OwnIt Audiology'.
    """
    label = domain_label(url)
    if not label:
        return False
    label_alnum = re.sub(r"[^a-z0-9]", "", label.lower())
    if not label_alnum:
        return False
    for name in names:
        full_words = [w for w in _LEGAL_SUFFIX.sub("", str(name or "").upper()).split()
                      if re.search(r"[A-Z]", w)]
        full_words = [re.sub(r"[^A-Z]", "", w) for w in full_words]
        full_words = [w for w in full_words if w and w not in ("THE", "AND", "OF")]
        if not full_words:
            continue
        toks = _significant_tokens(name)
        distinctive = [t for t in toks if t not in _GEO_TOKENS]

        # (a) any distinctive, non-geographic token (>=4 chars) in the label
        if any(len(t) >= 4 and t.lower() in label_alnum for t in distinctive):
            return True

        # (b) full compaction of ALL name words contained in the label
        #     (or the label contained in it): catches 'myhearingcenters',
        #     'wchearingclinic', 'ehchearing' via the initials+words forms below.
        full_compact = "".join(full_words).lower()
        if len(label_alnum) >= 5 and (label_alnum in full_compact or full_compact in label_alnum):
            return True

        # (c) leading-geo initials + spelled following words:
        #     'Western Colorado Hearing Clinic' -> 'wc' + 'hearingclinic'.
        #     When the name is all geo+filler, the 'spelled' part falls back to
        #     the filler trade words (hearing/clinic) that the domain spells out.
        geo_words = [w for w in full_words if w in _GEO_TOKENS]
        nongeo = [w for w in full_words if w not in _GEO_TOKENS]
        geo_lead = "".join(w[0] for w in geo_words).lower()
        spelled = "".join(nongeo).lower()
        if geo_lead and spelled and len(geo_lead) >= 2 \
                and label_alnum.startswith(geo_lead) and spelled[:6] in label_alnum:
            return True

        # (d) initialism of all words as a prefix (>=3 letters): 'ehc...'
        initials = "".join(w[0] for w in full_words).lower()
        if len(initials) >= 3 and label_alnum.startswith(initials):
            return True

        # (e) short distinctive token (2-3 chars like 'MY') that begins the label,
        #     provided the following label text spells a trade word
        for t in distinctive:
            tl = t.lower()
            if 2 <= len(tl) <= 3 and label_alnum.startswith(tl):
                return True
    return False


def is_national_brand(url):
    label = domain_label(url)
    if not label:
        return False
    lab = re.sub(r"[^a-z0-9]", "", label.lower())
    return any(b in lab for b in NATIONAL_BRAND_DOMAINS)


# ----------------------------------------------------------------
# Step 1 — Google Places
# ----------------------------------------------------------------

def _places_post(body, field_mask, api_key):
    headers = {"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": field_mask,
               "Content-Type": "application/json"}
    resp = None
    for attempt in range(4):
        try:
            resp = requests.post(PLACES_URL, headers=headers, json=body, timeout=20)
            break
        except requests.RequestException:
            if attempt == 3:
                return None
            time.sleep(3 * (attempt + 1))
    if resp.status_code in (401, 403):
        raise RuntimeError(f"Google Places rejected the API key ({resp.status_code}). "
                           "Check enrich_config.json and that 'Places API (New)' is enabled.")
    if resp.status_code == 429:
        raise RuntimeError("Google Places quota exceeded (429).")
    if resp.status_code == 400:
        return None
    try:
        resp.raise_for_status()
    except requests.RequestException:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def places_search_one(query, api_key):
    field_mask = ("places.displayName,places.formattedAddress,"
                  "places.nationalPhoneNumber,places.websiteUri,"
                  "places.businessStatus")
    data = _places_post({"textQuery": query}, field_mask, api_key)
    places = (data or {}).get("places") or []
    if not places:
        return None
    p = places[0]
    return {
        "google_name": (p.get("displayName") or {}).get("text"),
        "google_address": p.get("formattedAddress"),
        "google_phone": p.get("nationalPhoneNumber"),
        "website": p.get("websiteUri"),
        "business_status": p.get("businessStatus"),
    }


def resolve_place(dba, legal_name, city, state_code, nppes_phone, api_key):
    """
    DBA-first resolution, then decide trust.
    Returns dict: place, website(root), domain, trusted(bool), flag(str).

    Trust gate (policy 'b'):
      trusted if phone matches OR domain resembles the org/DBA name.
    Flags:
      "Verified (phone)"            phone matched
      "Verified (domain match)"    domain resembles name, phone didn't confirm
      "National brand - HQ contacts"  trusted but domain is a known franchise
      "Unverified - review"        not trusted; URL withheld, names only
      "Not found"                  Places returned nothing
    """
    np_ = digits(nppes_phone)

    def run(name):
        if blank(name):
            return None
        q = ", ".join(x for x in [str(name).strip(), city, state_code] if not blank(x))
        p = places_search_one(q, api_key)
        time.sleep(SLEEP)
        return {"place": p, "name": name}

    attempts = []
    if not blank(dba):
        attempts.append(run(dba))
    # retry legal name if no DBA, or DBA didn't phone-verify
    first = attempts[0] if attempts else None
    dba_verified = bool(first and first["place"]
                        and np_ and digits(first["place"].get("google_phone")) == np_)
    if blank(dba) or not dba_verified:
        attempts.append(run(legal_name))
    attempts = [a for a in attempts if a and a["place"]]

    # 1) phone-verified hit wins
    for a in attempts:
        p = a["place"]
        if np_ and digits(p.get("google_phone")) == np_:
            website = root_url(p.get("website"))
            flag = "National brand - HQ contacts" if is_national_brand(p.get("website")) else "Verified (phone)"
            return {"place": p, "website": website, "domain": registrable_domain(p.get("website")),
                    "trusted": bool(website), "flag": flag}

    # 2) domain-resemblance trust (no phone confirmation)
    for a in attempts:
        p = a["place"]
        if p.get("website") and domain_resembles_name(p.get("website"), dba, legal_name):
            website = root_url(p.get("website"))
            flag = "National brand - HQ contacts" if is_national_brand(p.get("website")) else "Verified (domain match)"
            return {"place": p, "website": website, "domain": registrable_domain(p.get("website")),
                    "trusted": True, "flag": flag}

    # 3) national brand even without resemblance (location belongs to franchise)
    for a in attempts:
        p = a["place"]
        if is_national_brand(p.get("website")):
            website = root_url(p.get("website"))
            return {"place": p, "website": website, "domain": registrable_domain(p.get("website")),
                    "trusted": True, "flag": "National brand - HQ contacts"}

    # 4) not trusted — withhold URL, names only
    if attempts:
        return {"place": attempts[0]["place"], "website": None, "domain": None,
                "trusted": False, "flag": "Unverified - review"}
    return {"place": None, "website": None, "domain": None,
            "trusted": False, "flag": "Not found"}


# ----------------------------------------------------------------
# Step 2 — Website scraping (emails + credential-anchored names)
# ----------------------------------------------------------------

def _decode_cfemail(hexstr):
    try:
        raw = bytes.fromhex(hexstr)
        key = raw[0]
        return "".join(chr(b ^ key) for b in raw[1:])
    except (ValueError, IndexError):
        return ""


def _fetch(url):
    try:
        resp = requests.get(url, headers=UA, timeout=10, allow_redirects=True)
        if resp.status_code != 200 or "text/html" not in resp.headers.get("Content-Type", "text/html"):
            return None
        return html_lib.unescape(resp.text)
    except requests.RequestException:
        return None


def scrape_site_emails(root):
    if blank(root):
        return []
    base = root.rstrip("/")
    found = []
    for path in EMAIL_PATHS:
        text = _fetch(base + path)
        if not text:
            continue
        for hexstr in re.findall(r'data-cfemail="([0-9a-fA-F]+)"', text):
            text += " " + _decode_cfemail(hexstr)
        for email in EMAIL_RE.findall(text):
            email = email.strip().lower()
            if not JUNK_EMAIL_RE.search(email) and email not in found:
                found.append(email)
        time.sleep(SLEEP)
    return found[:10]


_NAME_NEAR_CRED_RE = re.compile(
    r"([A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+(?:-[A-Z][a-z]+)?)"  # First [M.] Last
    r"\s*,?\s*(?:Au\.?D|Ph\.?D|M\.?A|M\.?S|B\.?C\.?-?HIS|HIS|Audiologist|CCC-A)",
)


def scrape_person_names(root, company_name):
    """
    Scrape about/team/staff pages for real staff names, anchored to clinical
    credentials for precision. Rejects company-word false positives.
    Returns list of 'First Last' strings.
    """
    if blank(root):
        return []
    base = root.rstrip("/")
    names, seen = [], set()
    for path in NAME_PATHS:
        text = _fetch(base + path)
        if not text:
            continue
        # strip tags to plain text for name matching
        plain = re.sub(r"<[^>]+>", " ", text)
        plain = re.sub(r"\s+", " ", plain)
        for m in _NAME_NEAR_CRED_RE.finditer(plain):
            cand = m.group(1).strip()
            if COMPANY_WORD_RE.search(cand):
                continue
            if norm_person(cand) == norm_person(company_name):
                continue
            toks = cand.split()
            if not (2 <= len(toks) <= 3):
                continue
            key = norm_person(cand)
            if key and key not in seen:
                seen.add(key)
                names.append(title_case_name(cand))
        time.sleep(SLEEP)
        if len(names) >= MAX_SCRAPED_NAMES * 2:
            break
    return names


# ----------------------------------------------------------------
# Step 3/4/5 — Hunter
# ----------------------------------------------------------------

def hunter_domain_search(domain, api_key, limit=10):
    resp = None
    for attempt in range(4):
        try:
            resp = requests.get(HUNTER_DOMAIN_URL, params={
                "domain": domain, "api_key": api_key, "limit": limit}, timeout=20)
            break
        except requests.RequestException:
            if attempt == 3:
                return []
            time.sleep(3 * (attempt + 1))
    if resp.status_code == 401:
        raise RuntimeError("Hunter.io rejected the API key (401).")
    if resp.status_code == 429:
        raise RuntimeError("Hunter.io rate limit reached (429).")
    if resp.status_code != 200:
        return []
    try:
        emails = (resp.json().get("data") or {}).get("emails") or []
    except ValueError:
        return []
    out = []
    for e in emails:
        out.append({"email": e.get("value"), "first": e.get("first_name"),
                    "last": e.get("last_name"), "title": e.get("position"),
                    "phone": e.get("phone_number"), "score": e.get("confidence"),
                    "generic": (e.get("type") == "generic")})
    return out


def hunter_email_finder(domain, first_name, last_name, api_key):
    resp = None
    for attempt in range(4):
        try:
            resp = requests.get(HUNTER_FINDER_URL, params={
                "domain": domain, "first_name": first_name,
                "last_name": last_name, "api_key": api_key}, timeout=20)
            break
        except requests.RequestException:
            if attempt == 3:
                return None
            time.sleep(3 * (attempt + 1))
    if resp.status_code == 401:
        raise RuntimeError("Hunter.io rejected the API key (401).")
    if resp.status_code == 429:
        raise RuntimeError("Hunter.io rate limit reached (429).")
    if resp.status_code != 200:
        return None
    try:
        data = resp.json().get("data") or {}
    except ValueError:
        return None
    if not data.get("email"):
        return None
    return {"email": data["email"], "score": data.get("score")}


def hunter_verify(email, api_key):
    resp = None
    for attempt in range(4):
        try:
            resp = requests.get(HUNTER_VERIFY_URL, params={
                "email": email, "api_key": api_key}, timeout=20)
            break
        except requests.RequestException:
            if attempt == 3:
                return None
            time.sleep(3 * (attempt + 1))
    if resp.status_code in (401, 429) or resp.status_code != 200:
        return None
    try:
        data = resp.json().get("data") or {}
    except ValueError:
        return None
    return {"status": data.get("status"), "score": data.get("score")}


# ----------------------------------------------------------------
# Org grouping
# ----------------------------------------------------------------

def group_key(row):
    for col in ("DBA_Name", "Chain_Group", "Organization_Name", "Name"):
        v = row.get(col)
        if not blank(v):
            return str(v).strip()
    return None


def build_orgs(df):
    orgs = {}
    for _, row in df.iterrows():
        key = group_key(row)
        if key is None:
            continue
        o = orgs.setdefault(key, {
            "key": key, "company_name": None, "dba": None,
            "city": None, "state": None, "phone": None,
            "group_locations": 0, "officials": [], "_seen": set(),
            "existing_emails": [], "_email_seen": set(),
        })
        if o["company_name"] is None and not blank(row.get("Organization_Name")):
            o["company_name"] = str(row["Organization_Name"]).strip()
        if o["dba"] is None and not blank(row.get("DBA_Name")):
            o["dba"] = str(row["DBA_Name"]).strip()
        if o["city"] is None and not blank(row.get("City")):
            o["city"] = str(row["City"]).strip()
        if o["state"] is None and not blank(row.get("State")):
            o["state"] = str(row["State"]).strip()
        if o["phone"] is None and not blank(row.get("Phone")):
            o["phone"] = str(row["Phone"]).strip()
        gl = row.get("Group_Locations")
        try:
            gl = int(gl)
        except (TypeError, ValueError):
            gl = 0
        o["group_locations"] = max(o["group_locations"], gl)
        off = row.get("Authorized_Official")
        if not blank(off):
            k = norm_person(off)
            if k and k not in o["_seen"]:
                o["_seen"].add(k)
                o["officials"].append({
                    "name": title_case_name(off),
                    "title": (None if blank(row.get("Authorized_Official_Title"))
                              else str(row["Authorized_Official_Title"]).strip()),
                    "phone": (None if blank(row.get("Authorized_Official_Phone"))
                              else str(row["Authorized_Official_Phone"]).strip()),
                })
        # Pre-existing emails from the source data (Direct_Address). A cell may
        # hold several separated by ; or whitespace. Keep valid ones, de-duped.
        direct = row.get("Direct_Address")
        if not blank(direct):
            for tok in re.split(r"[;,\s]+", str(direct).strip()):
                em = tok.strip().lower()
                if em and valid_email(em) and em not in o["_email_seen"]:
                    o["_email_seen"].add(em)
                    o["existing_emails"].append(em)
    for o in orgs.values():
        o.pop("_seen", None)
        o.pop("_email_seen", None)
        if o["company_name"] is None:
            o["company_name"] = o["key"]
    return orgs


def cap_for(group_locations):
    return 5 if (group_locations or 0) >= 4 else 3


def _valid_person(first, last):
    """Reject Hunter roster rows that aren't real people (company tokens etc)."""
    full = f"{first or ''} {last or ''}".strip()
    if not first or not last:
        return False
    if COMPANY_WORD_RE.search(full):
        return False
    if not re.match(r"^[A-Za-z][A-Za-z.'-]+ [A-Za-z][A-Za-z.'-]+$", full):
        return False
    return True


# ----------------------------------------------------------------
# Enrich one organization
# ----------------------------------------------------------------

def enrich_org(o, cfg, cache, use_hunter, rescrape=False):
    key = o["key"]
    entry = cache.get(key, {})
    if rescrape:
        for k in ("scrape_emails", "scrape_names"):
            entry.pop(k, None)

    hunter_key = cfg.get("HUNTER_API_KEY") if use_hunter else None

    # --- Step 1: Places + trust gate (cached) ---
    if "resolve" not in entry:
        entry["resolve"] = resolve_place(
            o["dba"], o["company_name"], o["city"], o["state"], o["phone"],
            cfg["GOOGLE_API_KEY"])
    resolve = entry["resolve"]
    place = resolve.get("place") or {}
    website = resolve.get("website")          # clean root URL or None
    domain = resolve.get("domain")            # registrable domain (Places) or None
    trusted = resolve.get("trusted")
    flag = resolve.get("flag")
    status = place.get("business_status")
    closed = (status == "CLOSED_PERMANENTLY")

    # --- Existing emails from source data ---
    existing = list(o.get("existing_emails") or [])
    existing_named = [e for e in existing if not is_generic(e)]        # real people/business
    existing_generic = [e for e in existing if is_generic(e)]          # info@, office@ ...
    # A pre-existing BUSINESS (non-personal, non-generic) email is strong,
    # independent evidence of the true domain — it overrides the Places trust
    # gate and provides a Hunter domain even when Places failed to verify.
    biz_email_domain = None
    for e in existing:
        if is_business_email_domain(e):     # covers generic-but-business too, e.g. info@clinic.com
            biz_email_domain = email_domain(e)
            break

    # --- Choose the Hunter/scrape domain, following redirects to where the
    #     site actually lives. Priority: existing business-email domain, then
    #     the Places domain (redirect-resolved). ---
    hunter_domain = entry.get("hunter_domain", "___unset___")
    if hunter_domain == "___unset___":
        chosen = None
        if biz_email_domain:
            chosen = biz_email_domain
        elif domain:
            # follow the resolved site's redirects to its real destination
            redirected = resolve_redirect_domain(website)
            chosen = redirected or domain
        entry["hunter_domain"] = chosen
        hunter_domain = chosen

    # can_enrich: Places-trusted OR we have a business email domain to work from,
    # as long as the business isn't permanently closed and we have a domain.
    can_enrich = bool(hunter_domain and not closed
                      and (trusted or biz_email_domain))
    # If Places wasn't trusted but an existing business email rescued the org,
    # surface a clear flag and a usable URL for the output.
    if not trusted and biz_email_domain:
        flag = "Verified (existing email domain)"
        if not website:
            website = f"https://{biz_email_domain}"

    # For scraping/Hunter we now use hunter_domain (not the raw Places domain).
    domain = hunter_domain

    # --- Step 2a: website emails (only when enrichable) ---
    scrape_emails = entry.get("scrape_emails")
    if scrape_emails is None:
        scrape_site = website or (f"https://{domain}" if domain else None)
        scrape_emails = scrape_site_emails(scrape_site) if can_enrich else []
        entry["scrape_emails"] = scrape_emails

    # --- Step 3: Hunter Domain Search (enrichable domains only) ---
    roster = entry.get("roster")
    if roster is None:
        roster = hunter_domain_search(domain, hunter_key) if (can_enrich and hunter_key and domain) else []
        entry["roster"] = roster
        if can_enrich and hunter_key and domain:
            time.sleep(SLEEP)

    # ---------- Assemble ----------
    contacts, seen_people, seen_emails = [], set(), set()
    roster_by_person = {}
    for r in roster:
        pk = norm_person(f"{r.get('first','')} {r.get('last','')}")
        if pk:
            roster_by_person[pk] = r

    # Priority 0: pre-existing NON-GENERIC emails from the source data.
    # Highest trust — they came from the data, not a lookup. For each, try to
    # attach a name/title, in order: (a) an NPPES official whose name matches
    # the email's local part, (b) a Hunter-roster person with that exact email.
    # Otherwise keep as an emailed-but-nameless contact (still a real contact,
    # so it suppresses the generic fallback and fills a slot).
    roster_by_email = {}
    for r in roster:
        rem = (r.get("email") or "").lower()
        if rem:
            roster_by_email[rem] = r

    def _localpart_matches_name(email, name):
        """True if the email local part looks built from the person's name,
        e.g. 'jennifer.bebee'/'jbebee'/'bebeej' <-> 'Jennifer Bebee'."""
        lp = str(email or "").split("@", 1)[0].lower()
        lp_alpha = re.sub(r"[^a-z]", "", lp)
        parts = [p for p in re.split(r"[^A-Za-z]", str(name or "")) if p]
        if len(parts) < 2 or not lp_alpha:
            return False
        first, last = parts[0].lower(), parts[-1].lower()
        if len(last) >= 3 and last in lp_alpha and (first in lp_alpha or first[0] in lp):
            return True
        # first.last / f.last / firstlast forms
        for form in (f"{first}{last}", f"{first[0]}{last}", f"{last}{first[0]}",
                     f"{first}.{last}", f"{last}.{first}"):
            if re.sub(r"[^a-z]", "", form) == lp_alpha:
                return True
        return False

    # Pre-mark which officials get claimed by an existing email, so Priority 1
    # attaches the email to them instead of re-searching.
    official_email = {}   # norm_person(name) -> email
    for em in existing_named:
        for off in o["officials"]:
            if norm_person(off["name"]) not in official_email and _localpart_matches_name(em, off["name"]):
                official_email[norm_person(off["name"])] = em
                break

    claimed_emails = set(official_email.values())
    for em in existing_named:
        if em in seen_emails or em in claimed_emails:
            continue
        seen_emails.add(em)
        r = roster_by_email.get(em)
        if r and (r.get("first") or r.get("last")):
            nm = title_case_name(f"{r.get('first','')} {r.get('last','')}".strip())
            seen_people.add(norm_person(nm))
            contacts.append({"name": nm, "title": r.get("title"), "email": em,
                             "phone": r.get("phone"), "score": 1000, "source": "existing"})
        else:
            contacts.append({"name": None, "title": None, "email": em,
                             "phone": None, "score": 1000, "source": "existing"})

    # Priority 1: NPPES officials (always kept; email may stay blank).
    # If an existing email was matched to this official, use it (no Finder call).
    for off in o["officials"]:
        pk = norm_person(off["name"])
        if pk in seen_people:
            continue
        seen_people.add(pk)
        email, score, phone, title = None, None, off["phone"], off["title"]
        if pk in official_email:                       # matched a pre-existing email
            email, score = official_email[pk], 1000
            seen_emails.add(email.lower())
        elif pk in roster_by_person and roster_by_person[pk].get("email"):
            r = roster_by_person[pk]
            if (r.get("email") or "").lower() not in seen_emails:
                email, score = r["email"], r.get("score")
                title = title or r.get("title")
                phone = phone or r.get("phone")
        elif can_enrich and hunter_key and domain:
            parts = off["name"].split()
            if len(parts) >= 2:
                fk = f"finder::{pk}"
                if fk not in entry:
                    entry[fk] = hunter_email_finder(domain, parts[0], parts[-1], hunter_key) or {}
                    time.sleep(SLEEP)
                cand = (entry[fk].get("email") or "").lower()
                if cand and cand not in seen_emails:
                    email, score = entry[fk]["email"], entry[fk].get("score")
        if email:
            seen_emails.add(email.lower())
        contacts.append({"name": off["name"], "title": title,
                         "email": email,
                         "phone": phone, "score": score if score is not None else 999,
                         "source": "existing" if pk in official_email else "nppes"})

    # Priority 2: other named people from Domain Search
    extra = [r for r in roster if not r.get("generic") and _valid_person(r.get("first"), r.get("last"))]
    extra.sort(key=lambda r: (r.get("score") or 0), reverse=True)
    for r in extra:
        pk = norm_person(f"{r.get('first','')} {r.get('last','')}")
        if not pk or pk in seen_people:
            continue
        em = (r.get("email") or "").lower()
        if em and em in seen_emails:
            continue
        seen_people.add(pk)
        if em:
            seen_emails.add(em)
        contacts.append({"name": title_case_name(f"{r.get('first','')} {r.get('last','')}".strip()),
                         "title": r.get("title"), "email": r.get("email"),
                         "phone": r.get("phone"), "score": r.get("score") or 0,
                         "source": "hunter"})

    cap = cap_for(o["group_locations"])
    # Slots already filled = named contacts + pre-existing emailed contacts.
    def _fills_slot(c):
        return bool(c.get("name") or (c["source"] == "existing" and c.get("email")))
    slot_now = [c for c in contacts if _fills_slot(c)]

    # Priority 3: credential-scraped names -> Email Finder (only if slots remain)
    if can_enrich and hunter_key and domain and len(slot_now) < cap:
        scraped = entry.get("scrape_names")
        if scraped is None:
            scraped = scrape_person_names(website, o["company_name"])
            entry["scrape_names"] = scraped
        tried = 0
        for nm in scraped:
            if len(slot_now) >= cap or tried >= MAX_SCRAPED_NAMES:
                break
            pk = norm_person(nm)
            if pk in seen_people:
                continue
            parts = nm.split()
            if len(parts) < 2:
                continue
            tried += 1
            fk = f"finder::{pk}"
            if fk not in entry:
                entry[fk] = hunter_email_finder(domain, parts[0], parts[-1], hunter_key) or {}
                time.sleep(SLEEP)
            fr = entry[fk]
            if fr.get("email") and fr["email"].lower() not in seen_emails:
                seen_people.add(pk)
                seen_emails.add(fr["email"].lower())
                c = {"name": nm, "title": None, "email": fr["email"],
                     "phone": None, "score": fr.get("score") or 0, "source": "scrape"}
                contacts.append(c)
                slot_now.append(c)

    # ---------- Rank, cap, verify ----------
    def rank_key(c):
        order = {"existing": 0, "nppes": 1, "hunter": 2, "scrape": 3}[c["source"]]
        return (order, -(c["score"] if c["score"] is not None else 0))

    # Selectable = any named contact, OR a pre-existing non-generic email that
    # has no name (still a real, usable contact worth keeping).
    selectable = [c for c in contacts
                  if c.get("name") or (c["source"] == "existing" and c.get("email"))]
    selectable.sort(key=rank_key)
    selected = selectable[:cap]

    # Generic retained as the FLOOR: whenever no selected contact carries an
    # email, attach one generic (pre-existing info@ from data first, then
    # roster generic, then a scraped generic). Also, if we have room and the
    # data shipped a generic, keep it as a fallback line even alongside
    # emailless named officials.
    have_email = any(c.get("email") for c in selected)
    if not have_email or (existing_generic and len(selected) < cap
                          and not any(c["source"].endswith("generic") for c in selected)):
        generic = None
        if existing_generic:
            generic = {"name": None, "title": None, "email": existing_generic[0],
                       "phone": None, "source": "existing-generic"}
        if not generic:
            for r in roster:
                if r.get("generic") and r.get("email"):
                    generic = {"name": None, "title": None, "email": r["email"],
                               "phone": None, "source": "hunter-generic"}
                    break
        if not generic:
            for em in scrape_emails:
                if is_generic(em):
                    generic = {"name": None, "title": None, "email": em,
                               "phone": None, "source": "scrape-generic"}
                    break
        if generic and generic["email"].lower() not in {(_c.get("email") or "").lower() for _c in selected}:
            if not selected:
                selected = [generic]
            elif len(selected) < cap:
                selected.append(generic)

    # Verify (cached per email)
    if hunter_key:
        vcache = entry.setdefault("verify", {})
        for c in selected:
            em = c.get("email")
            if em and em.lower() not in vcache:
                vcache[em.lower()] = hunter_verify(em, hunter_key) or {}
                time.sleep(SLEEP)

    cache[key] = entry

    rows = []
    for c in selected:
        rows.append({
            "Company Name": o["company_name"], "DBA": o["dba"],
            "Contact Name": c.get("name"), "Contact Title": c.get("title"),
            "Contact Email": c.get("email"), "Contact Phone": c.get("phone"),
            "URL": website, "Match_Flag": flag,
        })
    if not rows:
        rows.append({"Company Name": o["company_name"], "DBA": o["dba"],
                     "Contact Name": None, "Contact Title": None,
                     "Contact Email": None, "Contact Phone": None,
                     "URL": website, "Match_Flag": flag})
    return rows, {"flag": flag, "status": status, "website": website,
                  "n_contacts": sum(1 for c in selected if c.get("email"))}


# ----------------------------------------------------------------
# Main
# ----------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Build a contact list from an exported prospect file")
    parser.add_argument("input", help="CSV or XLSX exported from the app")
    parser.add_argument("--limit", type=int, default=None, help="Only process first N organizations (testing)")
    parser.add_argument("--no-hunter", action="store_true", help="Skip all Hunter calls (Places + scrape only)")
    parser.add_argument("--rescrape", action="store_true", help="Ignore cached website scrape and scrape again")
    args = parser.parse_args()

    cfg = load_json(CONFIG_FILE, {})
    if not cfg.get("GOOGLE_API_KEY"):
        sys.exit("Missing GOOGLE_API_KEY — create enrich_config.json (see header).")

    in_path = Path(args.input)
    if not in_path.exists():
        sys.exit(f"File not found: {in_path}")
    df = pd.read_excel(in_path) if in_path.suffix.lower() in (".xlsx", ".xls") \
        else pd.read_csv(in_path, dtype=str)
    if "NPI" not in df.columns:
        sys.exit("Input file has no NPI column — export it from the app first.")

    cache = load_cache(CACHE_FILE)
    use_hunter = not args.no_hunter
    orgs = build_orgs(df)
    keys = list(orgs.keys())
    total = len(keys) if not args.limit else min(args.limit, len(keys))
    print(f"{len(df)} rows -> {len(orgs)} organizations"
          + (f" (limited to {args.limit})" if args.limit else "")
          + (" | Hunter OFF" if not use_hunter else ""))

    all_rows, processed = [], 0
    for key in keys:
        if args.limit and processed >= args.limit:
            break
        processed += 1
        o = orgs[key]
        cap = cap_for(o["group_locations"])
        print(f"[{processed}/{total}] {o['company_name']}"
              + (f"  (DBA: {o['dba']})" if o["dba"] else "")
              + f"  [{o['group_locations']} loc, cap {cap}, {len(o['officials'])} official(s)]")
        try:
            rows, meta = enrich_org(o, cfg, cache, use_hunter, rescrape=args.rescrape)
        except RuntimeError as e:
            save_cache(CACHE_FILE, cache)
            sys.exit(f"\nSTOPPED: {e}\nProgress saved — rerun to resume.")
        all_rows.extend(rows)
        print(f"    {meta['flag']}"
              + (f" — {meta['website']}" if meta['website'] else "")
              + (f" — {meta['status']}" if meta['status'] else "")
              + f" | {meta['n_contacts']} email(s)")
        if processed % 10 == 0:
            save_cache(CACHE_FILE, cache)

    save_cache(CACHE_FILE, cache)
    out = pd.DataFrame(all_rows, columns=OUTPUT_COLUMNS)
    stem = in_path.stem + "_contacts"
    out.to_csv(in_path.parent / f"{stem}.csv", index=False)
    try:
        out.to_excel(in_path.parent / f"{stem}.xlsx", index=False, sheet_name="Contacts")
    except Exception as e:
        print(f"(Excel export skipped: {e} — CSV is complete)")

    n_orgs = out[["Company Name", "DBA"]].drop_duplicates().shape[0]
    print(f"\nDone. Wrote {stem}.csv / .xlsx")
    print(f"Summary: {n_orgs} orgs | {len(out)} rows | "
          f"{out['Contact Name'].notna().sum()} named | "
          f"{out['Contact Email'].notna().sum()} with email")
    print("Flags:", ", ".join(f"{k}={v}" for k, v in out["Match_Flag"].value_counts().items()))


if __name__ == "__main__":
    main()
