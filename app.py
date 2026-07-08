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
        "State": primary_addr.get("state"),
        "City": primary_addr.get("city"),
        "ZIP": (primary_addr.get("postal_code") or "")[:5] or None,
        "Phone": primary_addr.get("telephone_number"),
        "Authorized_Official": f"{basic.get('authorized_official_first_name', '')} "
                               f"{basic.get('authorized_official_last_name', '')}".strip() or None,
        "Authorized_Official_Title": basic.get("authorized_official_title_or_position"),
        "Authorized_Official_Phone": basic.get("authorized_official_telephone_number"),
        "Taxonomy_Codes": ", ".join(sorted(tax_codes)),
        "Matched_Codes": tax_codes & TARGET_CODES,  # set, used for filtering
        "Primary_Taxonomy": primary_tax.get("desc"),
        "Sells_Fits_Hearing_Aids": bool(tax_codes & HEARING_AID_CODES),
        "Prospect_Score": score,
    }


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

        st.session_state["results_df"] = df
        st.session_state["capped"] = capped_queries
    except RuntimeError as e:
        status_area.empty()
        st.error(str(e))

# ------------------------------------------------------------
# Display + interactive filtering (survives reruns)
# ------------------------------------------------------------
if "results_df" in st.session_state:
    df = st.session_state["results_df"]
    capped = st.session_state.get("capped", [])

    if df.empty:
        st.warning("No matching providers. Try a different state or remove the city/ZIP filter.")
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
            st.dataframe(multi.head(25), use_container_width=True)
            pick = st.selectbox("Drill into a chain", ["(all)"] + multi.index.tolist())
            if pick != "(all)":
                f = f[f["Chain_Group"] == pick]
        else:
            st.info("No multi-NPI groups detected in the current filtered set.")

        # --- Optional map via ZIP centroids (API provides no coordinates) ---
        with st.expander("📍 Map (approximate, by ZIP code)"):
            try:
                import pgeocode
                nomi = pgeocode.Nominatim("us")
                zips = f["ZIP"].dropna().unique()
                if len(zips) > 0:
                    geo = nomi.query_postal_code(list(zips))[["postal_code", "latitude", "longitude"]]
                    geo = geo.rename(columns={"postal_code": "ZIP"})
                    m = f.merge(geo, on="ZIP", how="left").dropna(subset=["latitude", "longitude"])
                    if not m.empty:
                        st.map(m[["latitude", "longitude"]])
                        st.caption("Pins are ZIP-code centroids — NPPES does not publish exact coordinates.")
                    else:
                        st.info("No mappable ZIPs in the current results.")
            except Exception:
                st.info("Map unavailable (pgeocode not installed or ZIP lookup failed).")

        # --- Results table ---
        st.subheader(f"📋 {len(f):,} qualified prospects")
        show_cols = [
            "Prospect_Score", "Name", "Total_Locations", "Chain_Size", "Type",
            "Enumeration_Date", "Last_Updated", "City", "State", "ZIP", "Phone",
            "Authorized_Official", "Authorized_Official_Title", "Authorized_Official_Phone",
            "Primary_Taxonomy", "Taxonomy_Codes", "NPI",
        ]
        st.dataframe(
            f[show_cols].sort_values(["Prospect_Score", "Total_Locations"], ascending=False),
            use_container_width=True,
            hide_index=True,
        )

        # --- Exports ---
        st.subheader("📤 Export")
        export_df = f.drop(columns=["Matched_Codes"], errors="ignore")
        e1, e2 = st.columns(2)
        stamp = datetime.now().strftime("%Y%m%d")
        with e1:
            st.download_button(
                "Download CSV",
                export_df.to_csv(index=False).encode("utf-8"),
                file_name=f"hearing_aid_prospects_{stamp}.csv",
                mime="text/csv",
            )
        with e2:
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                export_df.to_excel(writer, index=False, sheet_name="Prospects")
            buf.seek(0)
            st.download_button(
                "Download Excel (.xlsx)",
                buf,
                file_name=f"hearing_aid_prospects_{stamp}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
else:
    st.info("👈 Pick a state (recommended) and click **Search NPPES**. Provider-type filtering happens after the search, instantly.")

st.caption(
    "Data: official NPPES NPI Registry API v2.1 • Broad wildcard search + exact taxonomy-code "
    "filtering • Each API query caps at ~1,200 records • NPI enumeration date ≈ proxy for opening date"
)
