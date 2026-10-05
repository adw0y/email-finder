"""The workflow: find people, learn the company's email format as cheaply as
possible, then confirm each address. Credits are spent in this order:

  free     company website + local guesses
  cheap    one search query to find published addresses
  scarce   finder credits (Prospeo / Hunter / Tomba), only when the format
           isn't known yet or every guess was rejected
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .core import (Person, PatternEvidence, crawl_site, extract_emails, host_of,
                   learn_from_email, looks_like_company_site, mail_provider, mx_hosts,
                   parse_linkedin_result, pattern_from_email, render)
from .providers import Registry

VERIFIED = "Verified"
FOUND = "Found, unverified"
CATCH_ALL = "Catch-all domain"
GUESS = "Best guess"
NOT_FOUND = "Not found"


@dataclass
class Target:
    company: str
    domain: str = ""
    title: str = ""
    limit: int = 10
    people: list[Person] | None = None  # supplied names skip the search
    locations: list[str] = field(default_factory=list)  # optional, e.g. ["Bengaluru", "Mumbai"]


@dataclass
class Settings:
    mode: str = "saver"          # "saver" or "accurate"
    crawl: bool = True
    snippets: bool = True
    guesses_to_verify: int = 2


@dataclass
class Row:
    company: str
    domain: str
    name: str
    headline: str = ""
    linkedin: str = ""
    email: str = ""
    status: str = NOT_FOUND
    confidence: float = 0.0
    method: str = ""
    pattern: str = ""
    alternates: list[str] = field(default_factory=list)
    mail_host: str = ""
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "Company": self.company, "Domain": self.domain, "Name": self.name,
            "Headline": self.headline, "LinkedIn": self.linkedin, "Email": self.email,
            "Status": self.status, "Confidence": round(self.confidence, 2),
            "Found via": self.method, "Pattern": self.pattern,
            "Alternates": " ; ".join(self.alternates), "Mail host": self.mail_host,
            "Note": self.note,
        }


def guess_confidence(prob: float, strength: float) -> float:
    return round(min(0.25 + 0.45 * prob + 0.08 * min(strength, 3), 0.85), 2)


class Pipeline:
    def __init__(self, registry: Registry, settings: Settings,
                 log: Callable[[str], None] = print,
                 progress: Callable[[float, str], None] | None = None):
        self.reg = registry
        self.s = settings
        self._log = log
        self.progress = progress or (lambda f, t: None)
        self.events: list[str] = []
        registry.log = self.log
        for chain in (registry.search, registry.find, registry.verify):
            chain.log = self.log

    def log(self, msg: str) -> None:
        self.events.append(f"{datetime.now():%H:%M:%S}  {msg}")
        self._log(msg)

    # ------------------------------------------------------------------
    def run(self, targets: list[Target]) -> list[Row]:
        rows: list[Row] = []
        for i, t in enumerate(targets):
            self.progress(i / max(len(targets), 1), f"{t.company}")
            try:
                rows.extend(self.run_target(t, i, len(targets)))
            except Exception as exc:  # one bad company shouldn't kill the batch
                self.log(f"{t.company}: stopped on an unexpected error ({exc})")
        self.progress(1.0, "Done")
        self.reg.close()
        return rows

    def run_target(self, t: Target, idx: int = 0, total: int = 1) -> list[Row]:
        self.log(f"--- {t.company}" + (f" / {t.title}" if t.title else ""))
        domain = (t.domain or "").lower().strip().removeprefix("https://") \
            .removeprefix("http://").removeprefix("www.").split("/")[0]
        note = ""
        if not domain:
            domain = self.guess_domain(t.company)
            if not domain:
                self.log(f"Couldn't work out {t.company}'s domain. Add it in the Domain column.")
                return []
            note = f"domain guessed as {domain}"
            self.log(f"Domain looks like {domain} (guessed; check it)")

        people = t.people if t.people is not None else self.discover(t)
        self.log(f"{len(people)} people to look up")
        if not people:
            return []

        evidence = self.learn_pattern(domain, [p.name for p in people])
        mx = mx_hosts(domain)
        host = mail_provider(mx)
        if not mx:
            self.log(f"{domain} has no mail server records; addresses there won't work")

        rows = []
        for j, p in enumerate(people):
            self.progress((idx + j / len(people)) / total, f"{t.company}: {p.name}")
            row = self.resolve(p, domain, t.company, evidence)
            row.mail_host = host
            notes = [n for n in (note, row.note,
                                 "may no longer work there" if p.possibly_former else "") if n]
            row.note = "; ".join(notes)
            self.log(f"{p.name}: {row.email or 'nothing'}  [{row.status}, {row.confidence:.0%}]")
            rows.append(row)
        return rows

    # ------------------------------------------------------------------
    def guess_domain(self, company: str) -> str:
        res = self.reg.search.call("search", f"{company} official website", n=5)
        for r in res.value or []:
            if looks_like_company_site(r.get("url", "")):
                return host_of(r["url"])
        return ""

    @staticmethod
    def search_variants(t: Target) -> list[str]:
        """Different phrasings of the same search. Each surfaces a different
        slice of profiles. Locations come first since they're the most
        targeted; seniority variants only kick in for larger targets."""
        title, co = t.title, t.company
        queries = [f'"{title}" "{co}"']
        queries += [f'"{title}" "{co}" {loc}' for loc in t.locations]
        queries += [f"{title} {co}", f"{title} at {co}"]
        if t.limit > 30:
            queries += [f'"{level} {title}" "{co}"'
                        for level in ("Senior", "Lead", "Associate", "Principal")]
            queries += [f"{co} {title} {loc}" for loc in t.locations]
        return list(dict.fromkeys(queries))  # de-duplicate, keep order

    def discover(self, t: Target) -> list[Person]:
        if not self.reg.search.any_available():
            self.log("No search service available. Add a key or upload names.")
            return []
        people, seen = [], set()
        # Roughly 3 to 6 matching profiles come back per page of 10 results,
        # so page deeper for bigger targets. A query stops paging as soon as
        # a page adds nobody new.
        # Google serves about 10 pages per search, so big targets need more
        # distinct searches rather than deeper paging.
        max_pages = min(10, t.limit // 4 + 1)
        for query in self.search_variants(t):
            for page in range(1, max_pages + 1):
                res = self.reg.search.call("search", query, n=10, site="linkedin.com/in",
                                           page=page, accept=lambda rows: bool(rows))
                added = 0
                for row in res.value or []:
                    p = parse_linkedin_result(row, t.company, t.title)
                    if p and (p.first, p.last) not in seen:
                        seen.add((p.first, p.last))
                        people.append(p)
                        added += 1
                if res.provider:
                    self.log(f"Search page {page} via {res.provider.label}: "
                             f"+{added} people, {len(people)} so far")
                if len(people) >= t.limit:
                    return people[: t.limit]
                if not res.value or added == 0:
                    break
            if not self.reg.search.any_available():
                break
        if len(people) < t.limit:
            self.log(f"Found {len(people)} of the {t.limit} asked for. Searches only see "
                     "public profiles; a broader designation usually finds more.")
        return people

    def learn_pattern(self, domain: str, known: list[str]) -> PatternEvidence:
        ev = PatternEvidence()
        published: list[tuple[str, str]] = []
        if self.s.crawl:
            published += crawl_site(domain, log=self.log)
        if self.s.snippets and self.reg.search.any_available():
            res = self.reg.search.call("search", f'"@{domain}"', n=10)
            for r in res.value or []:
                published += extract_emails(f"{r['title']}. {r['body']}", domain)
        for email, ctx in published:
            learn_from_email(ev, email, ctx, known)
        top, prob = ev.ranked()[0]
        if ev.confirmed():
            ex = ", ".join(ev.examples.get(top, [])[:2])
            self.log(f"Email format looks like {top} ({ex})")
        else:
            self.log(f"Format not confirmed yet from {len(published)} published addresses")
        return ev

    # ------------------------------------------------------------------
    def resolve(self, p: Person, domain: str, company: str, ev: PatternEvidence) -> Row:
        base = Row(company=company, domain=domain, name=p.name,
                   headline=p.headline, linkedin=p.url)
        if self.s.mode == "accurate":
            row = self.try_finders(p, domain, company, ev, base)
            if row:
                return row
            row, _ = self.try_patterns(p, domain, ev, base)
            return row

        if ev.confirmed():
            row, all_rejected = self.try_patterns(p, domain, ev, base)
            if not all_rejected:
                return row
            return self.try_finders(p, domain, company, ev, base) or row
        row = self.try_finders(p, domain, company, ev, base)
        if row:
            return row
        row, _ = self.try_patterns(p, domain, ev, base)
        return row

    def try_finders(self, p, domain, company, ev, base: Row) -> Row | None:
        if not self.reg.find.any_available():
            return None
        res = self.reg.find.call("find", p, domain, company, accept=lambda h: h is not None)
        hit = res.value
        if not hit:
            return None
        if hit.email.endswith("@" + domain):
            pat = pattern_from_email(hit.email, p.name)
            if pat:
                ev.add_named(pat, hit.email, weight=1.5)
        row = self._copy(base, email=hit.email, pattern=pattern_from_email(hit.email, p.name) or "")
        if hit.verified:
            return self._copy(row, status=VERIFIED, confidence=0.95, method=res.provider.label)
        v = self.reg.verify.call("verify", hit.email, accept=lambda x: x != "unknown")
        how = res.provider.label + (f" + {v.provider.label}" if v.provider else "")
        conf = round(min(0.5 + 0.4 * hit.score, 0.85), 2)
        if v.value == "valid":
            return self._copy(row, status=VERIFIED, confidence=0.95, method=how)
        if v.value == "invalid":
            self.log(f"{res.provider.label}'s answer for {p.name} bounced in verification")
            return None
        if v.value == "catch_all":
            return self._copy(row, status=CATCH_ALL, confidence=conf, method=how)
        return self._copy(row, status=FOUND, confidence=conf, method=how)

    def try_patterns(self, p, domain, ev, base: Row) -> tuple[Row, bool]:
        """Returns (row, every_guess_was_rejected)."""
        seen, cands = set(), []
        for pat, prob in ev.ranked():
            email = f"{render(pat, p.first, p.last)}@{domain}"
            if email not in seen:
                seen.add(email)
                cands.append((email, pat, prob))

        rejected: list[str] = []
        for email, pat, prob in cands[: self.s.guesses_to_verify]:
            if not self.reg.verify.any_available():
                break
            v = self.reg.verify.call("verify", email, accept=lambda x: x != "unknown")
            how = f"Pattern + {v.provider.label}" if v.provider else "Pattern"
            if v.value == "valid":
                ev.add_named(pat, email)
                return self._copy(base, email=email, status=VERIFIED, confidence=0.95,
                                  method=how, pattern=pat,
                                  alternates=[c[0] for c in cands[:3] if c[0] != email][:2]), False
            if v.value == "catch_all":
                return self._copy(base, email=email, status=CATCH_ALL,
                                  confidence=guess_confidence(prob, ev.strength(pat)),
                                  method=how, pattern=pat,
                                  alternates=[c[0] for c in cands[1:3]]), False
            if v.value == "invalid":
                rejected.append(email)
                continue
            break  # unknown, or no verifier left: stop spending

        left = [c for c in cands if c[0] not in rejected]
        tried_all = bool(rejected) and len(rejected) >= min(self.s.guesses_to_verify, len(cands))
        email, pat, prob = left[0]
        note = f"{len(rejected)} guess(es) rejected by the mail server" if rejected else ""
        conf = guess_confidence(prob, ev.strength(pat)) * (0.6 if rejected else 1)
        row = self._copy(base, email=email, status=GUESS, confidence=round(conf, 2),
                         method="Pattern (unverified)", pattern=pat,
                         alternates=[c[0] for c in left[1:3]], note=note)
        return row, tried_all

    @staticmethod
    def _copy(row: Row, **changes) -> Row:
        data = {**row.__dict__, **changes}
        data["alternates"] = list(data.get("alternates") or [])
        return Row(**data)
