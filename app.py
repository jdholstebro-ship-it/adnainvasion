import hashlib
import io
import re
import time
from datetime import datetime

import pandas as pd
import requests
import streamlit as st

# ============================================================
# Hearing Aid Clinic Prospect Finder — NPPES NPI Registry
#
# v3 fixes:
#  - Taxonomy search now uses WILDCARDS ("Hearing*", "Audiolog*").
#    The API's exact-description matching is unreliable (e.g. the
#    registry's own description for 332S00000X contains a double
#    space: "Hearing  Aid Equipment"). Wildcards sidestep this;
#    precision comes from filtering taxonomy CODES client-side.
#  - Locations array is returned as "practiceLocations" (camelCase),
#    not "practice_locations" as documented. Both keys handled.
#  - Results persist via st.session_state; provider-type selection
#    is applied client-side, so changing it does NOT require re-search.
# ============================================================

st.set_page_config(page_title="Hearing Aid Clinic Prospect Finder", layout="wide")
st.title("🦻 Hearing Aid Clinic Prospect Finder")
st.markdown(
    "Find **hearing aid selling & fitting practices** in the NPPES NPI Registry, "
    "detect chains/common ownership, and surface recently opened, multi-location prospects."
)

API_URL = "https://npiregistry.cms.hhs.gov/api/"

# Wildcard queries sent to the API (broad, reliable)
SEARCH_WILDCARDS = ["Hearing*", "Audiolog*"]

# Precise filtering happens here, by NUCC taxonomy code
CODE_LABELS = {
    "237700000X": "Hearing Instrument Specialist",
    "237600000X": "Audiologist-Hearing Aid Fitter",
    "332S00000X": "Hearing Aid Equipment Supplier",
    "231H00000X": "Audiologist (many dispense aids)",
}
TARGET_CODES = set(CODE_LABELS.keys())
HEARING_AID_CODES = {"237700000X", "237600000X", "332S00000X"}

STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA",
    "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY",
    "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX",
    "UT", "VT", "VA", "WA", "WV", "WI", "WY", "PR",
]

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

SUFFIX_RE = re.compile(
    r"\b(LLC|L\.L\.C\.|INC|INCORPORATED|PLLC|P\.?C\.?|LTD|LLP|CORP(ORATION)?|CO|PA|P\.A\.)\b\.?",
    re.IGNORECASE,
)

def normalize_org_name(name):
    """Normalize so 'ABC Hearing, LLC' and 'ABC HEARING INC.' group together."""
    if not name or not isinstance(name, str):
        return None
    n = name.upper()
    n = SUFFIX_RE.sub("", n)
    n = re.sub(r"[^A-Z0-9 ]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n or None


def fetch_page(params, retries=3):
    """One API call with retry/backoff for throttling."""
    for attempt in range(retries):
        try:
            resp = requests.get(API_URL, params=params, timeout=20)
            if resp.status_code in (429, 503):
                time.sleep(3 * (attempt + 1))
                continue
            resp.raise_for_status()
            data = resp.json()
            if "Errors" in data:
                msgs = "; ".join(e.get("description", "") for e in data["Errors"])
                raise RuntimeError(f"NPPES API error on query '{params.get('taxonomy_description')}': {msgs}")
            return data.get("results", [])
        except requests.RequestException as e:
            if attempt == retries - 1:
                raise RuntimeError(f"Network error: {e}")
            time.sleep(2)
    return []


def run_search(wildcard, enum_type, state, city, postal_code, status_area):
    """Paginate one wildcard query. skip is capped at 1000 => max ~1,200 rows per query."""
    results = []
    skip = 0
    hit_cap = False
    while True:
        params = {
            "version": "2.1",
            "taxonomy_description": wildcard,
            "limit": 200,
            "skip": skip,
        }
        if enum_type:
            params["enumeration_type"] = enum_type  # must be exactly NPI-1 or NPI-2
        if state:
            params["state"] = state
        if city:
            params["city"] = city
        if postal_code:
            params["postal_code"] = postal_code

        status_area.write(f"Fetching '{wildcard}' … {len(results)} records so far")
        batch = fetch_page(params)
        results.extend(batch)

        if len(batch) < 200:
            break
        if skip >= 1000:  # API hard cap
            hit_cap = True
            break
        skip += 200
        time.sleep(0.5)  # polite rate limiting
    return results, hit_cap


def freshness_flag(days_since_open, last_updated):
    """Classify record vitality. New registrations are prospects, not ghosts —
    identical enum/update dates only imply staleness when the record is OLD."""
    if days_since_open is not None and days_since_open <= 365:
        return "New Business"
    if pd.isna(last_updated):
        return "Unknown"
    age_days = (datetime.now() - last_updated).days
    if age_days <= 730:
        return "Active"
    if age_days <= 1825:
        return "Aging"
    return "Stale - verify"


def to_record(item):
    basic = item.get("basic", {})
    addresses = item.get("addresses", []) or []
    # API actually returns camelCase "practiceLocations" (docs say practice_locations)
    practice_locs = item.get("practiceLocations") or item.get("practice_locations") or []
    taxonomies = item.get("taxonomies", []) or []

    primary_addr = next(
        (a for a in addresses if a.get("address_purpose") == "LOCATION"),
        addresses[0] if addresses else {},
    )

    enum_date = pd.to_datetime(basic.get("enumeration_date"), errors="coerce")
    last_updated = pd.to_datetime(basic.get("last_updated"), errors="coerce")
    days_since = (datetime.now() - enum_date).days if pd.notna(enum_date) else None

    tax_codes = {t.get("code") for t in taxonomies if t.get("code")}
    primary_tax = next((t for t in taxonomies if t.get("primary")), taxonomies[0] if taxonomies else {})

    # Endpoints: orgs can optionally list website/FHIR URLs and Direct
    # secure-messaging addresses. Sparse, but free signal when present.
    endpoints = item.get("endpoints", []) or []
    urls, directs = [], []
    for e in endpoints:
        ep = (e.get("endpoint") or "").strip()
        etype = (e.get("endpointType") or e.get("endpoint_type") or "").upper()
        if not ep:
            continue
        if ep.lower().startswith("http") or "URL" in etype or "WEBSITE" in etype:
            urls.append(ep)
        elif "@" in ep:
            directs.append(ep)
    urls = list(dict.fromkeys(urls))
    directs = list(dict.fromkeys(directs))

    org_name = basic.get("organization_name")
    parent = basic.get("parent_organization_legal_business_name")
    total_locations = 1 + len(practice_locs)

    # Prospect score: multi-location + growth signals + hearing-aid focus
    score = 0
    if total_locations >= 3:
        score += 50
    elif total_locations == 2:
        score += 30
    if days_since is not None and days_since <= 365:
        score += 20
    if pd.notna(last_updated) and (datetime.now() - last_updated).days <= 180:
        score += 15
    if tax_codes & HEARING_AID_CODES:
        score += 10

    return {
        "NPI": item.get("number"),
        "Name": org_name or f"{basic.get('first_name', '')} {basic.get('last_name', '')}".strip(),
        "Organization_Name": org_name,
        "Parent_Organization": parent,
        "Is_Subpart": basic.get("organizational_subpart"),
        "Type": item.get("enumeration_type"),
        "Status": basic.get("status"),
        "Total_Locations": total_locations,
        "Enumeration_Date": enum_date.strftime("%Y-%m-%d") if pd.notna(enum_date) else None,
        "Days_Since_Opened": days_since,
        "Last_Updated": last_updated.strftime("%Y-%m-%d") if pd.notna(last_updated) else None,
        "Record_Freshness": freshness_flag(days_since, last_updated),
        "State": primary_addr.get("state"),
        "City": primary_addr.get("city"),
        "ZIP": (primary_addr.get("postal_code") or "")[:5] or None,
        "Phone": primary_addr.get("telephone_number"),
        "Authorized_Official": f"{basic.get('authorized_official_first_name', '')} "
                               f"{basic.get('authorized_official_last_name', '')}".strip() or None,
        "Authorized_Official_Title": basic.get("authorized_official_title_or_position"),
        "Authorized_Official_Phone": basic.get("authorized_official_telephone_number"),
        "Website_or_URL": "; ".join(urls) or None,
        "Direct_Address": "; ".join(directs) or None,
        "Taxonomy_Codes": ", ".join(sorted(tax_codes)),
        "Matched_Codes": tax_codes & TARGET_CODES,  # set, used for filtering
        "Primary_Taxonomy": primary_tax.get("desc"),
        "Sells_Fits_Hearing_Aids": bool(tax_codes & HEARING_AID_CODES),
        "Prospect_Score": score,
    }


# ------------------------------------------------------------
# Shared post-processing (used by both search modes)
# ------------------------------------------------------------

def build_results_df(all_items):
    records = [to_record(it) for it in all_items.values()]
    # Keep only providers holding at least one of the four target taxonomies
    records = [r for r in records if r["Matched_Codes"]]
    df = pd.DataFrame(records)
    if not df.empty:
        # Chain grouping: parent org if present, else normalized org name
        df["Chain_Group"] = df["Parent_Organization"].apply(normalize_org_name)
        df["Chain_Group"] = df["Chain_Group"].fillna(df["Organization_Name"].apply(normalize_org_name))
        chain_sizes = df.groupby("Chain_Group")["NPI"].transform("count")
        df["Chain_Size"] = chain_sizes.where(df["Chain_Group"].notna(), 1).astype(int)
        df.loc[df["Chain_Size"] >= 3, "Prospect_Score"] += 15
    return df


@st.cache_data(ttl=21600, show_spinner=False)  # cache each state for 6h
def fetch_state_orgs(state_code, wildcard):
    """All NPI-2 records for one state+wildcard. Returns (items, hit_cap)."""
    results, skip = [], 0
    while True:
        params = {
            "version": "2.1",
            "taxonomy_description": wildcard,
            "enumeration_type": "NPI-2",
            "state": state_code,
            "limit": 200,
            "skip": skip,
        }
        batch = fetch_page(params)
        results.extend(batch)
        if len(batch) < 200:
            return results, False
        if skip >= 1000:
            return results, True
        skip += 200
        time.sleep(0.4)


# ------------------------------------------------------------
# Hunter.io email enrichment (optional, credit-metered)
# ------------------------------------------------------------

def _hunter_request(company, api_key):
    """One Hunter Domain Search call by company name. Returns parsed dict."""
    resp = requests.get(
        "https://api.hunter.io/v2/domain-search",
        params={"company": company, "api_key": api_key, "limit": 5},
        timeout=20,
    )
    if resp.status_code == 401:
        raise RuntimeError("Hunter.io rejected the API key (401). Check the key in your app secrets.")
    if resp.status_code == 429:
        raise RuntimeError("Hunter.io rate limit reached (429). Try again later or reduce batch size.")
    data = resp.json().get("data") or {}
    emails = []
    for e in data.get("emails", []) or []:
        val = e.get("value")
        if val:
            conf = e.get("confidence")
            emails.append(f"{val} ({conf}%)" if conf is not None else val)
    return {
        "domain": data.get("domain"),
        "company_found": data.get("organization"),
        "emails": "; ".join(emails) or None,
    }


@st.cache_data(ttl=604800, show_spinner=False)  # cache 7 days — each lookup costs a credit
def hunter_lookup(company, _api_key):
    """Cached wrapper (api key excluded from the cache key via underscore prefix)."""
    try:
        return _hunter_request(company, _api_key)
    except RuntimeError:
        raise
    except Exception as e:
        return {"domain": None, "company_found": None, "emails": None, "error": str(e)}


# ------------------------------------------------------------
# Sidebar — search inputs
# ------------------------------------------------------------
st.sidebar.header("Search")

enum_choice = st.sidebar.selectbox("Entity type", ["Both", "Individual (NPI-1)", "Organization (NPI-2)"])
ENUM_MAP = {"Both": None, "Individual (NPI-1)": "NPI-1", "Organization (NPI-2)": "NPI-2"}

state = st.sidebar.text_input("State (2-letter, e.g. CA)", max_chars=2).strip().upper()
city = st.sidebar.text_input("City (optional)").strip()
postal_code = st.sidebar.text_input("ZIP (optional, partial ok)").strip()

st.sidebar.caption(
    "The app searches broadly (Hearing*, Audiolog*) and then filters to exact "
    "taxonomy codes. The API returns at most ~1,200 records per query — for "
    "national coverage, run once per state."
)

search_clicked = st.sidebar.button("🔍 Search NPPES", type="primary")

st.sidebar.divider()
st.sidebar.subheader("🆕 Nationwide shortcut")
new_days = st.sidebar.selectbox("New organizations in the past…", [30, 60, 90, 180], index=2,
                                format_func=lambda d: f"{d} days")
st.sidebar.caption(
    "Sweeps every state for newly registered NPI-2 organizations. "
    "First run takes a few minutes; results are cached for 6 hours."
)
national_clicked = st.sidebar.button("Find new NPI-2s nationwide")

# ------------------------------------------------------------
# Run search (results persist in session_state)
# ------------------------------------------------------------
if search_clicked:
    status_area = st.empty()
    all_items, capped_queries = {}, []
    try:
        with st.spinner("Querying NPPES Registry…"):
            for wc in SEARCH_WILDCARDS:
                items, hit_cap = run_search(
                    wc, ENUM_MAP[enum_choice], state, city, postal_code, status_area
                )
                if hit_cap:
                    capped_queries.append(wc)
                for it in items:
                    all_items[it.get("number")] = it  # dedupe by NPI

        status_area.empty()
        st.session_state["results_df"] = build_results_df(all_items)
        st.session_state["capped"] = capped_queries
        st.session_state["mode"] = "search"
    except RuntimeError as e:
        status_area.empty()
        st.error(str(e))

if national_clicked:
    all_items, capped_states = {}, []
    progress = st.progress(0.0, text="Sweeping states for new NPI-2 organizations…")
    try:
        total = len(STATES) * len(SEARCH_WILDCARDS)
        step = 0
        for st_code in STATES:
            for wc in SEARCH_WILDCARDS:
                step += 1
                progress.progress(step / total, text=f"Checking {st_code} ({wc}) — {step}/{total}")
                items, hit_cap = fetch_state_orgs(st_code, wc)
                if hit_cap:
                    capped_states.append(f"{st_code}/{wc}")
                for it in items:
                    all_items[it.get("number")] = it
        progress.empty()

        df = build_results_df(all_items)
        if not df.empty:
            df = df[df["Days_Since_Opened"].notna() & (df["Days_Since_Opened"] <= new_days)]
        st.session_state["results_df"] = df.reset_index(drop=True)
        st.session_state["capped"] = capped_states
        st.session_state["mode"] = "national_new"
        st.session_state["national_days"] = new_days
    except RuntimeError as e:
        progress.empty()
        st.error(str(e))

# ------------------------------------------------------------
# Display + interactive filtering (survives reruns)
# ------------------------------------------------------------
if "results_df" in st.session_state:
    df = st.session_state["results_df"]
    capped = st.session_state.get("capped", [])

    if df.empty:
        if st.session_state.get("mode") == "national_new":
            st.warning(f"No new NPI-2 organizations found in the past "
                       f"{st.session_state.get('national_days', 90)} days.")
        else:
            st.warning("No matching providers. Try a different state or remove the city/ZIP filter.")
    else:
        if st.session_state.get("mode") == "national_new":
            nd = st.session_state.get("national_days", 90)
            st.success(f"🆕 {len(df):,} new NPI-2 organizations registered nationwide in the past {nd} days")
            by_state = (
                df.groupby("State")
                .agg(New_Orgs=("NPI", "count"),
                     Newest=("Enumeration_Date", "max"))
                .sort_values("New_Orgs", ascending=False)
            )
            st.markdown("**New organizations by state:**")
            st.dataframe(by_state, height=min(420, 40 + 35 * len(by_state)))
        else:
            st.success(f"✅ {len(df):,} unique providers with hearing-related taxonomies found")
        if capped:
            st.warning(
                f"Result cap (~1,200) hit for: {', '.join(capped)}. "
                "Narrow by state/city/ZIP for complete coverage."
            )

        # --- Post-search filters (all client-side, instant) ---
        st.subheader("🎯 Qualify prospects")

        selected_labels = st.multiselect(
            "Provider types (taxonomy codes)",
            options=[f"{v} ({k})" for k, v in CODE_LABELS.items()],
            default=[f"{CODE_LABELS[c]} ({c})" for c in HEARING_AID_CODES],
        )
        selected_codes = {lbl.split("(")[-1].rstrip(")") for lbl in selected_labels}

        c1, c2, c3 = st.columns(3)
        with c1:
            min_locations = st.slider("Min. locations", 1, 20, 1)
        with c2:
            recent_only = st.checkbox("Recently opened only")
            days_back = st.slider("…within days", 30, 730, 365, step=30, disabled=not recent_only)
        with c3:
            min_score = st.slider("Min. prospect score", 0, 100, 0, step=5)

        f = df.copy()
        if selected_codes:
            f = f[f["Matched_Codes"].apply(lambda s: bool(s & selected_codes))]
        if min_locations > 1:
            f = f[f["Total_Locations"] >= min_locations]
        if recent_only:
            f = f[f["Days_Since_Opened"].notna() & (f["Days_Since_Opened"] <= days_back)]
        if min_score > 0:
            f = f[f["Prospect_Score"] >= min_score]

        name_q = st.text_input("🔎 Filter by name / organization")
        if name_q:
            f = f[f["Name"].str.contains(name_q, case=False, na=False)]

        # --- Chain analysis ---
        st.subheader("🔗 Chains & common ownership")
        chains = (
            f[f["Chain_Group"].notna()]
            .groupby("Chain_Group")
            .agg(
                Locations_Found=("NPI", "count"),
                States=("State", lambda s: ", ".join(sorted(set(s.dropna())))),
            )
            .sort_values("Locations_Found", ascending=False)
        )
        multi = chains[chains["Locations_Found"] >= 2]
        if not multi.empty:
            st.markdown(f"**{len(multi)}** groups with 2+ NPIs in these results:")
            st.dataframe(multi.head(25))
            pick = st.selectbox("Drill into a chain", ["(all)"] + multi.index.tolist())
            if pick != "(all)":
                f = f[f["Chain_Group"] == pick]
        else:
            st.info("No multi-NPI groups detected in the current filtered set.")

        # --- Optional map via ZIP centroids (API provides no coordinates) ---
        with st.expander("📍 Map (approximate, by ZIP code)"):
            if st.checkbox("Load map", help="Downloads a ZIP-code lookup table on first use"):
                @st.cache_data(show_spinner="Loading ZIP coordinate data…")
                def zip_coords(zips_tuple):
                    import pgeocode
                    nomi = pgeocode.Nominatim("us")
                    geo = nomi.query_postal_code(list(zips_tuple))[
                        ["postal_code", "latitude", "longitude"]
                    ]
                    return geo.rename(columns={"postal_code": "ZIP"})

                try:
                    zips = tuple(sorted(f["ZIP"].dropna().unique()))
                    if zips:
                        m = f.merge(zip_coords(zips), on="ZIP", how="left").dropna(
                            subset=["latitude", "longitude"]
                        )
                        if not m.empty:
                            st.map(m[["latitude", "longitude"]])
                            st.caption("Pins are ZIP-code centroids — NPPES does not publish exact coordinates.")
                        else:
                            st.info("No mappable ZIPs in the current results.")
                except Exception:
                    st.info("Map unavailable (ZIP lookup download failed on this host).")

        # --- Results table with row selection ---
        st.subheader(f"📋 {len(f):,} qualified prospects")
        show_cols = [
            "Prospect_Score", "Name", "Total_Locations", "Chain_Size", "Type",
            "Record_Freshness", "Enumeration_Date", "Last_Updated", "City", "State", "ZIP", "Phone",
            "Authorized_Official", "Authorized_Official_Title", "Authorized_Official_Phone",
            "Website_or_URL", "Direct_Address",
            "Primary_Taxonomy", "Taxonomy_Codes", "NPI",
        ]

        if "selected_npis" not in st.session_state:
            st.session_state["selected_npis"] = set()
        if "sel_nonce" not in st.session_state:
            st.session_state["sel_nonce"] = 0

        f_sorted = f.sort_values(
            ["Prospect_Score", "Total_Locations"], ascending=False
        ).reset_index(drop=True)
        visible_npis = f_sorted["NPI"].tolist()

        b1, b2, b3 = st.columns([1, 1, 2])
        with b1:
            if st.button("✅ Select all"):
                st.session_state["selected_npis"] |= set(visible_npis)
                st.session_state["sel_nonce"] += 1
        with b2:
            if st.button("⬜ Clear selection"):
                st.session_state["selected_npis"] -= set(visible_npis)
                st.session_state["sel_nonce"] += 1

        table = f_sorted[show_cols].copy()
        table.insert(0, "Select", f_sorted["NPI"].isin(st.session_state["selected_npis"]))

        key_sig = hashlib.md5(",".join(map(str, visible_npis)).encode()).hexdigest()[:10]
        edited = st.data_editor(
            table,
            hide_index=True,
            disabled=show_cols,  # only the Select checkbox is editable
            column_config={"Select": st.column_config.CheckboxColumn("Select", default=False)},
            key=f"prospect_editor_{st.session_state['sel_nonce']}_{key_sig}",
        )
        # Sync ticked rows back into the persistent, NPI-keyed selection set
        sel_now = set(edited.loc[edited["Select"], "NPI"])
        st.session_state["selected_npis"] = (
            st.session_state["selected_npis"] - set(visible_npis)
        ) | sel_now
        n_sel = len(st.session_state["selected_npis"] & set(visible_npis))
        with b3:
            st.markdown(f"**{n_sel}** of {len(f_sorted)} rows selected")

        # --- Hunter.io email enrichment ---
        st.subheader("📧 Email enrichment (Hunter.io)")
        hunter_key = None
        try:
            hunter_key = st.secrets.get("HUNTER_API_KEY")
        except Exception:
            pass
        if not hunter_key:
            hunter_key = st.text_input(
                "Hunter.io API key", type="password",
                help="Better: store it once in Streamlit Cloud → app Settings → Secrets as "
                     'HUNTER_API_KEY = "your-key" so you never paste it again.',
            )

        orgs_only = f[(f["Type"] == "NPI-2") & f["Organization_Name"].notna()]
        hc1, hc2 = st.columns([1, 2])
        with hc1:
            max_enrich = st.slider("Orgs to enrich (top by score)", 5, 50, 20, step=5,
                                   help="Each org costs 1 Hunter search credit (cached 7 days).")
        with hc2:
            st.caption(f"{len(orgs_only):,} organizations in the current filtered results are eligible. "
                       "Individual providers (NPI-1) are skipped — Hunter works on companies.")

        if st.button("Find emails", disabled=not hunter_key):
            targets = orgs_only.sort_values("Prospect_Score", ascending=False).head(max_enrich)
            enriched = st.session_state.get("hunter", {})
            prog = st.progress(0.0, text="Querying Hunter.io…")
            try:
                for i, (_, row) in enumerate(targets.iterrows(), 1):
                    prog.progress(i / len(targets), text=f"Hunter.io: {row['Organization_Name'][:40]} ({i}/{len(targets)})")
                    enriched[row["NPI"]] = hunter_lookup(row["Organization_Name"], hunter_key)
                st.session_state["hunter"] = enriched
            except RuntimeError as e:
                st.error(str(e))
            prog.empty()

        hunter_data = st.session_state.get("hunter", {})
        if hunter_data:
            f["Hunter_Domain"] = f["NPI"].map(lambda n: (hunter_data.get(n) or {}).get("domain"))
            f["Hunter_Company_Match"] = f["NPI"].map(lambda n: (hunter_data.get(n) or {}).get("company_found"))
            f["Hunter_Emails"] = f["NPI"].map(lambda n: (hunter_data.get(n) or {}).get("emails"))
            found = f["Hunter_Emails"].notna().sum()
            st.markdown(f"**Emails found for {found} of {len(hunter_data)} enriched organizations:**")
            st.dataframe(
                f[f["NPI"].isin(hunter_data.keys())][
                    ["Name", "City", "State", "Hunter_Company_Match", "Hunter_Domain", "Hunter_Emails"]
                ],
                hide_index=True,
            )
            st.caption("⚠️ Verify Hunter_Company_Match against the clinic name — generic names can "
                       "resolve to the wrong company. Confidence % shown per email.")

        # --- Exports ---
        st.subheader("📤 Export")
        sel_set = st.session_state.get("selected_npis", set()) & set(visible_npis)
        scope = st.radio(
            "Rows to export",
            [f"All filtered rows ({len(f_sorted)})", f"Selected rows only ({len(sel_set)})"],
            horizontal=True,
        )
        use_selected = scope.startswith("Selected")
        if use_selected and not sel_set:
            st.info("No rows selected yet — tick rows in the table above or use ✅ Select all.")

        exp_base = f_sorted[f_sorted["NPI"].isin(sel_set)] if use_selected else f_sorted

        enrich = st.checkbox(
            "Look up emails via Hunter.io before export",
            disabled=not hunter_key,
            help="NPI-2 organizations only; 1 search credit per uncached lookup (cached 7 days)."
                 + ("" if hunter_key else " Add your Hunter API key above to enable."),
        )
        if enrich:
            org_mask = (exp_base["Type"] == "NPI-2") & exp_base["Organization_Name"].notna()
            cached = st.session_state.get("hunter", {})
            n_new = sum(1 for n in exp_base.loc[org_mask, "NPI"] if n not in cached)
            st.caption(f"Will look up {org_mask.sum()} organizations "
                       f"(~{n_new} new Hunter credits; the rest come from cache).")

        if st.button("⚙️ Prepare export", disabled=(use_selected and not sel_set)):
            exp = exp_base.drop(columns=["Matched_Codes"], errors="ignore").copy()

            if enrich and hunter_key:
                enriched = st.session_state.get("hunter", {})
                orgs = exp[(exp["Type"] == "NPI-2") & exp["Organization_Name"].notna()]
                prog = st.progress(0.0, text="Hunter.io lookups…")
                try:
                    for i, (_, row) in enumerate(orgs.iterrows(), 1):
                        prog.progress(i / max(len(orgs), 1),
                                      text=f"Hunter.io: {str(row['Organization_Name'])[:40]} ({i}/{len(orgs)})")
                        if row["NPI"] not in enriched:
                            enriched[row["NPI"]] = hunter_lookup(row["Organization_Name"], hunter_key)
                    st.session_state["hunter"] = enriched
                except RuntimeError as e:
                    st.error(str(e))
                prog.empty()

            hunter_data = st.session_state.get("hunter", {})
            if hunter_data:
                exp["Hunter_Domain"] = exp["NPI"].map(lambda n: (hunter_data.get(n) or {}).get("domain"))
                exp["Hunter_Company_Match"] = exp["NPI"].map(lambda n: (hunter_data.get(n) or {}).get("company_found"))
                exp["Hunter_Emails"] = exp["NPI"].map(lambda n: (hunter_data.get(n) or {}).get("emails"))

            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                exp.to_excel(writer, index=False, sheet_name="Prospects")
            buf.seek(0)
            st.session_state["export_pkg"] = {
                "csv": exp.to_csv(index=False).encode("utf-8"),
                "xlsx": buf.getvalue(),
                "n": len(exp),
                "stamp": datetime.now().strftime("%Y%m%d_%H%M"),
                "enriched": bool(enrich and hunter_key),
            }

        pkg = st.session_state.get("export_pkg")
        if pkg:
            st.markdown(f"Export ready: **{pkg['n']} rows**"
                        + (" — includes Hunter email columns" if pkg["enriched"] else ""))
            e1, e2 = st.columns(2)
            with e1:
                st.download_button(
                    "Download CSV",
                    pkg["csv"],
                    file_name=f"hearing_aid_prospects_{pkg['stamp']}.csv",
                    mime="text/csv",
                )
            with e2:
                st.download_button(
                    "Download Excel (.xlsx)",
                    pkg["xlsx"],
                    file_name=f"hearing_aid_prospects_{pkg['stamp']}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
else:
    st.info("👈 Pick a state (recommended) and click **Search NPPES**. Provider-type filtering happens after the search, instantly.")

st.caption(
    "Data: official NPPES NPI Registry API v2.1 • Broad wildcard search + exact taxonomy-code "
    "filtering • Each API query caps at ~1,200 records • NPI enumeration date ≈ proxy for opening date"
)
