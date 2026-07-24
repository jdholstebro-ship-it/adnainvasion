import streamlit as st
import requests
import pandas as pd
import re
import time
from datetime import datetime
from thefuzz import process

st.set_page_config(page_title="Hearing Aid Clinic Prospect Finder", layout="wide")
st.title("🦻 Hearing Aid Clinic Prospect Finder")
st.markdown("**Targeted for multi-location hearing aid sellers & fitters** — Ideal prospects for practice management software (scheduling, AI notes, resource optimization).")

# Sidebar
st.sidebar.header("Search Filters")

taxonomy_options = {
    "Audiologist-Hearing Aid Fitter (237600000X)": "237600000X",
    "Hearing Instrument Specialist (237700000X)": "237700000X",
    "Hearing Aid Equipment Supplier (332S00000X)": "332S00000X",
    "Any Hearing Aid Related": "Hearing*"
}

selected_tax = st.sidebar.selectbox("Provider Type (Hearing Aid Focused)", options=list(taxonomy_options.keys()))
taxonomy = taxonomy_options[selected_tax]

enumeration_type = st.sidebar.selectbox("Entity Type", ["Both", "Individual (NPI-1)", "Organization (NPI-2)"])

org_name = st.sidebar.text_input("Organization Name (optional, use * for wildcard)")
official_search = st.sidebar.text_input("Authorized Official (optional, use * for wildcard)")
state = st.sidebar.text_input("State (e.g. CA, TX, FL)", max_chars=2).upper()
city = st.sidebar.text_input("City (optional)")
postal_code = st.sidebar.text_input("ZIP Code (optional)")

max_results = st.sidebar.slider("Maximum Results", 100, 2000, 800, step=100)

st.sidebar.subheader("Prospect Filters")
days_back = st.sidebar.slider("Opened in last X days", 30, 730, 365, step=30)
include_all = st.sidebar.checkbox("Include older providers", value=True)
min_locations = st.sidebar.slider("Minimum Locations", 1, 20, 2)   # Default 2+ for your ideal customer

search_button = st.sidebar.button("🔍 Search Hearing Aid Providers", type="primary")


def wildcard_to_regex(pattern):
    """Convert a user wildcard pattern (using *) into a case-insensitive regex.
    A pattern with no * is treated as 'contains'."""
    pattern = pattern.strip()
    if not pattern:
        return None
    # Escape everything, then turn the escaped \* back into .*
    escaped = re.escape(pattern).replace(r"\*", ".*")
    if "*" not in pattern:
        # No wildcard: match anywhere in the string (contains)
        regex = escaped
    else:
        # Wildcard given: anchor to the start so 'hear*' means 'starts with hear'
        regex = "^" + escaped
    return re.compile(regex, re.IGNORECASE)


if search_button:
    with st.spinner("Querying NPPES Registry..."):
        params = {
            "version": "2.1",
            "taxonomy_description": taxonomy,
            "limit": 200,
            "skip": 0
        }

        if enumeration_type != "Both":
            params["enumeration_type"] = enumeration_type.replace(" (NPI-1)", "").replace(" (NPI-2)", "")

        if org_name: params["organization_name"] = org_name
        if state: params["state"] = state
        if city: params["city"] = city
        if postal_code: params["postal_code"] = postal_code

        all_results = []
        skip = 0
        while len(all_results) < max_results:
            params["skip"] = skip
            try:
                resp = requests.get("https://npiregistry.cms.hhs.gov/api/", params=params, timeout=20)
                resp.raise_for_status()
                results = resp.json().get("results", [])
                all_results.extend(results)
                if len(results) < 200: break
                skip += 200
                time.sleep(0.7)
            except Exception as e:
                st.error(f"API Error: {e}")
                break

        if all_results:
            records = []
            for item in all_results:
                basic = item.get("basic", {})
                addresses = item.get("addresses", [])
                practice_locs = item.get("practice_locations", [])
                taxonomies = item.get("taxonomies", [])
                other_names = item.get("other_names", [])

                primary_addr = next((a for a in addresses if a.get("address_purpose") == "LOCATION"),
                                  addresses[0] if addresses else {})

                # DBA / other names: NPPES returns a list of {organization_name, type, code}
                dba_names = [o.get("organization_name") for o in other_names if o.get("organization_name")]
                dba_name = "; ".join(dba_names) if dba_names else None

                enum_date = pd.to_datetime(basic.get("enumeration_date"), errors='coerce')
                last_updated = pd.to_datetime(basic.get("last_updated"), errors='coerce')
                days_since = (datetime.now() - enum_date).days if pd.notna(enum_date) else None

                total_locations = 1 + len(practice_locs)

                official_name = f"{basic.get('authorized_official_first_name','')} {basic.get('authorized_official_last_name','')}".strip()

                # Simple Prospect Score (higher = better fit for your software)
                score = 0
                if total_locations >= 3: score += 50
                elif total_locations >= 2: score += 30
                if days_since and days_since < 365: score += 20
                if last_updated and (datetime.now() - last_updated).days < 180: score += 15

                record = {
                    "NPI": item.get("number"),
                    "Entity_Type": "Organization (NPI-2)" if item.get("enumeration_type") == "NPI-2" else "Individual (NPI-1)",
                    "Name": basic.get("organization_name") or f"{basic.get('first_name','')} {basic.get('last_name','')}".strip(),
                    "Organization_Name": basic.get("organization_name"),
                    "DBA_Name": dba_name,
                    "Parent_Organization": basic.get("parent_organization_legal_business_name"),
                    "Total_Locations": total_locations,
                    "Enumeration_Date": enum_date.strftime('%Y-%m-%d') if pd.notna(enum_date) else None,
                    "Days_Since_Opened": days_since,
                    "Last_Updated": last_updated.strftime('%Y-%m-%d') if pd.notna(last_updated) else None,
                    "State": primary_addr.get("state"),
                    "City": primary_addr.get("city"),
                    "ZIP": primary_addr.get("postal_code"),
                    "Phone": primary_addr.get("telephone_number"),
                    "Authorized_Official": official_name,
                    "Authorized_Official_Title": basic.get("authorized_official_title_or_position"),
                    "Authorized_Official_Phone": basic.get("authorized_official_telephone_number"),
                    "EIN": basic.get("ein"),
                    "Primary_Taxonomy": next((t.get("desc") for t in taxonomies if t.get("primary")), ""),
                    "Prospect_Score": score,
                    "Latitude": primary_addr.get("latitude"),
                    "Longitude": primary_addr.get("longitude"),
                }
                records.append(record)

            df = pd.DataFrame(records)

            # Apply filters
            if not include_all:
                df = df[df["Days_Since_Opened"] <= days_back]
            df = df[df["Total_Locations"] >= min_locations]

            # Authorized Official filter (client-side, supports * wildcard)
            official_regex = wildcard_to_regex(official_search)
            if official_regex is not None:
                df = df[df["Authorized_Official"].fillna("").apply(lambda x: bool(official_regex.search(x)))]

            # Group_Locations: how many NPIs share the same chain (parent, falling back to legal name)
            df["Chain_Group"] = df["Parent_Organization"].fillna(df["Organization_Name"])
            group_sizes = df.groupby("Chain_Group")["NPI"].transform("count")
            df["Group_Locations"] = group_sizes

            st.success(f"✅ Found **{len(df)}** qualified hearing aid providers")

            # Chain Detection (Exact + Fuzzy)
            st.subheader("🔗 Chain Detection")
            if len(df) > 5:
                org_names = df["Chain_Group"].dropna().unique()
                df["Fuzzy_Chain"] = df["Chain_Group"].apply(
                    lambda x: process.extractOne(x, org_names, score_cutoff=85)[0] if pd.notna(x) else x
                )
            chain_col = "Fuzzy_Chain" if "Fuzzy_Chain" in df.columns else "Chain_Group"

            # Map
            st.subheader("📍 Locations Map")
            map_df = df.dropna(subset=["Latitude", "Longitude"])
            if not map_df.empty:
                st.map(map_df[["Latitude", "Longitude"]])
            else:
                st.info("No coordinates available.")

            # Results Table with row selection
            st.subheader("📋 Results")
            st.caption("Tick the rows you want to export. If you select none, all rows are exported.")

            display_cols = ["NPI", "Name", "DBA_Name", "Entity_Type", "Total_Locations",
                           "Group_Locations", "Prospect_Score", "Chain_Group",
                           "Enumeration_Date", "Last_Updated", "City", "State", "Phone",
                           "Authorized_Official", "Authorized_Official_Title"]

            display_df = df[display_cols].sort_values(
                ["Prospect_Score", "Group_Locations"], ascending=False
            ).reset_index(drop=True)

            selection = st.dataframe(
                display_df,
                use_container_width=True,
                hide_index=True,
                on_select="rerun",
                selection_mode="multi-row",
                key="results_table",
                column_config={
                    "Prospect_Score": st.column_config.NumberColumn("Prospect Score (0-85)"),
                    "Total_Locations": st.column_config.NumberColumn("Locations (this NPI)"),
                    "Group_Locations": st.column_config.NumberColumn("Chain Size (NPIs)"),
                    "DBA_Name": st.column_config.TextColumn("DBA / Other Name"),
                }
            )

            # Determine which rows to export
            selected_rows = selection.selection.rows if selection and selection.selection else []
            if selected_rows:
                # Map the selected display rows back to the full df via NPI
                selected_npis = display_df.iloc[selected_rows]["NPI"].tolist()
                export_df = df[df["NPI"].isin(selected_npis)]
                st.info(f"{len(export_df)} row(s) selected for export.")
            else:
                export_df = df
                st.info("No rows selected — all rows will be exported.")

            # === EXPORTS ===
            st.subheader("📤 Export Prospects")

            col1, col2 = st.columns(2)

            with col1:
                csv = export_df.to_csv(index=False).encode('utf-8')
                st.download_button(
                    label="📥 Download CSV",
                    data=csv,
                    file_name=f"hearing_aid_prospects_{datetime.now().strftime('%Y%m%d')}.csv",
                    mime="text/csv"
                )

            with col2:
                # Excel Download using BytesIO
                buffer = pd.io.common.BytesIO()
                with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                    export_df.to_excel(writer, index=False, sheet_name='Hearing Aid Prospects')
                buffer.seek(0)

                st.download_button(
                    label="📥 Download Excel (.xlsx)",
                    data=buffer,
                    file_name=f"hearing_aid_prospects_{datetime.now().strftime('%Y%m%d')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )

        else:
            st.warning("No results found with current filters.")

else:
    st.info("👈 Use the sidebar to find multi-location hearing aid clinics that are strong prospects for your practice management platform.")

st.caption("Focused on hearing aid fitting/sales providers • Prospect scoring based on locations + growth signals • Data from official NPPES API")
