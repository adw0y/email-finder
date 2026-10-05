"""Email Finder app. Run with:  streamlit run app.py"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from finder.core import Person
from finder.export import to_csv, to_xlsx
from finder.pipeline import CATCH_ALL, FOUND, GUESS, NOT_FOUND, VERIFIED, Pipeline, Settings, Target
from finder.providers import ALL_KEYS, Registry
from finder.usage import UsageStore

HERE = Path(__file__).parent
ENV_PATH = HERE / ".env"
EXPORTS = HERE / "exports"
load_dotenv(ENV_PATH, override=True)

st.set_page_config(page_title="Email Finder", page_icon="✉️", layout="wide")
usage = UsageStore(HERE / "usage.json")

# Catch a half-applied update (new app.py, old files in the finder folder)
# before it turns into a confusing crash.
if "locations" not in getattr(Target, "__dataclass_fields__", {}):
    st.error("finder\\pipeline.py is an older version than app.py. Replace the whole "
             "email-finder-app folder with the latest download (copy your .env file across "
             "to keep your keys), then restart the app.")
    st.stop()


def current_keys() -> dict:
    return {k: os.environ.get(k, "").strip() for k, _ in ALL_KEYS}


def save_keys(new: dict) -> None:
    lines, seen = [], set()
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            k = line.split("=", 1)[0].strip()
            if k in new:
                if new[k]:
                    lines.append(f"{k}={new[k]}")
                seen.add(k)
            else:
                lines.append(line)
    for k, v in new.items():
        if k not in seen and v:
            lines.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(lines) + "\n")
    for k, v in new.items():
        if v:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)


# ----------------------------------------------------------------- sidebar
registry_preview = Registry(current_keys(), usage, log=lambda m: None)

with st.sidebar:
    st.header("Services")
    with st.expander("API keys", expanded=not any(current_keys().values())):
        st.caption("Stored only in a .env file on this computer.")
        entered = {}
        for key, label in ALL_KEYS:
            entered[key] = st.text_input(label, value=os.environ.get(key, ""),
                                         type="password", key=f"k_{key}")
        if st.button("Save keys", width="stretch"):
            save_keys({k: v.strip() for k, v in entered.items()})
            st.success("Saved")
            st.rerun()

    st.subheader("Free credits this month")
    groups = [("Find people", registry_preview.searchers, "search"),
              ("Find emails", registry_preview.finders, "find"),
              ("Verify", registry_preview.verifiers, "verify")]
    shown = set()
    for title, providers, op in groups:
        st.markdown(f"**{title}**")
        for p in providers:
            status = p.status(op)
            cap = p.cap(op)
            line = f"{p.label}"
            if cap != float("inf"):
                used = p.used(op)
                line += f" · {used:g} / {cap:g}"
            icon = {"ready": "🟢", "no key": "⚪", "out of credits": "🔴",
                    "free limit reached": "🔴"}.get(status, "🟡")
            st.markdown(f"{icon} {line}  \n<small>{status} · {p.free_note}</small>",
                        unsafe_allow_html=True)
            if cap != float("inf") and p.configured():
                st.progress(min(p.used(op) / cap, 1.0))
            if status == "out of credits" and p.name not in shown:
                if st.button(f"Reset {p.label}", key=f"reset_{p.name}_{op}"):
                    usage.reset(p.name)
                    st.rerun()
            shown.add(p.name)
    st.caption("Order within each step is the fallback order. "
               "When one runs out, the next takes over automatically.")

# ----------------------------------------------------------------- main
st.title("Email Finder")
st.caption("Find work emails by company and designation. Uses free API credits in order, "
           "then falls back to pattern guessing.")

source = st.radio("Start from", ["Company + designation", "A list of names"], horizontal=True)

targets: list[Target] = []
if source == "Company + designation":
    if "targets_df" not in st.session_state:
        st.session_state.targets_df = pd.DataFrame(
            [{"Company": "", "Domain": "", "Designation": "", "Max people": 10, "Locations": ""}])
    df = st.data_editor(
        st.session_state.targets_df, num_rows="dynamic", width="stretch",
        column_config={
            "Company": st.column_config.TextColumn(help="As people write it on LinkedIn, e.g. Zepto"),
            "Domain": st.column_config.TextColumn(help="Email domain, e.g. zeptonow.com. Leave blank to guess."),
            "Designation": st.column_config.TextColumn(help="e.g. Category Manager"),
            "Max people": st.column_config.NumberColumn(min_value=1, max_value=500, step=1),
            "Locations": st.column_config.TextColumn(
                help="Optional, comma-separated, e.g. Bengaluru, Mumbai, Gurugram. "
                     "Each city is searched separately, which finds far more people "
                     "for big targets."),
        }, key="targets_editor")
    for _, r in df.iterrows():
        company = str(r.get("Company") or "").strip()
        title = str(r.get("Designation") or "").strip()
        if company and title:
            locs = [x.strip() for x in str(r.get("Locations") or "").split(",") if x.strip()]
            targets.append(Target(company=company, domain=str(r.get("Domain") or "").strip(),
                                  title=title, limit=int(r.get("Max people") or 10),
                                  locations=locs))
else:
    st.caption("CSV or Excel with a **name** and **company** column. Optional: domain, title, linkedin.")
    up = st.file_uploader("Upload names", type=["csv", "xlsx"])
    if up:
        names = pd.read_csv(up) if up.name.endswith(".csv") else pd.read_excel(up)
        names.columns = [c.strip().lower() for c in names.columns]
        if not {"name", "company"} <= set(names.columns):
            st.error("Needs at least 'name' and 'company' columns.")
        else:
            st.dataframe(names.head(10), width="stretch")
            names = names.fillna("")
            if "domain" not in names:
                names["domain"] = ""
            for (company, domain), grp in names.groupby(["company", "domain"]):
                people = [Person.from_name(str(r["name"]), str(r.get("title", "")),
                                           str(r.get("linkedin", ""))) for _, r in grp.iterrows()]
                people = [p for p in people if p]
                if people:
                    targets.append(Target(company=str(company), domain=str(domain), people=people))

with st.expander("Settings"):
    mode = st.radio("Mode", ["Save credits (recommended)", "Max accuracy"],
                    help="Save credits: one finder lookup per company to learn the email "
                         "format, then cheap verifications. Max accuracy: a finder lookup "
                         "for every person.")
    c1, c2 = st.columns(2)
    crawl = c1.checkbox("Read the company website for published emails", value=True)
    snippets = c1.checkbox("Search the web for published emails (1 search credit)", value=True)
    use_ddg = c2.checkbox("Fall back to DuckDuckGo when search credits run out", value=True)
    use_smtp = c2.checkbox("Fall back to a local mail-server check (needs port 25)", value=True)
    guesses = st.slider("Guesses to verify per person before spending a finder credit", 1, 4, 2)

people_est = sum(len(t.people) if t.people else t.limit for t in targets)
if targets:
    st.caption(f"{len(targets)} compan{'y' if len(targets) == 1 else 'ies'}, "
               f"up to {people_est} people.")
    # Each person usually needs 1 to 2 verifications. Warn before a run
    # would drain the month's free verification credits.
    verify_left = int(sum(
        p.remaining("verify") / p.cost("verify") for p in registry_preview.verifiers
        if p.available("verify") and p.cap("verify") != float("inf")))
    if people_est > verify_left:
        st.warning(f"About {verify_left} free verifications left this month across your "
                   f"verifiers, and this run may need {people_est} or more. Once they run out, "
                   "the remaining people get pattern-based best guesses instead of verified "
                   "addresses.")
    if people_est > 100:
        st.caption("Big runs take a while: verifying each address can take several seconds, "
                   "so expect very roughly 2 to 5 minutes per 50 people. Keep the black "
                   "window open until it finishes.")

if st.button("Find emails", type="primary", disabled=not targets):
    settings = Settings(mode="accurate" if mode.startswith("Max") else "saver",
                        crawl=crawl, snippets=snippets, guesses_to_verify=guesses)
    bar = st.progress(0.0, text="Starting")
    with st.status("Working", expanded=True) as box:
        def log(msg: str) -> None:
            box.write(msg)

        def progress(frac: float, text: str) -> None:
            bar.progress(min(max(frac, 0.0), 1.0), text=text)

        reg = Registry(current_keys(), usage, log=log, use_ddg=use_ddg, use_smtp=use_smtp)
        pipe = Pipeline(reg, settings, log=log, progress=progress)
        rows = pipe.run(targets)
        box.update(label=f"Done: {len(rows)} people", state="complete", expanded=False)

    st.session_state.rows = rows
    st.session_state.events = pipe.events
    st.session_state.spent = reg.spent_this_run()
    if rows:
        EXPORTS.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        (EXPORTS / f"emails-{stamp}.xlsx").write_bytes(
            to_xlsx(rows, pipe.events, st.session_state.spent))
    st.rerun()

# ----------------------------------------------------------------- results
rows = st.session_state.get("rows")
if rows:
    st.subheader("Results")
    counts = {s: sum(1 for r in rows if r.status == s)
              for s in (VERIFIED, FOUND, CATCH_ALL, GUESS, NOT_FOUND)}
    cols = st.columns(5)
    for col, (label, n) in zip(cols, counts.items()):
        col.metric(label, n)

    df = pd.DataFrame([r.as_dict() for r in rows])
    st.dataframe(df, width="stretch", hide_index=True, column_config={
        "LinkedIn": st.column_config.LinkColumn(display_text="profile"),
        "Confidence": st.column_config.ProgressColumn(min_value=0, max_value=1, format="percent"),
    })

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    d1, d2, _ = st.columns([1, 1, 3])
    d1.download_button("Download CSV", to_csv(rows), f"emails-{stamp}.csv", "text/csv",
                       width="stretch")
    d2.download_button("Download Excel",
                       to_xlsx(rows, st.session_state.events, st.session_state.spent),
                       f"emails-{stamp}.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       width="stretch")
    spent = st.session_state.get("spent") or {}
    if spent:
        st.caption("Credits used this run: " + ", ".join(f"{k} {v:g}" for k, v in spent.items()))
    with st.expander("Run log"):
        st.code("\n".join(st.session_state.events), language=None)
