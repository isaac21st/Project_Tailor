"""Networking in Project Tailor: the /network page and its API. Per company the person is applying to: where the
roles are, the ranked searches for people who could refer them, the recruiter a posting names, message drafts, and the
contacts the person saves (status, history, follow-ups, outcomes). Nothing here contacts anyone or reads any site
about people: the person opens the searches and sends the messages themselves.
"""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from ..config import Settings
from ..network import (FOLLOW_UP_DAYS, STATUSES, TIE_KINDS, ConnectionStore, ContactStore, board_places, canonical_company, degree_short,
                       company_key, connections_at, contact_score, job_ref, parse_connections_csv, referral_blurb,
                       contact_ties, due_follow_ups, fill_name, follow_up_draft, load_ties, location_focus,
                       message_drafts, metro_of, outreach_stats, parse_profile_text, posting_contacts, posting_places,
                       parse_search_results, relation_of, search_plan)

PAGE = Path(__file__).with_name("network.html")


class ContactIn(BaseModel):
    posting_id: str = Field(default="", max_length=40)
    company: str = Field(default="", max_length=120)
    name: str = Field(default="", max_length=120)
    headline: str = Field(default="", max_length=400)   # their title / headline as the person copied it
    location: str = Field(default="", max_length=120)
    link: str = Field(default="", max_length=400)       # their profile link, pasted by the person
    email: str = Field(default="", max_length=200)
    notes: str = Field(default="", max_length=2000)
    role: str = Field(default="", max_length=300)        # from a pasted search result: "Product Manager · Adobe"
    education: str = Field(default="", max_length=300)
    snippet: str = Field(default="", max_length=800)     # the result's text, kept to show why they matched
    connection: bool = False                              # a 1st-degree connection from the person's own export


class ResultsIn(BaseModel):
    text: str = Field(default="", max_length=60000)
    links: list[dict] = Field(default_factory=list, max_length=200)
    key: str = Field(default="", max_length=120)


class BulkIn(BaseModel):
    key: str = Field(default="", max_length=120)
    people: list[ContactIn] = Field(default_factory=list, max_length=60)


class ContactPatch(BaseModel):
    status: str | None = None
    notes: str | None = Field(default=None, max_length=2000)
    name: str | None = Field(default=None, max_length=120)
    headline: str | None = Field(default=None, max_length=400)
    location: str | None = Field(default=None, max_length=120)
    link: str | None = Field(default=None, max_length=400)
    email: str | None = Field(default=None, max_length=200)
    company: str | None = Field(default=None, max_length=120)
    followed_up: bool = False    # "I sent the follow-up": the reminder starts over


class TiesIn(BaseModel):
    weights: dict[str, float] = Field(default_factory=dict)
    off: list[str] = Field(default_factory=list)
    custom: list[dict] = Field(default_factory=list)


class ConnectionsIn(BaseModel):
    text: str = Field(default="", max_length=5_000_000)  # the person's Connections.csv


class AutopilotIn(BaseModel):
    enabled: bool = True
    hour: int = Field(default=8, ge=0, le=23)
    max_new: int = Field(default=5, ge=0, le=20)   # connections lined up per day
    notify: bool = True                            # a macOS notification with the day's plan


class ParseIn(BaseModel):
    text: str = Field(default="", max_length=8000)


def register_network(app: FastAPI, settings: Settings, auth: list, desk_store, profile=None, token: str = "") -> None:
    contacts = ContactStore(settings.data_dir)
    conn_store = ConnectionStore(settings.data_dir)
    overrides_path = settings.profile_dir / "profile_overrides.json"

    @app.get("/network", response_class=HTMLResponse)
    def network_page() -> HTMLResponse:
        # like the Desk: the page carries the token to call this server; only 127.0.0.1 is answered (Host checked)
        html = PAGE.read_text(encoding="utf-8").replace("__TAILOR_TOKEN__", token)
        return HTMLResponse(html, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                           "Referrer-Policy": "no-referrer"})

    def overrides() -> dict:
        try:
            return json.loads(overrides_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def ties_now():
        return load_ties(overrides(), profile)

    def person() -> dict:
        """The facts the drafts use, from the person's profile: name, the degrees they are finishing (named the way
        their ties name the schools), graduation, the school they would share with an alum, their latest employer."""
        name = getattr(profile, "full_name", "") or ""
        edu = list(getattr(profile, "education", []) or [])
        current = [e for e in edu if getattr(e, "expected", False)] or edu[:1]
        months = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                  "November", "December"]
        grad = ""
        for e in current:
            if getattr(e, "end", None):
                grad = (months[e.end.month - 1] + " " if e.end.month else "") + str(e.end.year)
                break
        ties = ties_now()

        def label_for(school: str) -> str:
            low = (school or "").lower()
            hits = [(bool(t.within), max(len(w) for w in t.words if w in low), t) for t in ties
                    if t.kind in ("school", "program") and t.words and any(w in low for w in t.words)]
            return max(hits, key=lambda h: (h[0], h[1]))[2].label if hits else school

        an = lambda abbr: "an" if abbr[:1] in "AEFHILMNORSX" else "a"  # noqa: E731  ("an MBA", "an MS", "a BA")
        # the degree whose school the person weights highest leads ("Your ties" on the Network page)
        weight = {t.label: t.weight for t in ties}
        current.sort(key=lambda e: -weight.get(label_for(e.school), 0))
        parts = []
        for e in current:
            abbr = degree_short(e.degree)
            field = f" in {e.field}" if e.field and abbr in ("MS", "MA", "BS", "BA") else ""
            parts.append(f"{an(abbr)} {abbr}{field} at {label_for(e.school)}")
        first = current[0] if current else None
        jobs = [x for x in (getattr(profile, "experience", []) or []) if getattr(x, "kind", "job") == "job"]
        out = {"name": name, "grad": grad or "this year", "school_line": " and ".join(parts) or "my degree"}
        if first is not None:
            abbr, school = degree_short(first.degree), label_for(first.school)
            out.update(school=school, study=f"my {abbr} at {school}", student=f"{an(abbr)} {abbr} student at {school}")
        if jobs:
            out["past"] = next((t.label for t in ties if t.kind == "employer" and t.words and
                                any(w in jobs[0].employer.lower() for w in t.words)), jobs[0].employer)
        return out

    def home_and_prefs() -> tuple[str, list[str]]:
        city = getattr(profile, "city", "") or ""
        state = getattr(profile, "state", "") or ""
        return (f"{city}, {state}".strip(", "), list(getattr(profile, "location_preferences", []) or []))

    def places_of(p: dict) -> list[str]:
        """A posting's office cities: from its text, else its job board (kept on the posting once looked up)."""
        if p.get("places") is not None:
            return list(p["places"])
        if p.get("stage") == "prospect":
            return posting_places(p.get("text", ""))
        places = posting_places(p.get("text", "")) or board_places(p.get("url", ""))
        with desk_store.lock:
            items = desk_store.load()
            for q in items:
                if q.get("id") == p.get("id"):
                    q["places"] = places
            desk_store.save(items)
        return places

    def clean_place(x: str) -> str:
        """"Dallas - 2651 N Harwood St", "US - VA, Arlington", "San Francisco · Remote" -> a city name."""
        x = re.sub(r"^.*>\s*", "", x.strip())                       # "United States > Austin : 8701 Bee Caves Rd"
        x = re.sub(r"^(?:US|USA|United States)\s*-\s*(?:[A-Z]{2},\s*)?", "", x)
        x = re.sub(r"\s*:.*$|\s+HQ\b", "", x)
        if re.match(r"^\d", x):                                     # a street address, not a city
            return ""
        x = re.split(r"\s+-\s+|\s+·\s+|,|\(", x)[0].strip()
        if re.search(r"\boffices?\b|flexible|negotiable|multiple|^united states$|^us$|^usa$", x, re.I):
            return ""
        return x

    def prospects() -> list[dict]:
        """Roles in the search results the person has not dismissed or saved yet: graduate / early-career roles and
        ones found by hand. Networking before applying is when a referral helps most."""
        from ..jobs.search import SearchStore, company_name, grouped

        try:
            results = SearchStore(settings.data_dir).load()["results"]
        except Exception:
            return []
        out = []
        for r in grouped(list(results.values())):
            if r.get("dismissed") or r.get("desk_id") or not (r.get("early") or r.get("source") == "found"):
                continue
            loc = r.get("location", "")
            places = [x.strip() for x in re.split(r"[;|]|\s{2,}", loc) if x.strip()][:4]
            out.append({"id": "s-" + r["id"], "title": r["title"], "url": r["url"], "stage": "prospect",
                        "company": company_name(r), "text": (results.get(r["id"]) or {}).get("text", ""),
                        "places": list(dict.fromkeys(clean_place(x) for x in places if clean_place(x))) or None,
                        "facts": {"deadline": r.get("deadline")}})
        return out

    def postings_by_company() -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for p in desk_store.load() + prospects():
            if p.get("stage") == "skipped" or not (p.get("company") or "").strip():
                continue
            out.setdefault(canonical_company(p["company"]), []).append(p)
        return out

    def company_places(posts: list[dict]) -> list[str]:
        seen: dict[str, str] = {}
        remote = False
        for p in posts:
            for pl in places_of(p):
                if pl == "Remote":
                    remote = True
                    continue
                seen.setdefault(metro_of(pl) or pl, pl)
        return list(seen.values()) + (["Remote"] if remote else [])

    def enrich(c: dict, places: list[str]) -> dict:
        ties = ties_now()
        home, _ = home_and_prefs()
        found = contact_ties(c, ties, places, home)
        due = c.get("status") == "messaged" and bool(c.get("contacted_at")) and (
            date.today() - date.fromisoformat(c["contacted_at"][:10]) >= timedelta(days=FOLLOW_UP_DAYS))
        return dict(c, ties=found, score=contact_score(found, ties), relation=relation_of(found, ties), follow_up_due=due)

    def places_for_contact(c: dict, by_company: dict[str, list[dict]] | None = None) -> list[str]:
        by_company = by_company if by_company is not None else postings_by_company()
        if c.get("posting_id"):
            for posts in by_company.values():
                for p in posts:
                    if p["id"] == c["posting_id"]:
                        return company_places(posts)
        return company_places(by_company.get(canonical_company(c.get("company", "")), []))

    def active_title(posts: list[dict]) -> str:
        live = [p for p in posts if p.get("stage") in ("saved", "applying")] or \
            [p for p in posts if p.get("stage") == "prospect"] or posts
        return live[0].get("title", "") if live else ""

    def days_left(posts: list[dict]) -> int | None:
        """Days to the nearest deadline of a posting not yet applied to (a referral helps most before applying)."""
        left = []
        for p in posts:
            dl = (p.get("facts") or {}).get("deadline")
            if p.get("stage") in ("saved", "applying") and dl:
                try:
                    left.append((date.fromisoformat(str(dl)[:10]) - date.today()).days)
                except ValueError:
                    pass
        future = [d for d in left if d >= 0]
        return min(future) if future else None

    def target_view(key: str, posts: list[dict], with_detail: bool = False) -> dict:
        home, prefs = home_and_prefs()
        places = company_places(posts)
        focus = location_focus(places, home, prefs)
        fallback = not [f for f in focus if f != "Remote"]
        if fallback:  # the postings name no city ("US offices", "Flexible"): the person's own cities, home first
            focus = location_focus(list(prefs) or ["San Francisco"], home, prefs)
        company = posts[0].get("company", "")
        mine = [c for c in contacts.load() if canonical_company(c.get("company", "")) == key]
        conns = connections_at(company, conn_store.load()["items"])
        counts = {s: sum(1 for c in mine if c.get("status") == s) for s in STATUSES}
        view = {
            "key": key, "company": company, "focus": focus, "home_metro": metro_of(home), "focus_fallback": fallback,
            "in_your_area": bool(focus and metro_of(focus[0]) and metro_of(focus[0]) == metro_of(home)),
            "postings": [{"id": p["id"], "title": p.get("title", ""), "stage": p.get("stage", ""), "url": p.get("url", ""),
                          "deadline": (p.get("facts") or {}).get("deadline")} for p in posts],
            "active": sum(1 for p in posts if p.get("stage") in ("saved", "applying")),
            "prospects": sum(1 for p in posts if p.get("stage") == "prospect"),
            "connections": len(conns),
            "days_left": days_left(posts),
            "applied": sum(1 for p in posts if p.get("stage") in ("applied", "interview", "offer")),
            "contacts": len(mine), "counts": counts,
        }
        if with_detail:
            ties = ties_now()
            top_school = max((t for t in ties if t.kind in ("school", "program", "club")), key=lambda t: t.weight, default=None)
            top_job = max((t for t in ties if t.kind == "employer"), key=lambda t: t.weight, default=None)
            live = next((p for p in posts if p.get("stage") in ("saved", "applying", "prospect") and p.get("url")), {})
            url = live.get("url", "")
            saved_links = {c.get("link") for c in mine if c.get("link")}
            view.update(
                searches=[asdict(s) for s in search_plan(company, focus, ties)],
                posting_contacts=[dict(pc, posting=p.get("title", "")) for p in posts for pc in posting_contacts(p.get("text", ""))],
                drafts=[asdict(d) for d in message_drafts(person(), company, active_title(posts), focus[0] if focus else "",
                                                          top_school, top_job, url, job_ref(url, live.get("text", "")))
                        if d.relation != "connection" or conns],
                connection_list=[dict(c, saved=c.get("link") in saved_links) for c in conns][:100],
                people=sorted((enrich(c, places) for c in mine), key=lambda c: -c["score"]))
        return view

    # ---- targets: the companies the person applies to ----
    @app.get("/api/network/targets", dependencies=auth)
    def targets() -> dict:
        views = [target_view(k, v) for k, v in postings_by_company().items()]
        # companies with open applications first, then the ones in the person's area, then fewest contacts so far
        # companies with open applications first (the nearest deadline first), then the person's area, then fewest contacts
        # open applications first, then companies where the person already knows someone, then prospects; inside
        # each, the nearest deadline, the person's area, fewest contacts so far
        views.sort(key=lambda t: (0 if t["active"] else 1 if t["connections"] else 2 if t["prospects"] else 3,
                                  t["connections"] == 0, t["days_left"] if t["days_left"] is not None else 999,
                                  not t["in_your_area"], t["contacts"] > 0, t["company"].lower()))
        return {"targets": views}

    @app.get("/api/network/target/{key}", dependencies=auth)
    def target(key: str) -> dict:
        posts = postings_by_company().get(key)
        if not posts:
            raise HTTPException(status_code=404, detail="no postings for that company")
        return target_view(key, posts, with_detail=True)

    # per posting (the Desk's Network card)
    @app.get("/api/network/posting/{pid}", dependencies=auth)
    def network_for_posting(pid: str) -> dict:
        p = desk_store.find(desk_store.load(), pid)
        key = canonical_company(p.get("company", ""))
        posts = postings_by_company().get(key) or [p]
        v = target_view(key, posts, with_detail=True)
        home, prefs = home_and_prefs()
        places = places_of(p)
        ties = ties_now()
        top_school = max((t for t in ties if t.kind in ("school", "program", "club")), key=lambda t: t.weight, default=None)
        top_job = max((t for t in ties if t.kind == "employer"), key=lambda t: t.weight, default=None)
        focus = location_focus(places, home, prefs)
        return {
            "company": p.get("company", ""), "title": p.get("title", ""), "key": key,
            "places": places, "focus": focus, "home_metro": metro_of(home),
            "searches": [asdict(s) for s in search_plan(p.get("company", ""), focus, ties)],
            "posting_contacts": posting_contacts(p.get("text", "")),
            "drafts": [asdict(d) for d in message_drafts(person(), p.get("company", ""), p.get("title", ""),
                                                         focus[0] if focus else "", top_school, top_job, p.get("url", ""))],
            "contacts": v["people"],
            "ties": [t.label for t in ties],
        }

    # ---- contacts ----
    @app.get("/api/network/contacts", dependencies=auth)
    def list_contacts() -> dict:
        by_company = postings_by_company()
        titles = {p["id"]: p for posts in by_company.values() for p in posts}
        out = []
        for c in contacts.load():
            e = enrich(c, places_for_contact(c, by_company))
            p = titles.get(c.get("posting_id", ""))
            e["posting"] = {"id": p["id"], "title": p.get("title", ""), "company": p.get("company", "")} if p else None
            e["company_key"] = canonical_company(c.get("company", ""))
            out.append(e)
        order = {s: i for i, s in enumerate(STATUSES)}
        out.sort(key=lambda c: (not c["follow_up_due"], order.get(c.get("status", "to contact"), 9), -c["score"]))
        return {"contacts": out, "statuses": list(STATUSES), "due": sum(1 for c in out if c["follow_up_due"])}

    def find_dupe(items: list[dict], c: dict) -> dict | None:
        return next((x for x in items if (c.get("link") and x.get("link") == c["link"]) or
                     (c.get("email") and x.get("email", "").lower() == c["email"].lower()) or
                     (c.get("name") and x.get("name", "").lower() == c["name"].lower() and
                      canonical_company(x.get("company", "")) == canonical_company(c.get("company", "")))), None)

    @app.post("/api/network/results", dependencies=auth)
    def parse_results(body: ResultsIn) -> dict:
        """The people on a page of search results the person pasted, with their ties and score, best first."""
        posts = postings_by_company().get(body.key, [])
        company = posts[0].get("company", "") if posts else ""
        places = company_places(posts)
        saved = contacts.load()
        out = []
        for p in parse_search_results(body.text, body.links, company):
            e = enrich(dict(p, company=company), places)
            e["saved"] = find_dupe(saved, dict(p, company=company)) is not None
            out.append(e)
        out.sort(key=lambda x: (x["saved"], not x["at_company"], -x["score"]))
        return {"company": company, "people": out}

    @app.post("/api/network/contacts/bulk", dependencies=auth)
    def add_many(body: BulkIn) -> dict:
        posts = postings_by_company().get(body.key)
        if not posts:
            raise HTTPException(status_code=404, detail="no postings for that company")
        live = next((p for p in posts if p.get("stage") in ("saved", "applying")), posts[0])
        now = datetime.now().isoformat(timespec="seconds")
        added, dupes = [], 0
        with contacts.lock:
            items = contacts.load()
            for b in body.people:
                c = {k: (v.strip() if isinstance(v, str) else v) for k, v in b.model_dump().items()}
                if not c["name"]:
                    continue
                c["company"], c["posting_id"] = live.get("company", ""), live["id"]
                if find_dupe(items, c):
                    dupes += 1
                    continue
                c.update(id=ContactStore.new_id(), status="to contact", added=now, contacted_at="", follow_ups=0,
                         history=[{"at": now, "what": "saved from search results"}])
                items.append(c)
                added.append(c["id"])
            contacts.save(items)
        return {"added": len(added), "duplicates": dupes, "ids": added}

    @app.post("/api/network/contacts", dependencies=auth)
    def add_contact(body: ContactIn) -> dict:
        if not body.name.strip() and not body.email.strip():
            raise HTTPException(status_code=422, detail="give at least a name or an email")
        c = {k: (v.strip() if isinstance(v, str) else v) for k, v in body.model_dump().items()}
        if body.posting_id:
            p = desk_store.find(desk_store.load(), body.posting_id)  # 404 for an unknown posting
            c["company"] = c["company"] or p.get("company", "")
        if not c["company"]:
            raise HTTPException(status_code=422, detail="which company do they work at?")
        now = datetime.now().isoformat(timespec="seconds")
        c.update(id=ContactStore.new_id(), status="to contact", added=now, contacted_at="", follow_ups=0,
                 history=[{"at": now, "what": "saved"}])
        with contacts.lock:
            items = contacts.load()
            dupe = find_dupe(items, c)
            if dupe:
                return dict(enrich(dupe, places_for_contact(dupe)), duplicate=True)
            items.append(c)
            contacts.save(items)
        return enrich(c, places_for_contact(c))

    @app.patch("/api/network/contacts/{cid}", dependencies=auth)
    def patch_contact(cid: str, body: ContactPatch) -> dict:
        now = datetime.now().isoformat(timespec="seconds")
        with contacts.lock:
            items = contacts.load()
            c = next((x for x in items if x.get("id") == cid), None)
            if c is None:
                raise HTTPException(status_code=404, detail="unknown contact")
            hist = c.setdefault("history", [])
            if body.status is not None and body.status != c.get("status"):
                if body.status not in STATUSES:
                    raise HTTPException(status_code=422, detail="status must be one of " + ", ".join(STATUSES))
                if body.status == "messaged" and not c.get("contacted_at"):
                    c["contacted_at"] = date.today().isoformat()
                c["status"] = body.status
                hist.append({"at": now, "what": body.status})
            if body.followed_up:
                c["contacted_at"] = date.today().isoformat()
                c["follow_ups"] = int(c.get("follow_ups", 0)) + 1
                c["status"] = "messaged"
                hist.append({"at": now, "what": f"follow-up {c['follow_ups']} sent"})
            for k in ("notes", "name", "headline", "location", "link", "email", "company"):
                if getattr(body, k) is not None:
                    c[k] = getattr(body, k).strip()
            contacts.save(items)
        return enrich(c, places_for_contact(c))

    @app.delete("/api/network/contacts/{cid}", dependencies=auth)
    def delete_contact(cid: str) -> dict:
        with contacts.lock:
            items = contacts.load()
            contacts.save([x for x in items if x.get("id") != cid])
        return {"deleted": cid}

    @app.get("/api/network/contacts/{cid}/draft", dependencies=auth)
    def contact_draft(cid: str) -> dict:
        """The message that fits this contact best (alum, past coworker, same city, recruiter), with their first name,
        and the follow-up nudge."""
        c = next((x for x in contacts.load() if x.get("id") == cid), None)
        if c is None:
            raise HTTPException(status_code=404, detail="unknown contact")
        ties = ties_now()
        by_company = postings_by_company()
        posts = by_company.get(canonical_company(c.get("company", "")), [])
        places = places_for_contact(c, by_company)
        e = enrich(c, places)
        home, prefs = home_and_prefs()
        focus = location_focus(places, home, prefs)
        p = next((x for x in posts if x["id"] == c.get("posting_id")), None) or \
            next((x for x in posts if x.get("stage") in ("saved", "applying")), None) or (posts[0] if posts else {})
        shared_school = next((t for t in ties if t.kind in ("school", "program", "club") and t.label in e["ties"]), None)
        shared_job = next((t for t in ties if t.kind == "employer" and t.label in e["ties"]), None)
        drafts = message_drafts(person(), c.get("company", ""), p.get("title", ""), focus[0] if focus else "",
                                shared_school, shared_job, p.get("url", ""))
        d = next((d for d in drafts if d.relation == e["relation"]), None) or next(d for d in drafts if d.relation == "local")
        return {"relation": e["relation"], "label": d.label, "note": fill_name(d.note, c.get("name", "")),
                "message": fill_name(d.message, c.get("name", "")), "ties": e["ties"],
                "follow_up": follow_up_draft(person(), c.get("company", ""), p.get("title", ""), c.get("name", ""))}

    @app.get("/api/network/due", dependencies=auth)
    def follow_ups() -> dict:
        due = due_follow_ups(contacts.load())
        return {"due": [{"id": c["id"], "name": c.get("name", ""), "company": c.get("company", ""),
                         "contacted_at": c.get("contacted_at", "")} for c in due]}

    @app.get("/api/network/stats", dependencies=auth)
    def stats() -> dict:
        enriched = [enrich(c, places_for_contact(c)) for c in contacts.load()]
        s = outreach_stats(enriched, ties_now())
        s["due"] = sum(1 for c in enriched if c["follow_up_due"])
        return s

    @app.post("/api/network/parse", dependencies=auth)
    def parse(body: ParseIn) -> dict:
        return parse_profile_text(body.text)

    @app.get("/api/network/contacts.csv", dependencies=auth)
    def export_csv() -> Response:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Name", "Company", "Headline", "Location", "Ties", "Score", "Status", "Messaged on", "Follow-ups",
                    "Email", "Profile link", "Notes"])
        for c in list_contacts()["contacts"]:
            w.writerow([c.get("name", ""), c.get("company", ""), c.get("headline", ""), c.get("location", ""),
                        "; ".join(c.get("ties", [])), c.get("score", 0), c.get("status", ""), c.get("contacted_at", ""),
                        c.get("follow_ups", 0), c.get("email", ""), c.get("link", ""), c.get("notes", "")])
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="network_contacts.csv"'})

    # ---- autopilot: the day's networking plan, prepared every morning (never sends anything) ----
    auto_path = settings.data_dir / "network" / "autopilot.json"

    def auto_load() -> dict:
        try:
            return json.loads(auto_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"settings": AutopilotIn().model_dump(), "last_run": "", "digest": None}

    def auto_save(data: dict) -> None:
        auto_path.parent.mkdir(parents=True, exist_ok=True)
        auto_path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")

    def run_autopilot(now: datetime | None = None) -> dict:
        """Line up today's referral work: 1st-degree connections at companies with nobody saved yet (saved as "to
        contact" with their draft ready), recruiters named on postings the person applied to, companies with a deadline
        within two weeks and nobody to ask, and the follow-ups due. Nothing is sent; the person sends every message."""
        now = now or datetime.now()
        data = auto_load()
        cfg = AutopilotIn(**data.get("settings", {}))
        by_company = postings_by_company()
        conns = conn_store.load()["items"]
        stamp = now.isoformat(timespec="seconds")
        warm, recruiters, urgent = [], [], []
        with contacts.lock:
            items = contacts.load()
            # 1. people who already know the person, one per company, best placed first
            for key, posts in by_company.items():
                if len(warm) >= cfg.max_new:
                    break
                company = posts[0].get("company", "")
                if any(canonical_company(c.get("company", "")) == key for c in items):
                    continue
                here = connections_at(company, conns)
                if not here:
                    continue
                live = next((p for p in posts if p.get("stage") in ("saved", "applying")), None)
                if not live and not any(p.get("stage") == "prospect" for p in posts):
                    continue  # already applied everywhere there: a referral comes too late
                pick = sorted(here, key=lambda c: -contact_score(contact_ties({"headline": c.get("position", ""),
                                                                                 "connection": True}, ties_now(), [], ""), ties_now()))[0]
                c = {"id": ContactStore.new_id(), "posting_id": live["id"] if live else "", "company": company,
                     "name": pick["name"], "headline": pick.get("position", ""), "location": "", "link": pick.get("link", ""),
                     "email": pick.get("email", ""), "notes": f"1st-degree connection (connected {pick.get('connected_on', '')})",
                     "connection": True, "status": "to contact", "added": stamp, "contacted_at": "", "follow_ups": 0,
                     "history": [{"at": stamp, "what": "lined up by autopilot"}]}
                if not find_dupe(items, c):
                    items.append(c)
                    warm.append({"id": c["id"], "name": c["name"], "company": company, "key": key})
            # 2. the recruiter a posting names, once the person has applied there
            for key, posts in by_company.items():
                for p in posts:
                    if p.get("stage") not in ("applied", "interview"):
                        continue
                    for pc in posting_contacts(p.get("text", "")):
                        if not pc.get("name") and not pc.get("email"):
                            continue
                        c = {"id": ContactStore.new_id(), "posting_id": p["id"], "company": p.get("company", ""),
                             "name": pc.get("name", ""), "headline": pc.get("title", "") or "Recruiting", "location": "",
                             "link": "", "email": pc.get("email", ""), "notes": "Named on the posting",
                             "status": "to contact", "added": stamp, "contacted_at": "", "follow_ups": 0,
                             "history": [{"at": stamp, "what": "lined up by autopilot (you applied)"}]}
                        if not find_dupe(items, c):
                            items.append(c)
                            recruiters.append({"id": c["id"], "name": c["name"] or c["email"], "company": c["company"]})
            contacts.save(items)
        # 3. a deadline within two weeks, not applied yet, and nobody to ask
        for key, posts in by_company.items():
            view = target_view(key, posts)
            if view["days_left"] is not None and view["days_left"] <= 14 and not view["contacts"] and not view["connections"]:
                plan = search_plan(view["company"], view["focus"], ties_now())
                urgent.append({"key": key, "company": view["company"], "days_left": view["days_left"],
                               "search": asdict(plan[0]) if plan else None})
        urgent.sort(key=lambda u: u["days_left"])
        due = [{"id": c["id"], "name": c.get("name", ""), "company": c.get("company", "")} for c in due_follow_ups(contacts.load())]
        digest = {"at": stamp, "warm": warm, "recruiters": recruiters, "urgent": urgent[:8], "follow_ups": due,
                  "to_contact": sum(1 for c in contacts.load() if c.get("status") == "to contact")}
        data.update(last_run=stamp, digest=digest)
        auto_save(data)
        print(f"network autopilot: {len(warm)} to ask, {len(recruiters)} recruiters, {len(urgent)} deadlines, "
              f"{len(due)} follow-ups", flush=True)
        if cfg.notify:
            notify_mac(digest)
        return digest

    def notify_mac(d: dict) -> None:
        """A macOS notification with the day's plan (the server runs in the person's session)."""
        import subprocess

        parts = []
        if d["warm"]:
            parts.append(f"{len(d['warm'])} people you know to ask")
        if d["follow_ups"]:
            parts.append(f"{len(d['follow_ups'])} follow-up{'s' if len(d['follow_ups']) > 1 else ''} due")
        if d["urgent"]:
            parts.append(f"{len(d['urgent'])} deadline{'s' if len(d['urgent']) > 1 else ''} with nobody to ask")
        if d["recruiters"]:
            parts.append(f"{len(d['recruiters'])} recruiter{'s' if len(d['recruiters']) > 1 else ''} to thank")
        if not parts:
            return
        msg = "; ".join(parts) + ". Open 127.0.0.1:8765/network"
        try:
            subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "Tailor: today\'s referrals"'],
                           timeout=10, check=False)
        except Exception:
            pass

    def autopilot_loop() -> None:
        import time

        while True:
            time.sleep(600)
            try:
                data = auto_load()
                cfg = AutopilotIn(**data.get("settings", {}))
                now = datetime.now()
                if cfg.enabled and now.hour >= cfg.hour and data.get("last_run", "")[:10] != now.date().isoformat():
                    run_autopilot(now)
            except Exception as e:  # a bad record must not stop the loop
                print(f"network autopilot failed: {type(e).__name__}: {e}", flush=True)

    if not getattr(app.state, "testing", False):
        import threading

        threading.Thread(target=autopilot_loop, daemon=True).start()

    @app.get("/api/network/autopilot", dependencies=auth)
    def autopilot_state() -> dict:
        data = auto_load()
        return {"settings": AutopilotIn(**data.get("settings", {})).model_dump(), "last_run": data.get("last_run", ""),
                "digest": data.get("digest")}

    @app.put("/api/network/autopilot", dependencies=auth)
    def autopilot_settings(body: AutopilotIn) -> dict:
        data = auto_load()
        data["settings"] = body.model_dump()
        auto_save(data)
        return autopilot_state()

    @app.post("/api/network/autopilot/run", dependencies=auth)
    def autopilot_now() -> dict:
        return run_autopilot()

    # ---- the person's own connections ----
    @app.post("/api/network/connections", dependencies=auth)
    def import_connections(body: ConnectionsIn) -> dict:
        """Replace the saved connections with this export (LinkedIn: Settings, Data privacy, Get a copy of your data,
        Connections). Returns which companies on the person's list they cover."""
        items = parse_connections_csv(body.text)
        if not items:
            raise HTTPException(status_code=422, detail="that does not look like LinkedIn's Connections.csv")
        with conn_store.lock:
            conn_store.save({"imported_at": datetime.now().isoformat(timespec="seconds"), "items": items})
        return connections_summary()

    @app.get("/api/network/connections", dependencies=auth)
    def connections_summary() -> dict:
        data = conn_store.load()
        by_company = postings_by_company()
        warm = []
        for key, posts in by_company.items():
            here = connections_at(posts[0].get("company", ""), data["items"])
            if here:
                warm.append({"key": key, "company": posts[0].get("company", ""), "count": len(here),
                             "names": [c["name"] for c in here[:5]]})
        warm.sort(key=lambda w: -w["count"])
        return {"imported_at": data.get("imported_at", ""), "total": len(data["items"]), "warm": warm}

    @app.delete("/api/network/connections", dependencies=auth)
    def forget_connections() -> dict:
        with conn_store.lock:
            conn_store.save({"imported_at": "", "items": []})
        return {"total": 0}

    # ---- what a referrer needs ----
    @app.get("/api/network/packet/{key}", dependencies=auth)
    def packet(key: str, pid: str = "") -> dict:
        """For a referrer: the job title, its job ID, the link, two lines about the person, and the tailored resume
        when there is one."""
        posts = postings_by_company().get(key)
        if not posts:
            raise HTTPException(status_code=404, detail="no postings for that company")
        p = next((x for x in posts if x["id"] == pid), None) or \
            next((x for x in posts if x.get("stage") in ("saved", "applying")), None) or posts[0]
        result = p.get("result") or {}
        strengths = [c.replace("_", " ") for c in (result.get("report") or {}).get("emphasis", [])][:3]
        return {"company": p.get("company", ""), "title": p.get("title", ""), "url": p.get("url", ""),
                "ref": job_ref(p.get("url", ""), p.get("text", "")),
                "blurb": referral_blurb(person(), p.get("title", ""), p.get("company", ""), strengths),
                "resume_url": f"/api/resume?id={result['id']}" if result.get("id") else "",
                "postings": [{"id": x["id"], "title": x.get("title", ""), "stage": x.get("stage", "")} for x in posts]}

    # ---- the person's ties ----
    @app.get("/api/network/ties", dependencies=auth)
    def get_ties() -> dict:
        from ..network import ties_from_profile

        o = overrides()
        current = {t.label: t for t in load_ties(dict(o, network_off=[]), profile)}
        mine = {str(t.get("label", "")) for t in o.get("network_ties") or []}
        from_profile = {t.label for t in ties_from_profile(profile)} if o.get("network_profile_ties", True) else set()
        return {"kinds": list(TIE_KINDS), "off": o.get("network_off") or [],
                "ties": [{"label": t.label, "short": t.short, "kind": t.kind, "search": t.search, "weight": t.weight,
                          "words": t.words, "within": t.within, "alumni_page": t.alumni_page,
                          "custom": t.label in mine and t.label not in from_profile} for t in current.values()]}

    @app.put("/api/network/ties", dependencies=auth)
    def put_ties(body: TiesIn) -> dict:
        custom = []
        before = {str(x.get("label", "")): x for x in overrides().get("network_ties") or []}
        for t in body.custom[:40]:
            label = str(t.get("label", "")).strip()[:80]
            if not label:
                continue
            kind = t.get("kind") if t.get("kind") in TIE_KINDS else "club"
            words = [w.strip().lower() for w in (t.get("words") or [label]) if str(w).strip()][:8]
            item = {"label": label, "kind": kind, "search": str(t.get("search") or label).strip()[:80],
                    "weight": max(0.0, min(3.0, float(t.get("weight", 1.5)))), "words": words or [label.lower()]}
            for extra in ("short", "within", "alumni_page"):  # kept from before when the page does not send them
                value = t.get(extra, before.get(label, {}).get(extra))
                if value:
                    item[extra] = str(value)[:80]
            custom.append(item)
        o = overrides()
        o["network_ties"] = custom
        o["network_weights"] = {k: max(0.0, min(3.0, float(v))) for k, v in body.weights.items()}
        o["network_off"] = [str(x) for x in body.off][:40]
        overrides_path.parent.mkdir(parents=True, exist_ok=True)
        overrides_path.write_text(json.dumps(o, indent=2, ensure_ascii=False), encoding="utf-8")
        return get_ties()
