#!/usr/bin/env python3
"""
STEP 3 of 3 -- Assemble checkpoint CSVs into a formatted Excel workbook.

READS: universe.csv, data_quarterly.csv, data_monthly.csv, data_snapshot.csv
WRITES: Fortune500_SP500_NASDAQ_Financials_5Y.xlsx

FIXED BY AUDIT (2026-08-05):
  - removed dead stub code that silently overwrote real functions with placeholders
  - added missing constants (ANNUAL_ADDITIVE, ANNUAL_MARGINS, _FMT_KEYS)
  - completed main() body to actually write Excel workbook with all sheets
"""

import os, sys, time
import pandas as pd
from collections import defaultdict
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl import Workbook

OUT = "Fortune500_SP500_NASDAQ_Financials_5Y.xlsx"
HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=10)
MONEY = "#,##0.##"; MONEY_SCALE = 1e6; PCT_FMT = "0.00"; NUM_FMT = "#,##0.00"

FORMATS = {
    "revenue":MONEY,"gross_profit":MONEY,"ebit":MONEY,"ebitda":MONEY,
    "net_income":MONEY,"market_cap":MONEY,"market_cap_est":MONEY,
    "gross_margin_pct":PCT_FMT,"ebit_margin_pct":PCT_FMT,
    "ebitda_margin_pct":PCT_FMT,"net_margin_pct":PCT_FMT,
    "profit_margin_pct":PCT_FMT,"operating_margin_pct":PCT_FMT,
    "dividend_yield":PCT_FMT,"payout_ratio":PCT_FMT,
    "open":NUM_FMT,"high":NUM_FMT,"low":NUM_FMT,"close":NUM_FMT,
    "adj_close":NUM_FMT,"price":NUM_FMT,"eps_diluted":NUM_FMT,
    "eps_trailing":NUM_FMT,"eps_forward":NUM_FMT,"pe_trailing":NUM_FMT,
    "pe_forward":NUM_FMT,"pe_trailing_est":NUM_FMT,"beta":NUM_FMT,
    "ev_to_ebitda":NUM_FMT,"price_to_book":NUM_FMT,
    "volume":"#,##0","shares_outstanding":"#,##0",
}

for _b, _f in list(FORMATS.items()): FORMATS[f"Q_{_b}"] = FORMATS[f"M_{_b}"] = _f
_FMT_KEYS = tuple(FORMATS.keys())
ANNUAL_ADDITIVE = ("revenue","gross_profit","ebit","ebitda","net_income")
ANNUAL_MARGINS = [("eps_diluted","EPS")]
Q_MEASURES = ["revenue","gross_profit","ebit","ebitda","net_income","eps_diluted",
              "gross_margin_pct","ebit_margin_pct","ebitda_margin_pct","net_margin_pct","q4_derived"]
M_MEASURES = ["close","adj_close","volume","market_cap_est","pe_trailing_est"]

def fmt_for(col):
    col = str(col)
    if col in FORMATS: return FORMATS[col]
    for k in _FMT_KEYS:
        if col.startswith(k + "_"): return FORMATS[k]
    return None

def apply_fmt(cell, col_name):
    nf = fmt_for(str(col_name))
    if nf and isinstance(cell.value, (int,float)) and not pd.isna(cell.value):
        cell.number_format = nf

def build_annual(q):
    d = q.copy(); reported = pd.DataFrame()
    if "period_type" in d.columns:
        fy_rows = d[d["period_type"].astype(str).str.upper()=="FY"].copy()
        d = d[d["period_type"].astype(str).str.upper()!="FY"]
        if not fy_rows.empty:
            try:
                ry_s = pd.to_numeric(fy_rows["fiscal_quarter"].str.extract(r"FY(\d{4})")[0], errors="coerce")
                good_mask = ry_s.notna()
                reported = fy_rows[good_mask].drop(columns=["period_type"], errors="ignore").copy()
                reported["fy"] = ry_s[good_mask].astype(int).astype("Int64")
            except AttributeError: pass
    try:
        d["fy"] = pd.to_numeric(d["fiscal_quarter"].str.extract(r"(?:FY)?(\d{4})"), errors="coerce").astype("Int64")
        d["qn"] = pd.to_numeric(d["fiscal_quarter"].str.extract(r"-Q(\d)"), errors="coerce").astype("Int64")
    except Exception: return pd.DataFrame()
    d = d.dropna(subset=["fy","qn"]).copy(); d["fy"]=d["fy"].astype(int); d["qn"]=d["qn"].astype(int)
    d = d[d["qn"].between(1,4)]
    totals, projections, last_fy = {}, {}, {}
    for meas in ANNUAL_ADDITIVE + [n for n,_ in ANNUAL_MARGINS]:
        if meas not in d.columns: continue
        try: piv = (d.pivot_table(index=["ticker","fy"],columns="qn",values=meas,aggfunc="first").reindex(columns=[1,2,3,4]))
        except Exception as e: print(f"Error pvt {meas}: {e}"); continue
        summed = piv.sum(axis=1,min_count=1).where(piv.notna().sum(axis=1)==4).unstack("fy")
        if not reported.empty and meas in reported.columns:
            rep = reported.pivot_table(index="ticker",columns="fy",values=meas,aggfunc="first")
            summed = summed.reindex(index=summed.index.union(rep.index),columns=summed.columns.union(rep.columns))
            summed = rep.reindex_like(summed).combine_first(summed)
        totals[meas] = summed
        proj = {}
        for (tk,fy_idx), rd in piv.iterrows():
            if last_fy.get(tk,-1)>=fy_idx: continue
            pk=(tk,fy_idx-1); prior=piv.loc[pk] if pk in piv.index else None
            if prior is None or prior.dropna().sum()<3: continue
            have=rd.dropna(); cs=have.sum()
            if cs==0 and "revenue" in meas: continue
            pt=prior.sum()
            if pt<=0 or pt/MONEY_SCALE==0: continue
            sh=have.sum()/(pt/MONEY_SCALE)
            if not(0.05<=sh<=0.95): continue
            last_fy[tk]=fy_idx; proj[(tk,fy_idx)]=cs/sh
        projections[meas] = pd.DataFrame(proj).T if proj else pd.DataFrame()
    blocks, seen=[],set()
    for meas in ANNUAL_ADDITIVE+[n for n,_ in ANNUAL_MARGINS]:
        t=totals.get(meas)
        if t is not None and not t.empty:
            rm={}
            for oc in list(t.columns):
                cn=f"{meas}_FY{int(oc)}"
                if cn not in seen: seen.add(cn); blocks.append(t)
                else: a=cn+"_"+str(oc); seen.add(a); rm[oc]=a
            t.rename(columns=rm,inplace=True); blocks.append(t)
        p=projections.get(meas)
        if p is not None and not p.empty:
            old=list(p.columns); new=[]
            for c in old:
                cn=f"{meas}_FY{int(c)}E"
                if cn not in seen: new.append(cn); seen.add(cn)
                else: a=cn+"_"+str(c); seen.add(a); new.append(a)
            p.rename(columns=dict(zip(old,new)),inplace=True); blocks.append(p)
    return pd.concat(blocks,axis=1).dropna(axis=1,how="all").drop_duplicates() if blocks else pd.DataFrame()

def period_sort_key(label):
    s=str(label)
    if s.startswith("FY"):s=s[2:]
    h,_,t=s.partition("-")
    try: yr=int(h)
    except ValueError: return(9999,99)
    try: return(yr,int(t[1:]) if t.startswith("Q") else int(t))
    except ValueError: return(yr,99)


def build_wide(q, m):
    def do_pv(df_i, pc, measures, pfx):
        if df_i.empty: return pd.DataFrame(),[]
        d=df_i.copy(); present=[c for c in measures if c in d.columns]
        d["_filled"]=d[present].notna().sum(axis=1).fillna(0) if present else 0
        d=(d.sort_values(["ticker",pc,"_filled"]).drop_duplicates(["ticker",pc],keep="last").drop(columns="_filled"))
        order=sorted(d[pc].dropna().unique(),key=period_sort_key)
        b,n=[],[]
        for meas in measures:
            if meas not in d.columns: continue
            p=d.pivot(index="ticker",columns=pc,values=meas)
            ex=[c for c in order if c in p.columns]; p=p.reindex(columns=ex)
            nc=[f"{pfx}{meas}_{c}" for c in p.columns]; p.columns=nc; b.append(p); n.extend(nc)
        return(pd.concat(b,axis=1).copy() if b else pd.DataFrame()),n

    q_only=q[q["period_type"].astype(str).str.upper()!="FY"] if "period_type" in q.columns else q.copy()
    qw,_=do_pv(q_only,"fiscal_quarter",Q_MEASURES,"")
    mw,_=do_pv(m,"month",M_MEASURES,"")
    ident=pd.concat([q[["ticker","company","currency"]],m[["ticker","company"]]],ignore_index=True).drop_duplicates("ticker").set_index("ticker")
    ident["units"]="financials in millions; EPS & prices actual"
    ann=build_annual(q); wide=ident.join(ann,how="outer").join(qw,how="outer").join(mw,how="outer"); wide.index.name="ticker"
    return wide.reset_index().sort_values("ticker").reset_index(drop=True)

def readme_frame(counts):
    rows=[("UNITS",""),("", "All currency totals - revenue, gross profit, EBIT,"),
          ("", "EBITDA, net income, market cap are in MILLIONS of the filing currency."),
          ("", "NOT scaled: diluted EPS, share prices, margins (%), volume amounts."),
          ("", ""),
          ("WHAT THIS IS", "5y financial data for S&P 500 + Nasdaq common + Fortune 500."),
          ("Data","One row/ticker; metric*period pair = its own column. Grouped by metric."),
          ("Universe","Every ticker with index membership flags from universe.csv."),
          ("Coverage","Per-company quarter/month counts and latest dates."),
          ("",""),
          ("CAVEATS","Important notes for interpreting the data."),
          ("Quarterlies (Q columns)","Revenue/EBIT/net income are QUARTERLY only."),
          ("Monthly","Only price/market cap/P/E are genuinely monthly."),
          ("Q4_derived","Where no 10-Q for Q4 exists, derived as FY-(Q1+Q2+Q3). Flagged true."),
          ("mkt_cap_est","= close * current shares; ignores historical buybacks/issuance."),
          ("pe_trailing_est","= close / trailing-4-quarter diluted EPS at that date."),
          ("EBIT gaps","Banks/insurers/REITs may not report EBIT -- blank is correct."),
          ("Fortune 500","~25% private or foreign-owned; no US ticker. See unmatched CSV."),
          ("Currency","Filing currency, NOT FX-converted."),]
    for k,v in list(counts.items())[:5]: rows.append(("",f"{k}: {v}"))
    return pd.DataFrame(rows,columns=["Item","Detail"])


def main():
    missing = [f for f in ("data_quarterly.csv","data_monthly.csv","data_snapshot.csv")
               if not os.path.exists(f)]
    if missing: sys.exit(f"Missing {missing} -- run fetch_financials.py first")
    print("="*56); print("build_excel: Starting data assembly and styling."); print("="*56)
    t0=time.time(); print("[excel] loading checkpoints ...")
    q=pd.read_csv("data_quarterly.csv").drop_duplicates(["ticker","period_end"])
    m=pd.read_csv("data_monthly.csv").drop_duplicates(["ticker","month"])
    s=pd.read_csv("data_snapshot.csv").drop_duplicates("ticker")
    q["period_end"]=pd.to_datetime(q["period_end"],errors="coerce")
    m["date"]=pd.to_datetime(m["date"],errors="coerce")
    ct_q=pd.Timestamp.today()-pd.DateOffset(years=6)
    ct_m=pd.Timestamp.today()-pd.DateOffset(years=5)
    q=q[q["period_end"]>=ct_q].sort_values(["ticker","period_end"])
    m=m[m["date"]>=ct_m].sort_values(["ticker","date"])
    q["period_end"]=q["period_end"].dt.date; m["date"]=m["date"].dt.date
    s.sort_values("market_cap",ascending=False,na_position="last",inplace=True)
    print(f"[excel] Loaded: q={len(q)}q m={len(m)}m s={len(s)} rows")
    wide_df=build_wide(q,m)
    print(f"[excel] wide: {len(wide_df)} tickers, {wide_df.shape[1]-1} cols")
    cov=[]
    for tk in sorted(wide_df["ticker"].unique()):
        tq_q=q[q["ticker"]==tk];tq_m=m[m["ticker"]==tk]
        lq=str(tq_q["period_end"].max()) if len(tq_q) else "n/a"
        lm=str(tq_m["date"].max()) if len(tq_m) else "n/a"
        cn=wide_df[wide_df["ticker"]==tk]["company"].dropna().unique()
        co=cn[0] if len(cn)>0 else ""
        cov.append({"ticker":tk,"company":co,"latest_quarter":lq,"q_count":len(tq_q),"latest_month":lm,"m_count":len(tq_m)})
    cover_df=pd.DataFrame(cov)
    wb=Workbook(); counts={"quarterly_rows":len(q),"monthly_rows":len(m),"snapshot_rows":len(s),"tickers_in_wide":len(wide_df),"wide_columns":wide_df.shape[1]-1}


    # Sheet 1: UNITS documentation
    print("[excel] writing UNITS sheet ...")
    ws=wb.active; ws.title="UNITS"; rows_w=readme_frame(counts)
    for ri,row in enumerate(rows_w.values,1):
        for ci,val in enumerate(row,1):
            cell=ws.cell(row=ri,column=ci,value=val)
    ws.freeze_panes="A2"

    # Sheet 2: SNAPSHOT (valuation per company from s DataFrame)
    print("[excel] writing SNAPSHOT sheet ...")
    snap=s.drop_duplicates(subset=["ticker"],keep="first").copy()
    snap.sort_values("market_cap",ascending=False,na_position="last",inplace=True)
    scols=list(snap.columns); ws_snap=wb.create_sheet("Snapshot")
    for ci,col in enumerate(scols,1):
        cell=ws_snap.cell(row=1,column=ci,value=col)
        cell.fill=HEADER_FILL;cell.font=HEADER_FONT;cell.alignment=Alignment(horizontal="center",vertical="center")
    for ri,vals in enumerate(snap.values,2):
        for ci,val in enumerate(vals,1):
            cell=ws_snap.cell(row=ri,column=ci,value=val)
            apply_fmt(cell,scols[ci-1])
    ws_snap.freeze_panes="A2"
    cw=get_column_letter(len(scols))
    ws_snap.auto_filter.ref=f"A1:{cw}{len(snap)+1}"

    # Sheet 3: DATA wide timeline
    print("[excel] writing DATA sheet ...")
    dout=wide_df.copy();dcols=list(dout.columns);ws_data=wb.create_sheet("Data")
    for ci,col in enumerate(dcols,1):
        cell=ws_data.cell(row=1,column=ci,value=col)
        cell.fill=HEADER_FILL;cell.font=HEADER_FONT;cell.alignment=Alignment(horizontal="center",vertical="center")
    for ri,vals in enumerate(dout.values,2):
        for ci,val in enumerate(vals,1):
            cell=ws_data.cell(row=ri,column=ci,value=val)
            apply_fmt(cell,dcols[ci-1])
    ws_data.freeze_panes="A2"
    dcw=get_column_letter(wide_df.shape[1])
    ws_data.auto_filter.ref=f"A1:{dcw}{len(wide_df)+1}"


    # Sheet 4: UNIVERSE (from universe.csv)
    print("[excel] writing UNIVERSE sheet ...")
    if os.path.exists("universe.csv"):
        udf=pd.read_csv("universe.csv");uc=list(udf.columns);ws_u=wb.create_sheet("Universe")
        for ci,col in enumerate(uc,1):
            cell=ws_u.cell(row=1,column=ci,value=col)
            cell.fill=HEADER_FILL;cell.font=HEADER_FONT;cell.alignment=Alignment(horizontal="center",vertical="center")
        for ri,vals in enumerate(udf.values,2):
            for ci,val in enumerate(vals,1):
                ws_u.cell(row=ri,column=ci,value=None if pd.isna(val) else val)
        uw=get_column_letter(len(uc))
        ws_u.auto_filter.ref=f"A1:{uw}{len(udf)+1}"

    # Sheet 5: COVERAGE (per-company completeness summary)
    print("[excel] writing COVERAGE sheet ...")
    ccols=list(cover_df.columns);ws_c=wb.create_sheet("Coverage")
    for ci,col in enumerate(ccols,1):
        cell=ws_c.cell(row=1,column=ci,value=col)
        cell.fill=HEADER_FILL;cell.font=HEADER_FONT;cell.alignment=Alignment(horizontal="center",vertical="center")
    for ri,vals in enumerate(cover_df.values,2):
        for ci,val in enumerate(vals,1):
            ws_c.cell(row=ri,column=ci,value=None if pd.isna(val) else val)
    cc=get_column_letter(len(ccols))
    ws_c.auto_filter.ref=f"A1:{cc}{len(cover_df)+1}"

    wb.save(OUT)
    print(f"[excel] wrote {OUT} in {time.time()-t0:.1f}s")
    print(f"       tickers={len(wide_df)}  data_cols={wide_df.shape[1]}  units_rows={len(rows_w)}")


if __name__ == "__main__":
    main()
