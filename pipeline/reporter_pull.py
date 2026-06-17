#!/usr/bin/env python3
"""
Nephrology K-Grant Hub — data pipeline.

Queries the public NIH RePORTER API v2 for active career-development (K) awards
related to nephrology, classifies each by subarea and by clinical vs. non-clinical
research, computes institutional / geographic aggregates, and writes data.json for
the webpage.

Run:  python reporter_pull.py            # writes ../data.json
      python reporter_pull.py --years 2023 2024 2025 2026

NOTE: This must run server-side (CI runner, your laptop) — the RePORTER API does
not allow direct in-browser calls (no CORS). The sandbox used to author this repo
cannot reach the API; run it where outbound HTTPS to api.reporter.nih.gov is allowed.
"""
import argparse, json, os, sys, time, urllib.request, urllib.error

API = "https://api.reporter.nih.gov/v2/projects/search"

# K-series career-development mechanisms (NIH + K-equivalent K-codes in RePORTER)
ACTIVITY_CODES = ["K01","K02","K08","K12","K22","K23","K24","K25","K38","K43","K76","K99"]

# Broad recall query; precision is enforced afterwards by NEPH_GATE.
SEARCH_TEXT = ("kidney renal nephrology nephrologist dialysis hemodialysis glomerular "
               "glomerulonephritis podocyte nephron nephropathy CKD ESKD ESRD transplant "
               "proteinuria albuminuria tubular nephrotic")

# ---- nephrology study-population gate ------------------------------------------
# An award is nephrology if kidney disease is the study FOCUS (kidney-disease term
# in the title) OR the study POPULATION (an explicit kidney-disease cohort named in
# the abstract). This keeps comorbidity studies that enroll kidney-disease patients
# (anemia/cardiovascular/metabolic studies in CKD/dialysis/transplant cohorts) while
# dropping incidental kidney mentions and pure kidney-cancer/urology.
def _strip(s):  # avoid "adrenal" matching "renal"
    return (s or "").lower().replace("adrenal", "___").replace("suprarenal", "___")

TITLE_FOCUS = ["kidney","renal","nephro","nephritis","nephropathy","dialysis","hemodialysis",
               "haemodialysis","glomerul","podocyte","nephron","ckd","eskd","esrd","tubular",
               "albuminuria","proteinuria","nephrotic","apol1","fsgs","polycystic kidney",
               "end-stage renal","uremi","dialysate","kidney stone","nephrolithiasis"]
POP = ["patients on dialysis","on hemodialysis","on maintenance dialysis","maintenance hemodialysis",
       "dialysis patients","hemodialysis patients","peritoneal dialysis patients","patients receiving dialysis",
       "patients with chronic kidney disease","patients with ckd","adults with ckd","adults with chronic kidney disease",
       "children with ckd","children with chronic kidney disease","youth with chronic kidney","patients with kidney failure",
       "patients with esrd","patients with eskd","patients with end-stage renal","patients with end-stage kidney",
       "kidney transplant recipients","renal transplant recipients","kidney transplant candidates","living kidney donor",
       "patients with kidney disease","individuals with ckd","persons with ckd","people with kidney disease",
       "glomerular disease","predialysis ckd","advanced ckd","incident dialysis","kidney disease patients"]
RCC = ["renal cell","kidney cancer","nephroblastoma","wilms","clear cell renal","renal tumor","renal mass","urothelial"]
DCTX = ["ckd","eskd","esrd","dialysis","kidney disease","kidney failure","renal failure","glomerul",
        "nephritis","nephropathy","nephrotic","proteinuria","kidney transplant","renal transplant",
        "podocyte","tubular","kidney injury","cast nephropathy"]

def is_nephrology(title, terms, abstract, loose=False):
    title_s, terms_s, ab_s = _strip(title), _strip(terms), _strip(abstract)
    if loose:  # maximum recall: any kidney term anywhere
        if not any(k in (title_s + " " + terms_s + " " + ab_s) for k in TITLE_FOCUS):
            return False
    else:
        title_focus = any(k in title_s for k in TITLE_FOCUS)
        pop_hit = any(p in ab_s for p in POP) or any(p in title_s for p in POP)
        if not (title_focus or pop_hit):
            return False
    allt = title_s + " " + terms_s + " " + ab_s
    if any(k in title_s for k in RCC) and not any(k in allt for k in DCTX):
        return False  # pure kidney-cancer with no kidney-disease context
    return True

def extract_foa(rec):
    """Funding-opportunity (FOA/NOFO) id, if available."""
    import re as _re
    for key in ("opportunity_number", "full_foa"):
        v = rec.get(key)
        if v:
            m = _re.search(r"(PA|PAR|RFA|PAS)-[A-Z]{2}-\d{2}-\d{2,4}", str(v), _re.I)
            return m.group(0) if m else str(v)
    return ""

# ---- subarea classifier --------------------------------------------------------
def classify_subarea(text):
    t = text.lower()
    has = lambda *a: any(k in t for k in a)
    if has("transplant") and has("kidney","renal","allograft"): return "Transplant"
    if has("dialysis","hemodialysis","peritoneal dialysis","esrd","eskd","end-stage",
           "end stage renal","vascular access","arteriovenous fistula"): return "ESKD / Dialysis"
    if has("glomerul","podocyte","fsgs","nephrotic","iga nephropathy","membranous",
           "lupus nephritis","crescentic"): return "Glomerular disease"
    if has("acute kidney injury","aki ","ischemia-reperfusion","ischemia reperfusion",
           "acute tubular","acute renal failure"): return "Acute kidney injury"
    if has("polycystic","pkd","alport","ciliopath","apol1","monogenic kidney","cystic kidney",
           "hereditary kidney","congenital anomalies of the kidney"): return "PKD / Genetic"
    if has("nephrolithiasis","kidney stone","oxalate","electrolyte","acid-base","hypokalem",
           "hyperkalem","phosphate","mineral metabolism","magnesium"): return "Stones / Electrolytes"
    if has("hypertension","blood pressure","aldosterone","renin","salt-sensitiv","sodium intake"):
        return "Hypertension"
    if has("pediatric","paediatric","children","neonat","childhood"): return "Pediatric nephrology"
    if has("disparit","health services","outcomes research","epidemiolog","implementation",
           "access to care","cost-effective","quality of care","patient-reported","equity",
           "social determinants"): return "Health services / Epi"
    if has("chronic kidney disease","ckd","egfr decline","diabetic kidney","diabetic nephropathy",
           "fibrosis","tubulointerstitial"): return "CKD progression"
    return "General nephrology"

# ---- clinical vs non-clinical classifier --------------------------------------
CLIN = ["patient","clinical trial","randomized","randomised","cohort","participants",
        "human subjects","observational","enroll","recruit","ehr","electronic health record",
        "registry","cross-sectional","clinical","adults with","individuals with"]
TRIAL = ["randomized","randomised","clinical trial","rct","placebo","double-blind","phase i",
         "phase ii","trial of","pilot trial","feasibility trial"]
OBS = ["cohort","observational","registry","cross-sectional","prospective study","retrospective",
       "epidemiolog","case-control"]
BASIC = ["mouse","mice","murine","in vitro","cell line","cultured cells","molecular mechanism",
         "zebrafish"," rat ","rats","knockout","signaling pathway","animal model","organoid",
         "single-cell","transcriptom","mechanistic","proteom"]
def classify_clinical(text, ac):
    t = text.lower()
    c = sum(k in t for k in CLIN); b = sum(k in t for k in BASIC)
    if ac in ("K23","K24"): c += 1
    if ac in ("K08","K99","K01"): b += 0.5
    is_clin = c >= b and c > 0
    if not is_clin and b == 0 and c == 0:
        is_clin = (ac == "K23")
    if is_clin:
        tr = sum(k in t for k in TRIAL); ob = sum(k in t for k in OBS)
        return "Clinical trial" if tr > ob else ("Observational / clinical" if ob > 0 else "Clinical (other)")
    return "Non-clinical / basic"

# ---- API helper with retry/back-off -------------------------------------------
def post(body, tries=5):
    data = json.dumps(body).encode()
    for i in range(tries):
        try:
            req = urllib.request.Request(API, data=data,
                  headers={"Content-Type":"application/json","Accept":"application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            wait = 2 * (i + 1)
            sys.stderr.write(f"  API error ({e}); retry in {wait}s\n")
            time.sleep(wait)
    raise RuntimeError("RePORTER API failed after retries")

def fetch_all(years):
    """Pull every matching K award across the requested fiscal years."""
    fields = ["ApplId","ProjectNum","CoreProjectNum","ActivityCode","FiscalYear","AwardAmount",
              "Organization","PrincipalInvestigators","ProgramOfficers","AgencyIcAdmin",
              "ProjectTitle","AbstractText","Terms","PhrText","ProjectStartDate","ProjectEndDate",
              "OpportunityNumber","FullFoa"]
    out, offset, LIMIT = [], 0, 500
    while True:
        body = {"criteria":{"activity_codes":ACTIVITY_CODES,"fiscal_years":years,
                 "advanced_text_search":{"operator":"or",
                   "search_field":"projecttitle,abstracttext,terms","search_text":SEARCH_TEXT}},
                "include_fields":fields,"offset":offset,"limit":LIMIT,
                "sort_field":"fiscal_year","sort_order":"desc"}
        res = post(body)
        rows = res.get("results", [])
        out.extend(rows)
        total = res.get("meta", {}).get("total", len(out))
        sys.stderr.write(f"  fetched {len(out)}/{total}\n")
        offset += len(rows)
        if not rows or offset >= total:
            break
        time.sleep(0.5)
    return out

def keep_latest_per_core(rows):
    """One row per award (latest fiscal year / budget)."""
    best = {}
    for r in rows:
        key = r.get("core_project_num") or r.get("project_num")
        if key not in best or (r.get("fiscal_year") or 0) > (best[key].get("fiscal_year") or 0):
            best[key] = r
    return list(best.values())

def transform(rows, loose=False):
    grants = []
    for r in rows:
        org = r.get("organization") or {}
        title = r.get("project_title") or ""
        terms = r.get("terms") or ""
        abstract = " ".join([r.get("abstract_text") or "", r.get("phr_text") or ""])
        if not is_nephrology(title, terms, abstract, loose=loose):
            continue
        full_text = " ".join([title, abstract, terms])
        pis = [p.get("full_name") for p in (r.get("principal_investigators") or []) if p.get("full_name")]
        ic = (r.get("agency_ic_admin") or {})
        grants.append({
            "num": r.get("project_num"), "core": r.get("core_project_num"),
            "ac": r.get("activity_code"), "fy": r.get("fiscal_year"),
            "amt": r.get("award_amount") or 0,
            "org": org.get("org_name") or "", "st": org.get("org_state") or "",
            "city": org.get("org_city") or "",
            "pi": (pis[0] if pis else ""), "pis": pis,
            "po": [p.get("full_name") for p in (r.get("program_officers") or []) if p.get("full_name")],
            "ic": ic.get("abbreviation") or ic.get("code") or "",
            "t": r.get("project_title") or "",
            "sub": classify_subarea(full_text), "cl": classify_clinical(full_text, r.get("activity_code")),
            "id": r.get("appl_id"), "foa": extract_foa(r),
        })
    return grants

def aggregate(grants):
    def tally(key):
        m = {}
        for g in grants:
            k = g.get(key) or "?"
            m[k] = m.get(k, 0) + 1
        return dict(sorted(m.items(), key=lambda x: -x[1]))
    is_clin = lambda g: "Non-clinical" not in g["cl"]
    inst = {}
    for g in grants:
        o = g["org"] or "?"
        d = inst.setdefault(o, {"name": o, "state": g["st"], "total": 0, "clinical": 0, "nonclinical": 0})
        d["total"] += 1
        d["clinical" if is_clin(g) else "nonclinical"] += 1
    institutions = sorted(inst.values(), key=lambda x: (-x["total"], x["name"]))  # all institutions
    states = {}
    for g in grants:
        if g["st"]:
            states[g["st"]] = states.get(g["st"], 0) + 1
    return {
        "stats": {
            "by_mechanism": tally("ac"), "by_subarea": tally("sub"),
            "by_clinical": tally("cl"), "by_institute": tally("ic"),
            "by_fiscal_year": tally("fy"),
        },
        "institutions": institutions,
        "states": dict(sorted(states.items(), key=lambda x: -x[1])),
    }

def default_years():
    """Active awards span roughly the last two fiscal years plus the next one
    (NIH assigns future FYs). Computed at run time so the hub stays current."""
    y = time.localtime().tm_year
    return [y - 2, y - 1, y, y + 1]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", nargs="+", type=int, default=None,
                    help="Fiscal years to pull (default: rolling window around the current year).")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "data.json"))
    ap.add_argument("--loose", action="store_true",
                    help="Maximum-recall abstract-level match (includes onco-nephrology/urology). "
                         "Default is the kidney study-population filter.")
    args = ap.parse_args()
    if not args.years:
        args.years = default_years()
    sys.stderr.write(f"Fiscal years: {args.years}\n")

    sys.stderr.write("Querying NIH RePORTER…\n")
    raw = fetch_all(args.years)
    raw = keep_latest_per_core(raw)
    grants = transform(raw, loose=args.loose)
    grants.sort(key=lambda g: -(g["amt"] or 0))
    agg = aggregate(grants)
    total_amt = sum(g["amt"] or 0 for g in grants)

    payload = {
        "meta": {
            "generated": time.strftime("%Y-%m-%d"),
            "source": ("NIH RePORTER API v2 (active K-series, " +
                       ("looser abstract-level" if args.loose else "kidney study-population") +
                       " filter)"),
            "snapshot_note": ("Active career-development (K) awards where kidney disease is the study focus "
                              "or study population (comorbidity studies in CKD/dialysis/transplant cohorts are "
                              "included; incidental kidney mentions and pure kidney-cancer/urology are excluded). "
                              "Subarea and clinical/non-clinical labels are first-pass algorithmic classifications."),
            "application_pdf_note": ("NIH does not publish full grant applications publicly (FOIA-only); "
                                     "each award links to its NIH RePORTER record with the public abstract."),
            "universe_total_active": len(grants),
            "n_institutions": len(agg["institutions"]),
            "total_current_year_award_usd": total_amt,
        },
        "stats": agg["stats"],
        "institutions": agg["institutions"],
        "states": agg["states"],
        "grants": grants,
    }
    out = os.path.abspath(args.out)
    with open(out, "w") as f:
        json.dump(payload, f, indent=1)
    sys.stderr.write(f"Wrote {len(grants)} nephrology K awards to {out}\n")

if __name__ == "__main__":
    main()
