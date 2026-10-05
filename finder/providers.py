"""Every external service, behind one small interface each, plus the
fallback chain that moves to the next service when one runs out.

Three kinds:
  search    search(query, n, site)        -> list of {title, url, body}
  finder    find(person, domain, company)  -> FinderHit or None
  verifier  verify(email)                  -> "valid" | "invalid" | "catch_all" | "unknown"
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

from .core import Person, SmtpProbe, mx_hosts


# --------------------------------------------------------------------------
# Errors that drive the fallback
# --------------------------------------------------------------------------

class QuotaExceeded(Exception):
    """Free credits used up. Service is switched off until next month."""


class RateLimited(Exception):
    """Too many requests right now. Retried once, then skipped for this run."""


class AuthError(Exception):
    """Key missing or rejected. Skipped for this run."""


class Unavailable(Exception):
    """Can't be used from here at all (e.g. port 25 blocked). Skipped for this run."""


class ProviderError(Exception):
    """Anything else: server error, odd response. Skip this one call."""


QUOTA_WORDS = (
    "insufficient credit", "insufficient_credit", "not enough credit", "out of credit",
    "ran out of credit", "no credits", "usage limit", "quota", "credit limit",
    "limit reached", "exceeded your", "plan limit", "upgrade your plan",
)


def has_quota_words(text: str) -> bool:
    t = (text or "").lower()
    return any(w in t for w in QUOTA_WORDS)


def normalize_status(raw: str | None) -> str:
    s = (raw or "").strip().lower().replace(" ", "_")
    if s in {"valid", "safe", "deliverable", "inbox_full", "abuse", "verified"}:
        return "valid"
    if s in {"invalid", "undeliverable", "disabled", "disposable", "spamtrap"}:
        return "invalid"
    if s in {"catch_all", "catch-all", "accept_all", "acceptall", "accept-all"}:
        return "catch_all"
    return "unknown"


@dataclass
class FinderHit:
    email: str
    verified: bool
    score: float  # 0..1


@dataclass
class Meter:
    key: str
    cap: float
    period: str = "month"  # or "lifetime"


# --------------------------------------------------------------------------
# Base class
# --------------------------------------------------------------------------

class Provider:
    name = ""
    label = ""
    kind = ""
    env: list[str] = []
    meters: dict[str, Meter] = {}
    costs: dict[str, float] = {}
    signup = ""
    free_note = ""
    timeout = 25

    def __init__(self, keys: dict, usage, http=None):
        self.keys = keys
        self.usage = usage
        self.http = http or requests.Session()
        self.session_off: str | None = None
        self.run_spent = 0.0
        self.timeouts = 0

    # ---- credit bookkeeping -------------------------------------------
    def configured(self) -> bool:
        return all(self.keys.get(k) for k in self.env)

    def meter(self, op: str) -> Meter | None:
        return self.meters.get(op)

    def cap(self, op: str) -> float:
        m = self.meter(op)
        if not m:
            return float("inf")
        override = os.environ.get(f"CAP_{m.key.upper()}")
        try:
            return float(override) if override else m.cap
        except ValueError:
            return m.cap

    def used(self, op: str) -> float:
        m = self.meter(op)
        return self.usage.used(m.key, m.period) if m else 0.0

    def remaining(self, op: str) -> float:
        return self.cap(op) - self.used(op)

    def cost(self, op: str) -> float:
        return self.costs.get(op, 1.0)

    def status(self, op: str) -> str:
        if not self.configured():
            return "no key"
        if self.usage.is_exhausted(self.name):
            return "out of credits"
        if self.session_off:
            return self.session_off
        if self.remaining(op) < self.cost(op):
            return "free limit reached"
        return "ready"

    def available(self, op: str) -> bool:
        return self.status(op) == "ready"

    def charge(self, op: str, cost: float | None = None) -> None:
        cost = self.cost(op) if cost is None else cost
        m = self.meter(op)
        if m:
            self.usage.add(m.key, cost, m.period)
        self.run_spent += cost

    # ---- HTTP -----------------------------------------------------------
    def request(self, method: str, url: str, **kw) -> requests.Response:
        try:
            resp = self.http.request(method, url, timeout=kw.pop("timeout", self.timeout), **kw)
        except requests.RequestException as exc:
            raise ProviderError(f"network error: {exc}") from exc
        self.raise_for(resp)
        return resp

    def raise_for(self, resp) -> None:
        code, text = resp.status_code, resp.text or ""
        if 200 <= code < 300:
            return
        if code == 402 or has_quota_words(text):
            raise QuotaExceeded(text[:200])
        if code == 429:
            raise RateLimited(text[:200])
        if code in (401, 403):
            raise AuthError(text[:200])
        raise ProviderError(f"HTTP {code}: {text[:200]}")


def _dig(obj: Any, *path, default=None):
    for p in path:
        if isinstance(obj, dict):
            obj = obj.get(p)
        else:
            return default
    return default if obj is None else obj


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------

class SerperSearch(Provider):
    name, label, kind = "serper", "Serper (Google)", "search"
    env = ["SERPER_API_KEY"]
    meters = {"search": Meter("serper", 2500, "lifetime")}
    signup = "https://serper.dev"
    free_note = "2,500 queries, one time, no card"

    def __init__(self, keys, usage, http=None):
        super().__init__(keys, usage, http)
        # Serper's free plan rejects search operators (site:, "quotes").
        # Once we see that, we send plain-word queries for the rest of the run.
        self.plain = False

    @staticmethod
    def plain_query(query: str, site: str | None) -> str:
        q = query.replace('"', "").replace("@", " ")
        if site:
            q += " " + site.split(".")[0]   # "linkedin.com/in" -> "linkedin"
        return " ".join(q.split())

    def search(self, query: str, n: int = 10, site: str | None = None,
               page: int = 1) -> list[dict]:
        num = 10 if n <= 10 else 20
        for plain in ([True] if self.plain else [False, True]):
            q = self.plain_query(query, site) if plain else (f"site:{site} {query}" if site else query)
            body = {"q": q, "num": num}
            if page > 1:
                body["page"] = page
            try:
                r = self.request("POST", "https://google.serper.dev/search", json=body,
                                 headers={"X-API-KEY": self.keys["SERPER_API_KEY"]})
                break
            except ProviderError as exc:
                if "pattern not allowed" not in str(exc).lower():
                    raise
                if plain:
                    raise Unavailable("free plan rejects these searches") from exc
                self.plain = True
        self.charge("search", 1 if num <= 10 else 2)
        rows = [{"title": x.get("title", ""), "url": x.get("link", ""),
                 "body": x.get("snippet", "")} for x in r.json().get("organic", [])]
        if site and self.plain:  # no site: filter on the query, so filter here
            rows = [x for x in rows if site in x["url"]]
        return rows


class TavilySearch(Provider):
    name, label, kind = "tavily", "Tavily", "search"
    env = ["TAVILY_API_KEY"]
    meters = {"search": Meter("tavily", 1000)}
    signup = "https://tavily.com"
    free_note = "1,000 credits a month, no card"

    def raise_for(self, resp) -> None:
        if resp.status_code in (432, 433):
            raise QuotaExceeded(resp.text[:200])
        super().raise_for(resp)

    def search(self, query: str, n: int = 10, site: str | None = None,
               page: int = 1) -> list[dict]:
        if page > 1:
            return []  # Tavily has no result pages; the chain moves to the next service
        # Same 1 credit for up to 20 results, so always ask for 20
        body = {"query": query, "max_results": 20, "search_depth": "basic"}
        if site:
            body["include_domains"] = [site.split("/")[0]]
        r = self.request("POST", "https://api.tavily.com/search", json=body,
                         headers={"Authorization": f"Bearer {self.keys['TAVILY_API_KEY']}"})
        self.charge("search")
        rows = r.json().get("results", [])
        if site and "/" in site:  # include_domains can't take a path; filter here
            rows = [x for x in rows if site in x.get("url", "")]
        return [{"title": x.get("title", ""), "url": x.get("url", ""),
                 "body": x.get("content", "")} for x in rows]


class BraveSearch(Provider):
    name, label, kind = "brave", "Brave Search", "search"
    env = ["BRAVE_API_KEY"]
    # Brave gives $5 credit a month (~1,000 queries) but keeps a card on file,
    # so we stop a little early to stay clear of charges.
    meters = {"search": Meter("brave", 900)}
    signup = "https://api-dashboard.search.brave.com"
    free_note = "$5 credit a month (~1,000 queries), card required"

    def search(self, query: str, n: int = 10, site: str | None = None,
               page: int = 1) -> list[dict]:
        import re
        if page > 10:
            return []
        q = f"site:{site} {query}" if site else query
        r = self.request("GET", "https://api.search.brave.com/res/v1/web/search",
                         params={"q": q, "count": min(n, 20), "offset": page - 1},
                         headers={"X-Subscription-Token": self.keys["BRAVE_API_KEY"],
                                  "Accept": "application/json"})
        self.charge("search")
        strip = lambda s: re.sub(r"<[^>]+>", "", s or "")
        return [{"title": strip(x.get("title")), "url": x.get("url", ""),
                 "body": strip(x.get("description"))}
                for x in r.json().get("web", {}).get("results", [])]


class DuckDuckGoSearch(Provider):
    name, label, kind = "ddg", "DuckDuckGo (no key)", "search"
    env = []
    free_note = "no key; can be rate-limited"

    def search(self, query: str, n: int = 10, site: str | None = None,
               page: int = 1) -> list[dict]:
        try:
            from ddgs import DDGS
        except ImportError as exc:
            raise Unavailable("ddgs not installed") from exc
        q = f"site:{site} {query}" if site else query
        try:
            rows = DDGS().text(q, max_results=n, page=page) or []
        except Exception as exc:
            msg = str(exc).lower()
            if "no results" in msg:
                return []
            if "ratelimit" in msg or "rate limit" in msg or "202" in msg:
                raise RateLimited(str(exc)) from exc
            raise ProviderError(str(exc)) from exc
        return [{"title": r.get("title", ""), "url": r.get("href", ""),
                 "body": r.get("body", "")} for r in rows]


# --------------------------------------------------------------------------
# Email finders
# --------------------------------------------------------------------------

class ProspeoFinder(Provider):
    name, label, kind = "prospeo", "Prospeo", "finder"
    env = ["PROSPEO_API_KEY"]
    meters = {"find": Meter("prospeo", 75)}
    signup = "https://prospeo.io"
    free_note = "75 credits a month; only charges when it finds someone"

    def find(self, person: Person, domain: str, company: str = "") -> FinderHit | None:
        data = {"first_name": person.first.title(), "last_name": person.last.title(),
                "company_website": domain}
        if company:
            data["company_name"] = company
        if person.url:
            data["linkedin_url"] = person.url
        try:
            resp = self.http.request(
                "POST", "https://api.prospeo.io/enrich-person", timeout=self.timeout,
                json={"only_verified_email": True, "data": data},
                headers={"X-KEY": self.keys["PROSPEO_API_KEY"], "Content-Type": "application/json"})
        except requests.RequestException as exc:
            raise ProviderError(str(exc)) from exc
        text = resp.text or ""
        if "NO_MATCH" in text or "INVALID_DATAPOINTS" in text:
            return None                      # Prospeo doesn't charge for misses
        if "INSUFFICIENT_CREDITS" in text:
            raise QuotaExceeded("insufficient credits")
        if "INVALID_API_KEY" in text:
            raise AuthError("invalid key")
        self.raise_for(resp)
        email_obj = _dig(resp.json(), "person", "email", default={}) or {}
        email = email_obj.get("email") if isinstance(email_obj, dict) else None
        if not email or "*" in email:
            return None
        self.charge("find")
        verified = str(email_obj.get("status", "")).upper() == "VERIFIED"
        return FinderHit(email.lower(), verified, 1.0 if verified else 0.6)


class HunterFinder(Provider):
    name, label, kind = "hunter", "Hunter", "finder"
    env = ["HUNTER_API_KEY"]
    meters = {"find": Meter("hunter", 50), "verify": Meter("hunter", 50)}
    costs = {"find": 1.0, "verify": 0.5}
    signup = "https://hunter.io"
    free_note = "50 credits a month (find 1, verify 0.5)"

    def raise_for(self, resp) -> None:
        # Hunter: 403 = going too fast, 429 = monthly usage used up
        if resp.status_code == 429:
            raise QuotaExceeded(resp.text[:200])
        if resp.status_code == 403:
            raise RateLimited(resp.text[:200])
        super().raise_for(resp)

    def find(self, person: Person, domain: str, company: str = "") -> FinderHit | None:
        r = self.request("GET", "https://api.hunter.io/v2/email-finder",
                         params={"domain": domain, "first_name": person.first,
                                 "last_name": person.last,
                                 "api_key": self.keys["HUNTER_API_KEY"]})
        d = r.json().get("data") or {}
        if not d.get("email"):
            return None
        self.charge("find")
        verified = _dig(d, "verification", "status", default="") == "valid"
        return FinderHit(d["email"].lower(), verified, (d.get("score") or 50) / 100)

    def verify(self, email: str) -> str:
        r = self.request("GET", "https://api.hunter.io/v2/email-verifier",
                         params={"email": email, "api_key": self.keys["HUNTER_API_KEY"]})
        if r.status_code == 202:   # still checking; don't wait on it
            return "unknown"
        self.charge("verify")
        status = _dig(r.json(), "data", "status", default="")
        return "unknown" if status == "webmail" else normalize_status(status)


class TombaFinder(Provider):
    name, label, kind = "tomba", "Tomba", "finder"
    env = ["TOMBA_API_KEY", "TOMBA_SECRET"]
    meters = {"find": Meter("tomba_search", 50), "verify": Meter("tomba_verify", 100)}
    signup = "https://tomba.io"
    free_note = "50 searches + 100 verifications a month"
    timeout = 60  # Tomba's live lookups are slow

    def _headers(self) -> dict:
        return {"X-Tomba-Key": self.keys["TOMBA_API_KEY"],
                "X-Tomba-Secret": self.keys["TOMBA_SECRET"]}

    def find(self, person: Person, domain: str, company: str = "") -> FinderHit | None:
        r = self.request("GET", "https://api.tomba.io/v1/email-finder",
                         params={"domain": domain, "first_name": person.first,
                                 "last_name": person.last}, headers=self._headers())
        d = r.json().get("data") or {}
        email = d.get("email")
        if not email:
            return None
        self.charge("find")
        status = _dig(d, "verification", "status", default="")
        score = d.get("score") or 50
        return FinderHit(email.lower(), normalize_status(status) == "valid", float(score) / 100)

    def verify(self, email: str) -> str:
        r = self.request("GET", "https://api.tomba.io/v1/email-verifier",
                         params={"email": email}, headers=self._headers())
        self.charge("verify")
        e = _dig(r.json(), "data", "email", default={}) or {}
        if isinstance(e, dict):
            if e.get("accept_all"):
                return "catch_all"
            for key in ("status", "result"):
                v = normalize_status(e.get(key))
                if v != "unknown":
                    return v
        return "unknown"


# --------------------------------------------------------------------------
# Verifiers
# --------------------------------------------------------------------------

class ReoonVerifier(Provider):
    name, label, kind = "reoon", "Reoon", "verifier"
    env = ["REOON_API_KEY"]
    meters = {"verify": Meter("reoon", 600)}
    signup = "https://emailverifier.reoon.com"
    free_note = "600 verifications a month, no card"
    timeout = 90  # power mode can take up to a minute

    def verify(self, email: str) -> str:
        r = self.request("GET", "https://emailverifier.reoon.com/api/v1/verify",
                         params={"email": email, "key": self.keys["REOON_API_KEY"],
                                 "mode": "power"})
        data = r.json()
        if str(data.get("status", "")).lower() == "error":
            reason = str(data.get("reason", "")) + str(data.get("message", ""))
            if has_quota_words(reason) or "credit" in reason.lower():
                raise QuotaExceeded(reason)
            if "key" in reason.lower():
                raise AuthError(reason)
            raise ProviderError(reason)
        self.charge("verify")
        status = str(data.get("status", "")).lower()
        if status == "role_account":
            return "unknown"
        return normalize_status(status)


class ZeroBounceVerifier(Provider):
    name, label, kind = "zerobounce", "ZeroBounce", "verifier"
    env = ["ZEROBOUNCE_API_KEY"]
    meters = {"verify": Meter("zerobounce", 100)}
    signup = "https://www.zerobounce.net"
    free_note = "100 a month (sign up with a business email)"
    timeout = 60

    def verify(self, email: str) -> str:
        r = self.request("GET", "https://api.zerobounce.net/v2/validate",
                         params={"api_key": self.keys["ZEROBOUNCE_API_KEY"],
                                 "email": email, "ip_address": ""})
        data = r.json()
        if data.get("error"):
            # ZeroBounce uses one message for both a bad key and no credits
            raise QuotaExceeded(str(data["error"]))
        self.charge("verify")
        status = str(data.get("status", "")).lower()
        if status == "do_not_mail":
            return "unknown"
        return normalize_status(status)


class TombaVerifier(TombaFinder):
    """Same Tomba account; verifications have their own monthly allowance."""
    name, label, kind = "tomba_verify", "Tomba verifier", "verifier"


class HunterVerifier(HunterFinder):
    """Same Hunter account. Shares the name, so finding and verifying draw
    from (and run out of) one pool of credits together."""
    label, kind = "Hunter verifier", "verifier"


class SmtpVerifier(Provider):
    name, label, kind = "smtp", "Local SMTP check", "verifier"
    env = []
    free_note = "free and unlimited, needs open port 25"

    def __init__(self, keys, usage, http=None):
        super().__init__(keys, usage, http)
        self._probes: dict[str, SmtpProbe] = {}
        self._catch_all: dict[str, bool | None] = {}

    def verify(self, email: str) -> str:
        domain = email.split("@")[1]
        probe = self._probes.get(domain)
        if probe is None:
            mx = mx_hosts(domain)
            if not mx:
                return "unknown"
            probe = SmtpProbe(mx[0])
            self._probes[domain] = probe
            try:
                self._catch_all[domain] = probe.is_catch_all(domain)
            except ConnectionError as exc:
                raise Unavailable(str(exc)) from exc
        if self._catch_all.get(domain):
            return "catch_all"
        if self._catch_all.get(domain) is None:
            return "unknown"
        try:
            return probe.check(email)
        except ConnectionError as exc:
            raise Unavailable(str(exc)) from exc

    def close(self) -> None:
        for p in self._probes.values():
            p.close()


# --------------------------------------------------------------------------
# Fallback chain
# --------------------------------------------------------------------------

@dataclass
class ChainResult:
    value: Any
    provider: Provider | None
    tried_any: bool


class Chain:
    def __init__(self, providers: list[Provider], op: str, log: Callable[[str], None]):
        self.providers, self.op, self.log = providers, op, log

    def any_available(self) -> bool:
        return any(p.available(self.op) for p in self.providers)

    def call(self, method: str, *args, accept: Callable[[Any], bool] | None = None,
             **kwargs) -> ChainResult:
        """Try providers in order. Move on when one is out of credits, broken,
        rate-limited, or returns something `accept` rejects (e.g. no match)."""
        fallback, tried = None, False
        for p in self.providers:
            if not p.available(self.op):
                continue
            tried = True
            for attempt in (1, 2):
                try:
                    value = getattr(p, method)(*args, **kwargs)
                except QuotaExceeded:
                    self.usage_off(p)
                    break
                except RateLimited:
                    if attempt == 1:
                        wait = 15 if p.name == "ddg" else 4
                        self.log(f"{p.label} is rate-limiting; waiting {wait}s")
                        time.sleep(wait)
                        continue
                    p.session_off = "rate-limited"
                    self.log(f"{p.label} still rate-limited, skipping it for this run")
                    break
                except AuthError as exc:
                    p.session_off = "key rejected"
                    self.log(f"{p.label} rejected the API key ({exc}). Check it in the sidebar.")
                    break
                except Unavailable as exc:
                    p.session_off = "unavailable here"
                    self.log(f"{p.label} unavailable: {exc}")
                    break
                except ProviderError as exc:
                    if isinstance(exc.__cause__, requests.Timeout):
                        p.timeouts += 1
                        if p.timeouts >= 2:
                            p.session_off = "not responding"
                            self.log(f"{p.label} timed out twice, skipping it for the rest of this run")
                        else:
                            self.log(f"{p.label} took too long to answer, moving on")
                    else:
                        self.log(f"{p.label} error: {exc}")
                    break
                if accept is None or accept(value):
                    return ChainResult(value, p, True)
                if fallback is None:
                    fallback = ChainResult(value, p, True)
                break
        return fallback or ChainResult(None, None, tried)

    def usage_off(self, p: Provider) -> None:
        p.usage.mark_exhausted(p.name)
        nxt = [q.label for q in self.providers if q is not p and q.available(self.op)]
        tail = f"switching to {nxt[0]}" if nxt else "no backup left for this step"
        self.log(f"{p.label} is out of free credits for this month, {tail}")


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

SEARCH_PROVIDERS = [SerperSearch, TavilySearch, BraveSearch, DuckDuckGoSearch]
FINDER_PROVIDERS = [ProspeoFinder, HunterFinder, TombaFinder]
VERIFIER_PROVIDERS = [ReoonVerifier, ZeroBounceVerifier, TombaVerifier, HunterVerifier, SmtpVerifier]

ALL_KEYS = [
    ("SERPER_API_KEY", "Serper"), ("TAVILY_API_KEY", "Tavily"), ("BRAVE_API_KEY", "Brave Search"),
    ("PROSPEO_API_KEY", "Prospeo"), ("HUNTER_API_KEY", "Hunter"),
    ("TOMBA_API_KEY", "Tomba key"), ("TOMBA_SECRET", "Tomba secret"),
    ("REOON_API_KEY", "Reoon"), ("ZEROBOUNCE_API_KEY", "ZeroBounce"),
]


class Registry:
    def __init__(self, keys: dict, usage, log: Callable[[str], None] = print,
                 http=None, use_ddg: bool = True, use_smtp: bool = True):
        self.log = log
        mk = lambda cls: cls(keys, usage, http)
        self.searchers = [mk(c) for c in SEARCH_PROVIDERS if use_ddg or c is not DuckDuckGoSearch]
        self.finders = [mk(c) for c in FINDER_PROVIDERS]
        self.verifiers = [mk(c) for c in VERIFIER_PROVIDERS if use_smtp or c is not SmtpVerifier]
        self.search = Chain(self.searchers, "search", log)
        self.find = Chain(self.finders, "find", log)
        self.verify = Chain(self.verifiers, "verify", log)

    def all(self) -> list[Provider]:
        return self.searchers + self.finders + self.verifiers

    def spent_this_run(self) -> dict[str, float]:
        return {p.label: p.run_spent for p in self.all() if p.run_spent}

    def close(self) -> None:
        for p in self.verifiers:
            if isinstance(p, SmtpVerifier):
                p.close()
