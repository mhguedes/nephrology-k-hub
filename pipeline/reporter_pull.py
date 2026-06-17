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
# Onco- and urology-primary signals. A title hit here drops the award UNLESS the study also has an
# explicit kidney-disease context (DCTX) — that keeps e.g. AKI-in-cancer or CKD-in-transplant work
# while removing renal-cell/renal carcinoma oncology and surgical-urology (renal colic, lithotripsy…).
ONCO_URO = ["renal cell","renal carcinoma","renal cancer","kidney cancer","carcinoma","oncolog",
            "nephroblastoma","wilms","clear cell renal","renal tumor","renal mass","urothelial",
            "renal colic","urolog","ureteroscop","lithotripsy","nephrolithotomy","cystoscop",
            "prostate","bladder cancer"]
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
    if any(k in title_s for k in ONCO_URO) and not any(k in allt for k in DCTX):
        return False  # onco-/urology-primary with no kidney-disease context
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

def extract_study_section(rec):
    """Human-readable scientific review group (study section) name, if available."""
    fss = rec.get("full_study_section")
    if isinstance(fss, dict):
        name = (fss.get("name") or "").strip()
        if name:
            return name
        code = (fss.get("srg_code") or fss.get("group_code") or "").strip()
        if code:
            return code
    elif isinstance(fss, str) and fss.strip():
        return fss.strip()
    for key in ("study_section_name", "study_section"):
        v = rec.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""

def start_year(rec):
    """Project start fiscal/calendar year as int, from the ISO start date."""
    d = str(rec.get("project_start_date") or "")
    try:
        y = int(d[:4])
        return y if 1985 <= y <= 2100 else None
    except (ValueError, TypeError):
        return None

# ---- subarea classifier --------------------------------------------------------
# Subarea = the kidney-disease ENTITY or PATIENT POPULATION a study centers on, judged
# primarily from the TITLE (the study's stated subject). This prevents an incidental
# outcome mention ("...at risk for ESRD") from hijacking the label. Disease entities are
# tested before ESKD / Dialysis, which is reserved for studies *of* dialysis/ESKD or that
# enroll patients *already on* dialysis / with ESKD — not studies that list them as outcomes.
GLOM  = ["glomerul","podocyte","fsgs","focal segmental","nephrotic","iga nephropathy","membranous",
         "lupus nephritis","crescentic","anca","c3 glomerulopathy","minimal change disease","mpgn","nell1","pla2r"]
TX    = ["transplant","allograft","kidney donor","living donor"]
AKI   = ["acute kidney injury","acute renal failure","acute tubular","ischemia-reperfusion",
         "ischemia reperfusion","ischemic kidney","cardiorenal","cardio-renal"]
PKD   = ["polycystic","pkd","alport","ciliopath","cystic kidney","monogenic","hereditary kidney",
         "congenital anomalies of the kidney","cakut","apol1"]
STONE = ["nephrolithiasis","kidney stone","renal stone","urolithiasis","oxalate","hypercalciuria",
         "electrolyte","acid-base","hypokalem","hyperkalem","hyperphosphat","mineral metabolism",
         "magnesium","potassium secretion","calcium oxalate"]
# Dialysis/ESKD as a POPULATION or treatment (the study's subject) — these rarely appear as a mere
# outcome, so they identify a genuine dialysis/ESKD study.
ESKD_POP = ["dialysis patient","on dialysis","on hemodialysis","hemodialysis","haemodialysis",
            "peritoneal dialysis","dialysate","dialysis care","dialysis unit","dialysis access",
            "maintenance dialysis","maintenance hemodialysis","incident dialysis","prevalent dialysis",
            "vascular access","arteriovenous fistula","renal replacement therapy"]
# Dialysis/ESKD as a bare term that is often an OUTCOME ("progression to ESRD", "predicting dialysis").
# Only assigned when no disease entity or CKD was named first.
ESKD_OUT = ["dialysis","esrd","eskd","end-stage renal","end stage renal","end-stage kidney",
            "end stage kidney","kidney failure","renal failure"]
HTN   = ["hypertension","blood pressure","aldosterone","renin-angiotensin","salt-sensitiv","sodium intake","preeclampsia"]
PEDS  = ["pediatric","paediatric","childhood","neonat","preterm","infant","youth","adolescen","children"]
HSR   = ["disparit","health services","outcomes research","epidemiolog","implementation","access to care",
         "cost-effective","quality of care","patient-reported","health equity","social determinants","telemedicine","education"]
CKD   = ["chronic kidney disease","ckd","egfr","diabetic kidney","diabetic nephropathy","fibrosis",
         "tubulointerstitial","albuminuria","proteinuria","kidney function decline"]
# Population phrases proving the study enrolls a specific kidney population (abstract fallback).
POP_TX   = ["kidney transplant recipients","renal transplant recipients","kidney transplant candidates",
            "living kidney donor","transplant recipients","transplant waitlist","post-transplant","posttransplant"]
POP_ESKD = ["on dialysis","on hemodialysis","on maintenance dialysis","maintenance hemodialysis",
            "dialysis patients","hemodialysis patients","peritoneal dialysis patients","patients receiving dialysis",
            "patients with esrd","patients with eskd","patients with end-stage","incident dialysis",
            "prevalent dialysis","undergoing dialysis","receiving hemodialysis"]
POP_CKD  = ["patients with chronic kidney disease","patients with ckd","adults with ckd","children with ckd",
            "persons with ckd","individuals with ckd","predialysis ckd","advanced ckd","ckd cohort"]

def _hits(text, kws):
    return any(k in text for k in kws)

def classify_subarea(title, text=None):
    ti = _strip(title)
    tx = _strip(text) if text is not None else ti
    # 1) TITLE-first: the study's stated subject wins. Disease entities are tested before
    #    ESKD / Dialysis so an entity (e.g. membranous nephropathy) is not relabeled by an
    #    incidental dialysis/ESRD outcome mentioned elsewhere.
    if _hits(ti, TX):       return "Transplant"
    if _hits(ti, GLOM):     return "Glomerular disease"
    if _hits(ti, PKD):      return "PKD / Genetic"
    if _hits(ti, AKI):      return "Acute kidney injury"
    if _hits(ti, STONE):    return "Stones / Electrolytes"
    if _hits(ti, ESKD_POP): return "ESKD / Dialysis"  # enrolled dialysis/ESKD population or treatment
    if _hits(ti, HTN):      return "Hypertension"
    if _hits(ti, PEDS):     return "Pediatric nephrology"
    if _hits(ti, HSR):      return "Health services / Epi"
    if _hits(ti, CKD):      return "CKD progression"   # named CKD entity beats a bare ESRD/dialysis outcome
    if _hits(ti, ESKD_OUT): return "ESKD / Dialysis"  # dialysis/ESKD is the title subject (no entity named)
    # 2) POPULATION fallback: a generic title — classify by the population actually enrolled.
    if _hits(tx, POP_TX):   return "Transplant"
    if _hits(tx, POP_ESKD): return "ESKD / Dialysis"  # study enrolls patients already on dialysis/ESKD
    if _hits(tx, POP_CKD):  return "CKD progression"
    # 3) ABSTRACT entity fallback. NOTE: generic ESKD terms are deliberately NOT consulted here,
    #    so a dialysis/ESRD mention in the body (as an outcome) cannot create an ESKD label.
    if _hits(tx, TX):    return "Transplant"
    if _hits(tx, GLOM):  return "Glomerular disease"
    if _hits(tx, PKD):   return "PKD / Genetic"
    if _hits(tx, AKI):   return "Acute kidney injury"
    if _hits(tx, STONE): return "Stones / Electrolytes"
    if _hits(tx, HTN):   return "Hypertension"
    if _hits(tx, PEDS):  return "Pediatric nephrology"
    if _hits(tx, HSR):   return "Health services / Epi"
    if _hits(tx, CKD):   return "CKD progression"
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

PUB_API = "https://api.reporter.nih.gov/v2/publications/search"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"

def _post_json(url, body, tries=4, timeout=60):
    data = json.dumps(body).encode()
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data,
                  headers={"Content-Type": "application/json", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception:
            time.sleep(1.5 * (i + 1))
    return {}

def _surname(name):
    name = (name or "").strip()
    return (name.split(",")[0] if "," in name else name.split(" ")[0]).lower()

def enrich(grants):
    """Attach publication counts to each grant and return an inferred-mentor list.
    Mentor inference: most-common senior (last) author across a scholar's K-supported
    publications, excluding the scholar; aggregated across scholars. Best-effort — any
    network/parse failure leaves grants intact and returns []. Never raises."""
    from collections import Counter
    try:
        by_core = {g["core"]: g for g in grants if g.get("core")}
        cores = list(by_core.keys())
        pmids_by_core = {}
        for i in range(0, len(cores), 50):
            res = _post_json(PUB_API, {"criteria": {"core_project_nums": cores[i:i+50]},
                                       "include_fields": ["CoreProject", "Pmid"],
                                       "offset": 0, "limit": 500})
            for r in (res.get("results") or []):
                c = r.get("coreproject") or r.get("core_project") or r.get("core_project_num")
                pm = r.get("pmid") or r.get("pm_id")
                if c and pm:
                    pmids_by_core.setdefault(c, []).append(str(pm))
            time.sleep(0.34)
        for g in grants:
            g["pubs"] = len(pmids_by_core.get(g.get("core"), []))
        all_pmids = sorted({pm for v in pmids_by_core.values() for pm in v})
        last_author = {}
        for i in range(0, len(all_pmids), 200):
            batch = all_pmids[i:i+200]
            try:
                url = EUTILS + "?db=pubmed&retmode=json&id=" + ",".join(batch)
                with urllib.request.urlopen(url, timeout=45) as r:
                    js = json.loads(r.read().decode())
                res = js.get("result", {})
                for pm in res.get("uids", []):
                    auths = res.get(pm, {}).get("authors") or []
                    names = [a.get("name") for a in auths if a.get("name")]
                    if names:
                        last_author[pm] = names[-1]
            except Exception:
                pass
            time.sleep(0.4)
        mentors = {}
        for g in grants:
            la = [last_author.get(pm) for pm in pmids_by_core.get(g.get("core"), []) if last_author.get(pm)]
            la = [a for a in la if _surname(a) and _surname(a) != _surname(g.get("pi", ""))]
            if not la:
                continue
            top = Counter(la).most_common(1)[0][0]
            m = mentors.setdefault(top, {"scholars": set(), "subs": Counter()})
            m["scholars"].add(g.get("pi"))
            m["subs"][g.get("sub")] += 1
        out = [{"name": n, "count": len(v["scholars"]), "subs": v["subs"].most_common(1)[0][0]}
               for n, v in mentors.items() if len(v["scholars"]) >= 2]
        out.sort(key=lambda m: -m["count"])
        sys.stderr.write(f"  enrichment: {sum(1 for g in grants if g.get('pubs'))} awards with pubs, "
                         f"{len(out)} inferred mentors\n")
        return out[:25]
    except Exception as e:
        sys.stderr.write(f"  enrichment skipped ({e})\n")
        return []

def fetch_all(years):
    """Pull every matching K award across the requested fiscal years."""
    fields = ["ApplId","ProjectNum","CoreProjectNum","ActivityCode","FiscalYear","AwardAmount",
              "Organization","PrincipalInvestigators","ProgramOfficers","AgencyIcAdmin",
              "ProjectTitle","AbstractText","Terms","PhrText","ProjectStartDate","ProjectEndDate",
              "OpportunityNumber","FullFoa","FullStudySection"]
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
            "sub": classify_subarea(title, full_text), "cl": classify_clinical(full_text, r.get("activity_code")),
            "id": r.get("appl_id"), "foa": extract_foa(r),
            "ss": extract_study_section(r), "sy": start_year(r),
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

    out = os.path.abspath(args.out)

    # Read the PREVIOUS snapshot (the data.json about to be overwritten) so we can flag
    # awards that are new this refresh. Identity is the stable core project number.
    prev_cores, prev_generated = set(), None
    try:
        if os.path.exists(out):
            with open(out) as f:
                prev = json.load(f)
            prev_cores = {g.get("core") for g in (prev.get("grants") or []) if g.get("core")}
            prev_generated = (prev.get("meta") or {}).get("generated")
    except Exception as e:
        sys.stderr.write(f"  (no usable previous snapshot: {e})\n")

    sys.stderr.write("Querying NIH RePORTER…\n")
    raw = fetch_all(args.years)
    raw = keep_latest_per_core(raw)
    grants = transform(raw, loose=args.loose)
    grants.sort(key=lambda g: -(g["amt"] or 0))
    # Mark awards whose core was absent from the prior snapshot (skip on first-ever run
    # so we don't flag the entire dataset as "new").
    n_new = 0
    if prev_cores:
        for g in grants:
            if g.get("core") and g["core"] not in prev_cores:
                g["new"] = True
                n_new += 1
    sys.stderr.write(f"  new-this-refresh awards: {n_new}\n")
    mentors = enrich(grants)   # attaches g["pubs"]; returns inferred mentors (best-effort)
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
            "prev_generated": prev_generated,
            "n_new_this_refresh": n_new,
        },
        "stats": agg["stats"],
        "institutions": agg["institutions"],
        "states": agg["states"],
        "mentors": mentors,
        "grants": grants,
    }
    with open(out, "w") as f:
        json.dump(payload, f, indent=1)
    sys.stderr.write(f"Wrote {len(grants)} nephrology K awards to {out}\n")

if __name__ == "__main__":
    main()
