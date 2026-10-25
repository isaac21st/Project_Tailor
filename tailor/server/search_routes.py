"""Role search on the Tailor Desk: preferences, a search that runs in the background (on demand and each morning while
the server is running), the ranked results, and saving a result to the Desk. Nothing here applies to anything.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from ..config import Settings
from ..jobs.search import (FOCUS, FOCUS_LABELS, Prefs, SearchStore, base_title, company_name, discover, discover_workday, grouped, import_found, rank_found,
                           host_of, link_desk, run_search, spread)
from .desk import add_posting_record, tailor_desk_posting


class PrefsBody(BaseModel):
    focus: list[str] = Field(default_factory=list)
    include_words: list[str] = Field(default_factory=list)
    exclude_words: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    remote_ok: bool = True
    max_years: int = 3
    skip_no_sponsorship: bool = False
    companies: dict = Field(default_factory=dict)
    daily: bool = True
    auto_discover: bool = True
    early_anywhere: bool = True
    daily_hour: int = 8
    auto_save: bool = True
    auto_min_fit: int = 65
    auto_max: int = 5
    consulting_firms: list[str] | None = None  # None keeps the saved list


class AddCompanies(BaseModel):
    add: list[dict] = Field(default_factory=list)  # [{"kind": "greenhouse", "name": "benchling"}]


class SaveBody(BaseModel):
    tailor: bool = True
    instruction: str = Field(default="", max_length=300)


def weekly_discover(store: SearchStore, now: datetime) -> list[dict]:
    """Add the boards that have 2+ roles matching the preferences right now (at most once a week); what was
    added is kept with the runs so the Desk can show it."""
    data = store.load()
    last = (data.get("discovered") or {}).get("at", "")
    if last and (now - datetime.fromisoformat(last)).days < 7:
        return []
    p = store.prefs()
    picks = [sg for sg in discover(p) + discover_workday(p) if sg["matching"] >= 2][:15]
    for sg in picks:
        if sg["name"] not in p.companies[sg["kind"]]:
            p.companies[sg["kind"]].append(sg["name"])
    p.validated().save(store.prefs_path)
    with store.lock:
        data = store.load()
        data["discovered"] = {"at": now.isoformat(timespec="seconds"),
                              "added": [{"kind": sg["kind"], "name": sg["name"], "company": sg["company"],
                                         "matching": sg["matching"]} for sg in picks]}
        store.save(data)
    print(f"search: weekly discover added {len(picks)} boards", flush=True)
    return picks


class ImportBody(BaseModel):
    rows: list[dict] = Field(default_factory=list, max_length=300)


def register_search(app: FastAPI, settings: Settings, auth: list, desk_store, run_tailor,
                    browser_lock: threading.Lock | None = None) -> None:
    store = SearchStore(settings.data_dir)
    state = {"running": False, "stage": "", "done": 0, "total": 0, "error": ""}
    run_lock = threading.Lock()

    def resume_text() -> str:
        path = settings.profile_dir / "base_resume.json"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def start(reason: str) -> bool:
        if not run_lock.acquire(blocking=False):
            return False

        def work():
            state.update(running=True, stage="starting", done=0, total=0, error="")
            try:
                prefs = store.prefs()
                s = run_search(prefs, store, resume_text(),
                               lambda stage, d, t: state.update(stage=stage, done=d, total=t))
                print(f"search ({reason}): read {s['read']}, kept {s['kept']}, new {s['new']}", flush=True)
                if prefs.auto_save and prefs.auto_max:
                    state.update(stage="preparing", done=0, total=0)
                    prepared = auto_prepare(prefs, s["at"])
                    with store.lock:
                        data = store.load()
                        if data.get("runs") and data["runs"][0]["at"] == s["at"]:
                            data["runs"][0]["prepared"] = prepared
                            store.save(data)
                    print(f"search ({reason}): prepared {len(prepared)} on the Desk", flush=True)
            except Exception as e:  # a feed format change must not take the server down
                state["error"] = f"{type(e).__name__}: {e}"[:200]
                print(f"search failed: {state['error']}", flush=True)
            finally:
                state["running"] = False
                run_lock.release()

        threading.Thread(target=work, daemon=True).start()
        return True

    def save_result(rid: str, tailor: bool = True, instruction: str = "") -> dict:
        rec = store.load()["results"].get(rid)
        if not rec:
            raise HTTPException(status_code=404, detail="no such result")
        text = f"{rec['title']}\n{rec['location']}\n\n{rec.get('text', '')}"
        posting = add_posting_record(desk_store, text, url=rec["url"], title=rec["title"],
                                     company=company_name(rec), source="link")
        _mark(rid, desk_id=posting["id"])
        if tailor and not posting.get("duplicate"):
            posting = tailor_desk_posting(desk_store, run_tailor, posting["id"], instruction)
        return posting

    def sync_desk() -> dict:
        """Results with the Desk's links filled in (the Desk changes outside this module)."""
        with store.lock:
            data = store.load()
            if link_desk(data["results"], desk_store.load()):
                store.save(data)
        return data

    def auto_prepare(prefs: Prefs, run_at: str) -> list[dict]:
        """Save and tailor the best roles first seen in this run: one per role (not per city), fit at or above the
        person's bar, never ones they dismissed, never more than they allowed."""
        data = sync_desk()
        fresh = [r for r in grouped(list(data["results"].values()))
                 if r.get("first_seen") == run_at and not r.get("dismissed") and not r.get("desk_id")
                 and r.get("fit", 0) >= prefs.auto_min_fit and not r.get("clearance")
                 # only roles written for graduates / early careers, in the person's own cities
                 and r.get("early") and not r.get("outside_cities")
                 and not (prefs.skip_no_sponsorship and r.get("sponsorship") == "restricted")]
        out = []
        for r in spread(fresh)[: prefs.auto_max]:
            state.update(done=len(out) + 1, total=min(len(fresh), prefs.auto_max))
            try:
                p = save_result(r["id"])
                out.append({"company": company_name(r), "title": r["title"], "desk_id": p["id"], "status": p.get("status")})
            except Exception as e:  # one bad posting must not stop the rest
                print(f"auto-prepare failed for {r['title']}: {type(e).__name__}", flush=True)
        return out

    def daily_loop():
        while True:
            time.sleep(600)
            try:
                p = store.prefs()
                runs = store.load().get("runs", [])
                last = runs[0]["at"][:10] if runs else ""
                now = datetime.now()
                if p.daily and now.hour >= p.daily_hour and last != now.date().isoformat():
                    if p.auto_discover:
                        weekly_discover(store, now)
                    start("daily")
            except Exception:
                pass

    threading.Thread(target=daily_loop, daemon=True).start()

    @app.get("/api/search/prefs", dependencies=auth)
    def get_prefs() -> dict:
        from dataclasses import asdict

        return {"prefs": asdict(store.prefs()), "focus_labels": FOCUS_LABELS}

    @app.put("/api/search/prefs", dependencies=auth)
    def put_prefs(body: PrefsBody) -> dict:
        from dataclasses import asdict

        bad = [f for f in body.focus if f not in FOCUS]
        if bad:
            raise HTTPException(status_code=422, detail=f"unknown focus: {', '.join(bad)}")
        data = body.model_dump()
        if data.get("consulting_firms") is None:
            data["consulting_firms"] = store.prefs().consulting_firms
        p = Prefs(**data).validated()
        p.save(store.prefs_path)
        return {"prefs": asdict(p), "focus_labels": FOCUS_LABELS}

    @app.post("/api/search/run", dependencies=auth)
    def run_now() -> dict:
        started = start("on demand")
        return dict(state, started=started)

    @app.post("/api/search/discover", dependencies=auth)
    def suggest() -> dict:
        """Companies worth adding: boards not searched yet that have matching roles now (takes ~20-40 s)."""
        p = store.prefs()
        return {"suggestions": discover(p) + discover_workday(p)}

    @app.post("/api/search/companies", dependencies=auth)
    def add_companies(body: AddCompanies) -> dict:
        from dataclasses import asdict

        p = store.prefs()
        for item in body.add[:50]:
            kind, name = item.get("kind"), str(item.get("name", "")).strip()
            if kind in ("greenhouse", "lever", "ashby", "workday") and name and name not in p.companies[kind]:
                p.companies[kind].append(name)
        p = p.validated()
        p.save(store.prefs_path)
        return {"prefs": asdict(p)}

    def read_found() -> None:
        """Open each found role's link in the local headless browser (one at a time, like the Desk's links) and rank
        it. School job boards need the person's sign-in, so they are left as they are."""
        from .desk import fetch_posting

        todo = [r for r in store.load()["results"].values() if r.get("source") == "found" and not r.get("checked")]
        found_state.update(running=True, done=0, total=len(todo))
        try:
            for i, rec in enumerate(todo):
                text = ""
                if "12twenty.com" not in rec["url"]:
                    try:
                        with (browser_lock or threading.Lock()):
                            text = fetch_posting(rec["url"], settings).get("text", "")
                    except Exception as e:  # a site that blocks or times out: the role stays, unranked
                        print(f"found role not read ({type(e).__name__}): {rec['url'][:80]}", flush=True)
                rank_found(store, rec["id"], text, resume_text())
                found_state["done"] = i + 1
        finally:
            found_state["running"] = False

    found_state = {"running": False, "done": 0, "total": 0}

    @app.post("/api/search/import", dependencies=auth)
    def import_rows(body: ImportBody) -> dict:
        out = import_found(store, body.rows)
        if not found_state["running"]:
            threading.Thread(target=read_found, daemon=True).start()
        return dict(out, reading=True)

    @app.get("/api/search/status", dependencies=auth)
    def status() -> dict:
        runs = store.load().get("runs", [])
        return dict(state, last=runs[0] if runs else None, discovered=store.load().get("discovered"), found=found_state)

    @app.get("/api/search/results", dependencies=auth)
    def results(show: str = "open") -> dict:
        data = sync_desk()
        last = data["runs"][0]["at"] if data.get("runs") else ""
        rows = grouped(list(data["results"].values()))
        if show == "open":
            rows = [r for r in rows if not r.get("dismissed") and not r.get("desk_id")]
        elif show == "new":
            rows = [r for r in rows if r.get("first_seen") == last and not r.get("dismissed")]
        elif show == "saved":
            rows = [r for r in rows if r.get("desk_id")]
        elif show == "dismissed":
            rows = [r for r in rows if r.get("dismissed")]
        for r in rows:
            r["company_name"] = company_name(r)
            r["host"] = host_of(r["url"])
            r["new"] = r.get("first_seen") == last
        rows = spread(rows)
        counts = {"open": sum(1 for r in grouped(list(data["results"].values()))
                              if not r.get("dismissed") and not r.get("desk_id"))}
        return {"rows": rows, "last": data["runs"][0] if data.get("runs") else None, "counts": counts}

    def _mark(rid: str, **fields) -> dict:
        with store.lock:
            data = store.load()
            rec = data["results"].get(rid)
            if not rec:
                raise HTTPException(status_code=404, detail="no such result")
            # the same role in other cities is one decision
            for other in data["results"].values():
                if other["company"] == rec["company"] and base_title(other["title"]) == base_title(rec["title"]):
                    other.update(fields)
            store.save(data)
            return rec

    @app.post("/api/search/results/{rid}/dismiss", dependencies=auth)
    def dismiss(rid: str) -> dict:
        _mark(rid, dismissed=True)
        return {"dismissed": rid}

    @app.post("/api/search/results/{rid}/restore", dependencies=auth)
    def restore(rid: str) -> dict:
        _mark(rid, dismissed=False)
        return {"restored": rid}

    @app.post("/api/search/results/{rid}/save", dependencies=auth)
    def save(rid: str, body: SaveBody) -> dict:
        return save_result(rid, body.tailor, body.instruction)
