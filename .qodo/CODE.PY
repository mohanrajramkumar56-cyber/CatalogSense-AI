# autometa_app.py
import os, time, json, re, urllib.parse, requests, streamlit as st

# -----------------------------
# Optional: spaCy for NER fallback
# -----------------------------
try:
    import spacy
    nlp = spacy.load("en_core_web_sm")
except Exception:
    nlp = None

# -----------------------------
# Utilities
# -----------------------------
def normalize_host(host: str) -> str:
    if not host: return ""
    h = host.strip().replace("https://", "").replace("http://", "")
    return h.rstrip("/")

def make_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token.strip()}", "Content-Type": "application/json"}

def pretty_resp(resp):
    try:
        return json.dumps(resp.json(), indent=2)
    except Exception:
        return resp.text

# -----------------------------
# Databricks helpers (unchanged)
# -----------------------------
def test_connection_all(host, token, warehouse=None, timeout=10):
    hostn = normalize_host(host)
    headers = make_headers(token)
    results = {"scim": None, "catalog": None, "sql": None}
    # SCIM
    try:
        url = f"https://{hostn}/api/2.0/preview/scim/v2/Me"
        r = requests.get(url, headers=headers, timeout=timeout)
        results["scim"] = {"ok": r.status_code == 200, "status": r.status_code, "body": pretty_resp(r)}
    except Exception as e:
        results["scim"] = {"ok": False, "status": None, "body": str(e)}
    # Catalog
    try:
        url = f"https://{hostn}/api/2.1/unity-catalog/catalogs"
        r = requests.get(url, headers=headers, timeout=timeout)
        results["catalog"] = {"ok": r.status_code == 200, "status": r.status_code, "body": pretty_resp(r)}
    except Exception as e:
        results["catalog"] = {"ok": False, "status": None, "body": str(e)}
    # SQL
    if warehouse:
        try:
            url = f"https://{hostn}/api/2.0/sql/statements"
            payload = {"statement": "SELECT 1", "warehouse_id": warehouse}
            r = requests.post(url, headers=headers, json=payload, timeout=timeout)
            sid = r.json().get("statement_id") or r.json().get("id")
            poll_url = f"https://{hostn}/api/2.0/sql/statements/{sid}"
            start = time.time()
            while time.time() - start < 30:
                rr = requests.get(poll_url, headers=headers, timeout=timeout)
                j = rr.json()
                state = j.get("status", {}).get("state")
                if state in ("SUCCEEDED", "FINISHED"):
                    results["sql"] = {"ok": True, "status": rr.status_code, "body": json.dumps(j, indent=2)}
                    break
                if state in ("FAILED", "CANCELED"):
                    results["sql"] = {"ok": False, "status": rr.status_code, "body": json.dumps(j, indent=2)}
                    break
                time.sleep(1)
            else:
                results["sql"] = {"ok": False, "status": None, "body": "SQL poll timeout"}
        except Exception as e:
            results["sql"] = {"ok": False, "status": None, "body": str(e)}
    else:
        results["sql"] = {"ok": None, "status": None, "body": "No warehouse provided - skipping SQL test."}
    return results

def list_catalogs(host, token, timeout=30):
    hostn = normalize_host(host)
    url = f"https://{hostn}/api/2.1/unity-catalog/catalogs"
    r = requests.get(url, headers=make_headers(token), timeout=timeout)
    r.raise_for_status()
    return r.json().get("catalogs") or r.json()

def list_schemas(host, token, catalog_name, timeout=30):
    hostn = normalize_host(host)
    url = f"https://{hostn}/api/2.1/unity-catalog/schemas?catalog_name={urllib.parse.quote(catalog_name)}"
    r = requests.get(url, headers=make_headers(token), timeout=timeout)
    r.raise_for_status()
    return r.json().get("schemas") or r.json()

def list_tables(host, token, catalog_name, schema_name, timeout=30):
    hostn = normalize_host(host)
    url = f"https://{hostn}/api/2.1/unity-catalog/tables?catalog_name={urllib.parse.quote(catalog_name)}&schema_name={urllib.parse.quote(schema_name)}"
    r = requests.get(url, headers=make_headers(token), timeout=timeout)
    r.raise_for_status()
    return r.json().get("tables") or r.json()

def get_table_metadata(host, token, full_table_name, timeout=30):
    hostn = normalize_host(host)
    url = f"https://{hostn}/api/2.1/unity-catalog/tables/{urllib.parse.quote(full_table_name)}"
    r = requests.get(url, headers=make_headers(token), timeout=timeout)
    r.raise_for_status()
    return r.json()

def fetch_sample_rows(host, token, warehouse_id, full_table_name, limit=5):
    sql_statement = f"SELECT * FROM {full_table_name} LIMIT {10}"
    headers = make_headers(token)
    payload = {"statement": sql_statement, "warehouse_id": warehouse_id}
    try:
        response = requests.post(f"{host}/api/2.0/sql/statements", headers=headers, json=payload)
        response.raise_for_status()
        statement_id = response.json().get("statement_id")
        if not statement_id: return None
        while True:
            status_resp = requests.get(f"{host}/api/2.0/sql/statements/{statement_id}", headers=headers).json()
            state = status_resp.get("status", {}).get("state")
            if state in ("SUCCEEDED", "FAILED", "CANCELED"): break
            time.sleep(1)
        if state != "SUCCEEDED": return None
        return status_resp.get("result", {}).get("data", [])
    except Exception as e:
        print(f"Error fetching sample rows: {e}")
        return None

# -----------------------------
# Prompt template
# -----------------------------
PROMPT_TEMPLATE = """
You are an expert metadata annotator for databases.
Input: table name, schema (columns + types), and sample rows.
Return ONLY a valid JSON array; each element should be:

{{
  "column_name": "<col>",
  "short_description": "<1-2 sentence explanation>",
  "tags": ["pii","email","id","phone","address","date","amount","name","financial","identifier","personal","general"],
  "sensitivity": "None|Low|Medium|High",
  "example_values": ["v1","v2"]
}}

Rules:
- Return only valid JSON (an array).
- Be conservative: only tag when reasonably sure.
- Use up to 5 example values from sample rows.
Table: {table}
Schema: {schema}
SampleRows: {rows}
"""

# -----------------------------
# AI call (Ollama local + Gemini)
# -----------------------------
def call_ai_server(table, schema, rows, model="llama3", gemini_key=None):
    prompt = PROMPT_TEMPLATE.format(table=table, schema=json.dumps(schema, indent=2), rows=json.dumps(rows, indent=2))
    try:
        if model == "llama3":
            body = {"model": "llama3", "prompt": prompt, "stream": False}
            resp = requests.post("http://localhost:11434/api/generate", json=body, timeout=120)
            resp.raise_for_status()
            txt = resp.json().get("response", "")
            # extract json array between brackets
            m = re.search(r"(\[.*\])", txt, re.S)
            if m:
                return json.loads(m.group(1))
            # fallback: try to parse whole as json
            try:
                return resp.json()
            except Exception:
                return []
        elif model == "gemini":
            if not gemini_key:
                raise ValueError("Gemini API key not provided")
            headers = {"Authorization": f"Bearer {gemini_key}", "Content-Type":"application/json"}
            # Using Google's Generative Language v1beta2 endpoint (text-bison-001) as an example
            body = {"prompt": {"text": prompt}, "max_output_tokens": 800}
            url = "https://api.generativeai.google.com/v1beta2/models/text-bison-001:generate"
            resp = requests.post(url, headers=headers, json=body, timeout=140)
            resp.raise_for_status()
            # get model text content
            # many Gemini variants return candidates -> content
            j = resp.json()
            txt = ""
            # defensively extract model content
            if isinstance(j.get("candidates"), list) and len(j["candidates"])>0:
                txt = j["candidates"][0].get("content","")
            else:
                # different response shape
                txt = json.dumps(j)
            m = re.search(r"(\[.*\])", txt, re.S)
            if m:
                return json.loads(m.group(1))
            # If model returned structured JSON under "output" or "content", try to find an array
            # Fallback: attempt to parse content as JSON:
            try:
                return json.loads(txt)
            except Exception:
                return []
    except Exception as e:
        print(f"AI server error ({model}):", e)
        return []

# -----------------------------
# Rule-based tag enhancer
# -----------------------------
def enhance_tags_with_rules(ai_output, schema, rows):
    """
    Ensure tags are present and reasonable. This augments or fixes AI output.
    Returns a list of columns with tags, sensitivities, descriptions.
    """
    enhanced = []
    # convenience: column sample values map (up to 5 examples)
    col_examples = {}
    if rows:
        # rows may be list of dicts or list of lists; attempt to map by column names
        if isinstance(rows[0], dict):
            for c in schema:
                name = c.get("name")
                examples = []
                for r in rows:
                    v = r.get(name)
                    if v is not None:
                        examples.append(str(v))
                    if len(examples) >= 5: break
                col_examples[name] = examples
        else:
            # assume rows are lists and schema columns in order
            col_names = [c.get("name") for c in schema]
            for idx, name in enumerate(col_names):
                ex = []
                for r in rows:
                    if idx < len(r): ex.append(str(r[idx]))
                    if len(ex) >= 5: break
                col_examples[name] = ex

    for col in ai_output:
        col_name = col.get("column_name") or ""
        name = col_name.lower()
        tags = set([t.lower() for t in col.get("tags", []) if isinstance(t, str)])
        desc = col.get("short_description", "")
        sens = col.get("sensitivity", "None") or "None"
        examples = col.get("example_values", []) or col_examples.get(col_name, [])

        # RULES based on name
        if re.search(r"\b(name|first_name|last_name|fullname|customer|student|patient)\b", name):
            tags.update(["personal","pii"])
        if re.search(r"\b(email|e-mail)\b", name):
            tags.update(["email","personal","pii"])
        if re.search(r"\b(phone|mobile|contact|tel)\b", name):
            tags.update(["phone","personal","pii"])
        if re.search(r"\b(address|addr|location|city|state|zip|postal)\b", name):
            tags.update(["address","personal","pii"])
        if re.search(r"\b(amount|price|cost|payment|balance|salary|total|amount_usd|amount_inr|transaction)\b", name):
            tags.update(["financial"])
        if re.search(r"\b(id\b|_id\b|id_|order|invoice|uid|uuid|code|ssn|card)\b", name):
            tags.update(["identifier"])
        if re.search(r"\b(date|dob|time|timestamp|year|month|day)\b", name):
            tags.update(["temporal"])
        if not tags:
            # try example values to detect
            for ex in examples:
                exs = str(ex).lower()
                if re.search(r"@.+\.", exs):
                    tags.update(["email","personal","pii"])
                elif re.fullmatch(r"\+?\d[\d\-\s]{6,}\d", exs):
                    tags.update(["phone","personal","pii"])
                elif re.fullmatch(r"\d+(\.\d+)?", exs) and "." in exs:
                    tags.update(["financial"])
                elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", exs) or re.search(r"\d{2}/\d{2}/\d{4}", exs):
                    tags.update(["temporal"])
            if not tags:
                tags.add("general")

        # Normalize tags (replace synonyms)
        normalized = set()
        for t in tags:
            if t in ("pii","personal"): normalized.add("pii")
            elif t in ("financial","amount","price"): normalized.add("financial")
            elif t in ("identifier","id","order"): normalized.add("identifier")
            elif t in ("email",): normalized.add("email")
            elif t in ("phone",): normalized.add("phone")
            elif t in ("address",): normalized.add("address")
            elif t in ("temporal","date"): normalized.add("date")
            else:
                normalized.add(t)

        # Sensitivity map
        if "pii" in normalized:
            sens = "High"
        elif "financial" in normalized:
            sens = "Medium"
        elif "identifier" in normalized:
            if sens == "None": sens = "Medium"
        else:
            if sens == "None": sens = "Low"

        enhanced.append({
            "column_name": col_name,
            "short_description": desc,
            "tags": sorted(list(normalized)),
            "sensitivity": sens,
            "example_values": examples
        })
    return enhanced

# -----------------------------
# NER / fallback tagging (kept for safety)
# -----------------------------
def extract_tags_from_rows(rows, column_name, meta_column_names=None):
    tags = set()
    email_re = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
    phone_re = re.compile(r"(\+?\d[\d\-\s]{6,}\d)")
    for r in rows:
        if isinstance(r, dict):
            val = r.get(column_name, "")
        elif isinstance(r, (list, tuple)):
            if meta_column_names and column_name in meta_column_names:
                idx = meta_column_names.index(column_name)
                val = r[idx] if idx < len(r) else ""
            else:
                val = " ".join([str(x) for x in r])
        else:
            val = str(r)
        sval = str(val)
        if not sval or sval.lower() in ("none","null","nan"): continue
        if email_re.search(sval): tags.update(["email","pii"])
        if phone_re.search(sval): tags.update(["phone","pii"])
        if re.fullmatch(r"\d{5,}", sval): tags.add("identifier")
        if nlp:
            try:
                doc = nlp(sval)
                for ent in doc.ents:
                    lab = ent.label_
                    if lab=="PERSON": tags.update(["personal","pii"])
                    elif lab in ("GPE","LOC"): tags.update(["address","pii"])
                    elif lab=="ORG": tags.add("org")
                    elif lab=="DATE": tags.add("date")
                    elif lab=="CARDINAL": tags.add("identifier")
            except Exception:
                pass
    return sorted(list(tags))

# -----------------------------
# Push to Unity Catalog (stub)
# -----------------------------
def push_column_comment(host, token, warehouse, full_table_name, column_name, comment_json):
    # Implement Databricks Unity Catalog comment / tagging API here.
    # This function currently returns a stubbed success result.
    return {"status": "ok", "column": column_name, "payload": comment_json}

# -----------------------------
# Streamlit UI (improved)
# -----------------------------
st.set_page_config(page_title="AutoMetaAI — Unity Catalog", layout="wide")
st.title("🚀 AutoMetaAI — Unity Catalog")

# Sidebar: credentials + model selection
with st.sidebar:
    st.markdown("### 🔑 Databricks Credentials")
    host_in = st.text_input("Databricks Host (e.g. https://adb-...)",
                            value=os.getenv("DATABRICKS_HOST",""))
    token_in = st.text_input("Databricks PAT", type="password", value=os.getenv("DATABRICKS_TOKEN",""))
    warehouse_in = st.text_input("SQL Warehouse ID", value=os.getenv("DATABRICKS_WAREHOUSE",""))
    st.markdown("---")
    st.markdown("### 🤖 AI Provider")
    ai_provider = st.radio("Choose AI Model", ["Ollama LLaMA3 (local)", "Gemini (cloud)"])
    st.markdown("### 🔐 Gemini API Key")
    # If you want the key prefilled (you provided it earlier), paste it here.
    # For safety prefer environment variable, but user asked to paste key, so we allow input.
    gemini_key = st.text_input("Paste Gemini API Key (or set env var GEMINI_API_KEY)", value=os.getenv("GEMINI_API_KEY","AIzaSyBbRmWr4ul0PkooEY7e-oh6BYb29NkePiw"), type="password")

# Step 1: Test connection
st.subheader("1️⃣ Test connection")
if st.button("Run connection tests"):
    if not host_in or not token_in:
        st.error("Enter Databricks host and PAT")
    else:
        res = test_connection_all(host_in, token_in, warehouse_in if warehouse_in else None)
        st.json(res)

# Step 2: Choose catalog/schema/table
st.header("2️⃣ Choose catalog / schema / table")
catalog_names = []
if host_in and token_in:
    try:
        catalog_names = [c.get("name") for c in list_catalogs(host_in, token_in)]
    except Exception as e:
        catalog_names = []
        st.warning(f"Could not list catalogs: {e}")
catalog_sel = st.selectbox("Catalog", [""] + (catalog_names or []))
schema_names = []
if catalog_sel:
    try:
        schema_names = [s.get("name") for s in list_schemas(host_in, token_in, catalog_sel)]
    except Exception as e:
        schema_names = []
        st.warning(f"Could not list schemas: {e}")
schema_sel = st.selectbox("Schema", [""] + (schema_names or []))
table_names = []
if schema_sel:
    try:
        table_names = [t.get("name") for t in list_tables(host_in, token_in, catalog_sel, schema_sel)]
    except Exception as e:
        table_names = []
        st.warning(f"Could not list tables: {e}")
table_sel = st.selectbox("Table", [""] + (table_names or []))

# Step 3: Fetch metadata & sample rows
st.header("3️⃣ Fetch metadata & sample rows")
if st.button("Fetch metadata & sample"):
    if not all([host_in, token_in, catalog_sel, schema_sel, table_sel]):
        st.error("Fill host, token, catalog, schema and table first.")
    else:
        try:
            full_table = f"{catalog_sel}.{schema_sel}.{table_sel}"
            st.session_state.meta = get_table_metadata(host_in, token_in, full_table)
            st.success("✅ Fetched table metadata")
            st.json(st.session_state.meta)
        except Exception as e:
            st.error(f"Failed to fetch metadata: {e}")
        if warehouse_in:
            try:
                st.session_state.sample = fetch_sample_rows(host_in, token_in, warehouse_in, f"{catalog_sel}.{schema_sel}.{table_sel}", limit=10)
                st.success("✅ Fetched sample rows")
                st.json(st.session_state.sample)
            except Exception as e:
                st.warning(f"Could not fetch sample rows: {e}")
        else:
            st.info("No warehouse provided; skipping sample rows.")

# Step 4: Enrich with AI + post-processing
st.header("4️⃣ Enrich with AI (model + tag enhancer)")
if st.button("Generate Metadata"):
    if "meta" not in st.session_state:
        st.error("Fetch metadata first (Step 3).")
    else:
        rows = st.session_state.get("sample", [])
        schema = st.session_state.meta.get("columns", [])
        table_full = f"{catalog_sel}.{schema_sel}.{table_sel}"
        model_key = "llama3" if ai_provider.startswith("Ollama") else "gemini"

        # call model
        ai_raw = call_ai_server(table_full, schema, rows, model=model_key, gemini_key=gemini_key if model_key=="gemini" else None)

        # If AI returned empty, try to create baseline ai_raw from schema
        if not ai_raw:
            st.warning("AI returned no results — falling back to rule-based baseline generation.")
            # baseline: use schema columns to populate entries
            baseline = []
            for c in schema:
                baseline.append({
                    "column_name": c.get("name"),
                    "short_description": f"Column {c.get('name')} from schema (baseline).",
                    "tags": [],
                    "sensitivity": "None",
                    "example_values": []
                })
            ai_raw = baseline

        # enhance tags with rules
        final_meta = enhance_tags_with_rules(ai_raw, schema, rows)
        st.session_state.ai_out = final_meta
        st.success(f"✅ Metadata generated & enhanced ({ai_provider})")
        st.json(final_meta)

# Step 5: Review & Push
st.header("5️⃣ Review, edit & push to Unity Catalog")
if "ai_out" in st.session_state and st.session_state.ai_out:
    edits = []
    for c in st.session_state.ai_out:
        st.subheader(c.get("column_name"))
        desc = st.text_area(f"Description — {c.get('column_name')}", c.get("short_description",""))
        tags_str = ",".join(c.get("tags",[]))
        tags_in = st.text_input(f"Tags — {c.get('column_name')}", tags_str, key=f"tags_{c.get('column_name')}")
        sens_default = c.get("sensitivity","None") or "None"
        sens = st.selectbox(f"Sensitivity — {c.get('column_name')}", ["None","Low","Medium","High"], index=["None","Low","Medium","High"].index(sens_default), key=f"sens_{c.get('column_name')}")
        edits.append({"column_name":c.get("column_name"),"short_description":desc,"tags":[t.strip() for t in tags_in.split(",") if t.strip()],"sensitivity":sens})
    if st.button("Push to Unity Catalog"):
        results = []
        for e in edits:
            comment_json = {"description": e["short_description"],"tags": e["tags"],"sensitivity": e["sensitivity"]}
            results.append(push_column_comment(host_in, token_in, warehouse_in, f"{catalog_sel}.{schema_sel}.{table_sel}", e["column_name"], json.dumps(comment_json)))
        st.json(results)
else:
    st.info("No AI enrichment results yet. Use Step 4.")

# Optional: Show sample expected consolidated JSON for copying
st.markdown("---")
if st.button("Show example consolidated JSON output from current ai_out"):
    if "ai_out" not in st.session_state:
        st.info("No AI output yet.")
    else:
        table_description = st.text_input("Optional: Table description", value=f"{table_sel} metadata" if table_sel else "Table metadata")
        consolidated = {"table_description": table_description, "columns": {}}
        for c in st.session_state.ai_out:
            consolidated["columns"][c["column_name"]] = {
                "description": c["short_description"],
                "tags": c["tags"],
                "classification": c["sensitivity"]
            }
        st.json(consolidated)
