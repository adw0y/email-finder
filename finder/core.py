"""Name handling, email patterns, website crawling and local mail-server checks.

Nothing in this module spends API credits.
"""
from __future__ import annotations

import random
import re
import smtplib
import socket
import string
import time
import unicodedata
from dataclasses import dataclass, field
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) email-finder/2.0"
HTTP_TIMEOUT = 12

# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------

HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "prof", "er", "adv", "shri", "smt", "sir"}
CREDENTIALS = {
    "jr", "sr", "ii", "iii", "iv", "phd", "mba", "cfa", "cpa", "pmp", "md",
    "bsc", "msc", "btech", "mtech", "ca", "fca", "acca", "cs", "llb", "llm",
}


def fold(text: str) -> str:
    norm = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in norm if not unicodedata.combining(c)).lower()


def split_name(full_name: str) -> tuple[str, str] | None:
    """'Dr. Arjun K. Mehta, MBA' -> ('arjun', 'mehta'). None if unusable."""
    if not full_name:
        return None
    name = re.sub(r"\(.*?\)|\[.*?\]", " ", full_name)
    name = name.split(",")[0].replace("-", "")
    tokens = [re.sub(r"[^a-z]", "", fold(t)) for t in name.split()]
    tokens = [t for t in tokens if t]
    while tokens and tokens[0] in HONORIFICS:
        tokens.pop(0)
    while tokens and tokens[-1] in CREDENTIALS:
        tokens.pop()
    if len(tokens) < 2:
        return None
    return tokens[0], tokens[-1]


# --------------------------------------------------------------------------
# Email patterns
# --------------------------------------------------------------------------

# Rough starting priors. They only order guesses until real evidence arrives.
PATTERNS: list[tuple[str, float]] = [
    ("{first}.{last}", 0.40),
    ("{first}", 0.18),
    ("{f}{last}", 0.12),
    ("{first}{last}", 0.07),
    ("{first}_{last}", 0.04),
    ("{f}.{last}", 0.04),
    ("{first}{l}", 0.04),
    ("{last}.{first}", 0.03),
    ("{last}{f}", 0.02),
    ("{first}.{l}", 0.02),
    ("{last}", 0.02),
    ("{first}-{last}", 0.02),
]
PATTERN_NAMES = [p for p, _ in PATTERNS]

GENERIC_LOCALS = {
    "info", "hr", "contact", "careers", "career", "sales", "support", "hello",
    "press", "media", "admin", "jobs", "team", "office", "enquiry", "enquiries",
    "marketing", "accounts", "billing", "noreply", "no-reply", "privacy",
    "legal", "partnerships", "partners", "investors", "ir", "help", "feedback",
    "grievance", "grievances", "compliance", "care", "customercare", "hiring",
    "recruitment", "talent", "pr", "communications", "webmaster", "security",
    "dpo", "abuse", "postmaster", "ops", "finance", "procurement", "vendor",
    "business", "bd", "connect", "query", "queries", "nodalofficer", "nodal",
}


def render(pattern: str, first: str, last: str) -> str:
    return pattern.format(first=first, last=last, f=first[0], l=last[0])


def patterns_matching(local: str, first: str, last: str) -> list[str]:
    return [p for p in PATTERN_NAMES if render(p, first, last) == local]


def pattern_from_email(email: str, full_name: str) -> str | None:
    """Which pattern produced this person's address? None if ambiguous."""
    parts = split_name(full_name)
    if not parts or "@" not in email:
        return None
    hits = patterns_matching(email.split("@")[0].lower(), *parts)
    return hits[0] if len(hits) == 1 else None


@dataclass
class PatternEvidence:
    named: dict[str, float] = field(default_factory=dict)
    shape: dict[str, float] = field(default_factory=dict)
    examples: dict[str, list[str]] = field(default_factory=dict)
    forced: str | None = None

    def add_named(self, pattern: str, email: str, weight: float = 1.0) -> None:
        self.named[pattern] = self.named.get(pattern, 0) + weight
        ex = self.examples.setdefault(pattern, [])
        if email and email not in ex:
            ex.append(email)

    def add_shape(self, pattern: str, weight: float) -> None:
        self.shape[pattern] = self.shape.get(pattern, 0.0) + weight

    def ranked(self) -> list[tuple[str, float]]:
        if self.forced:
            return [(self.forced, 1.0)] + [(p, 0.0) for p in PATTERN_NAMES if p != self.forced]
        scores = {p: prior * (1 + 4 * self.named.get(p, 0) + self.shape.get(p, 0.0))
                  for p, prior in PATTERNS}
        total = sum(scores.values())
        return sorted(((p, s / total) for p, s in scores.items()), key=lambda x: -x[1])

    def strength(self, pattern: str) -> float:
        return self.named.get(pattern, 0)

    def confirmed(self) -> str | None:
        """A pattern backed by at least one real, name-matched address."""
        if self.forced:
            return self.forced
        top, _ = self.ranked()[0]
        return top if self.named.get(top, 0) >= 1 else None


SHAPE_RULES = [
    (re.compile(r"^[a-z]{2,}\.[a-z]{2,}$"), [("{first}.{last}", 1.0), ("{last}.{first}", 0.3)]),
    (re.compile(r"^[a-z]{2,}_[a-z]{2,}$"), [("{first}_{last}", 1.0)]),
    (re.compile(r"^[a-z]{2,}-[a-z]{2,}$"), [("{first}-{last}", 1.0)]),
    (re.compile(r"^[a-z]\.[a-z]{2,}$"), [("{f}.{last}", 1.0)]),
    (re.compile(r"^[a-z]{2,}\.[a-z]$"), [("{first}.{l}", 1.0)]),
]
NAME_NEAR = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z]\.?)?(?:\s+[A-Z][a-z]+){1,2})\b")


def learn_from_email(evidence: PatternEvidence, email: str, context: str,
                     known_names: list[str]) -> None:
    local = email.split("@")[0].lower()
    if local in GENERIC_LOCALS or any(ch.isdigit() for ch in local):
        return
    for cand in NAME_NEAR.findall(context or "") + list(known_names):
        parts = split_name(cand)
        if not parts:
            continue
        hits = patterns_matching(local, *parts)
        if len(hits) == 1:
            evidence.add_named(hits[0], email)
            return
    for rx, votes in SHAPE_RULES:
        if rx.match(local):
            for p, w in votes:
                evidence.add_shape(p, w)
            return


# --------------------------------------------------------------------------
# People parsing (from search results)
# --------------------------------------------------------------------------

@dataclass
class Person:
    name: str
    headline: str = ""
    url: str = ""
    possibly_former: bool = False
    first: str = ""
    last: str = ""

    @classmethod
    def from_name(cls, name: str, headline: str = "", url: str = "") -> "Person | None":
        parts = split_name(name)
        if not parts:
            return None
        return cls(name=name.strip(), headline=headline, url=url,
                   first=parts[0], last=parts[1])


ABBREVIATIONS = {
    "vp": "vice president", "svp": "senior vice president",
    "avp": "assistant vice president", "evp": "executive vice president",
    "sr": "senior", "jr": "junior", "mgr": "manager", "dir": "director",
    "pm": "product manager", "apm": "associate product manager",
    "hr": "human resources", "bd": "business development",
    "ceo": "chief executive officer", "cto": "chief technology officer",
    "cfo": "chief financial officer", "coo": "chief operating officer",
    "cmo": "chief marketing officer", "cpo": "chief product officer",
    "chro": "chief human resources officer", "gm": "general manager",
    "eng": "engineering", "mktg": "marketing", "ops": "operations",
}
TITLE_STOPWORDS = {"of", "the", "and", "at", "for", "in", "a", "an"}


def expand_title(text: str) -> str:
    return " ".join(ABBREVIATIONS.get(w, w) for w in re.findall(r"[a-z]+", fold(text)))


def title_matches(wanted: str, found: str, threshold: float = 0.6) -> bool:
    kws = [w for w in expand_title(wanted).split() if w not in TITLE_STOPWORDS]
    if not kws:
        return True
    hay = set(expand_title(found).split())
    return sum(1 for k in kws if k in hay) / len(kws) >= threshold


def company_mentioned(company: str, text: str) -> bool:
    c = fold(company).strip()
    return bool(c) and c in fold(text)


FORMER_RX = re.compile(r"\b(ex[-\s]|former|formerly|previously|past:|alumni|alum)\b", re.I)


def parse_linkedin_result(row: dict, company: str, title: str) -> Person | None:
    url = row.get("url", "")
    if "linkedin.com/in/" not in url:
        return None
    raw = re.sub(r"\s*[|\-–]\s*LinkedIn\s*$", "", row.get("title", ""), flags=re.I)
    parts = re.split(r"\s+[-–—|]\s+", raw)
    blob = f"{raw} {row.get('body', '')}"
    if not company_mentioned(company, blob) or not title_matches(title, blob):
        return None
    person = Person.from_name(parts[0], " - ".join(p.strip() for p in parts[1:]),
                              url.split("?")[0])
    if not person:
        return None
    for m in FORMER_RX.finditer(blob):
        if company_mentioned(company, blob[m.start(): m.start() + 60]):
            person.possibly_former = True
    return person


# --------------------------------------------------------------------------
# Published emails: company website and text snippets
# --------------------------------------------------------------------------

CRAWL_PATHS = [
    "", "about", "about-us", "team", "our-team", "leadership", "people",
    "contact", "contact-us", "press", "media", "newsroom", "careers",
    "investors", "privacy", "privacy-policy", "terms", "grievance",
    "grievance-redressal", "imprint",
]
NOT_COMPANY_SITES = {
    "linkedin.com", "wikipedia.org", "facebook.com", "instagram.com", "twitter.com",
    "x.com", "crunchbase.com", "glassdoor.com", "glassdoor.co.in", "ambitionbox.com",
    "naukri.com", "youtube.com", "zaubacorp.com", "tracxn.com", "bloomberg.com",
    "indeed.com", "google.com", "justdial.com", "economictimes.indiatimes.com",
    "inc42.com", "yourstory.com", "play.google.com", "apps.apple.com", "medium.com",
}


def host_of(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def looks_like_company_site(url: str) -> bool:
    host = host_of(url)
    return bool(host) and not any(host == d or host.endswith("." + d) for d in NOT_COMPANY_SITES)


def deobfuscate(text: str) -> str:
    text = re.sub(r"\s*[\[\(\{]\s*at\s*[\]\)\}]\s*", "@", text, flags=re.I)
    return re.sub(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", ".", text, flags=re.I)


def extract_emails(text: str, domain: str, window: int = 120) -> list[tuple[str, str]]:
    text = deobfuscate(text or "")
    rx = re.compile(r"[A-Za-z0-9._%+\-]+@" + re.escape(domain) + r"\b", re.I)
    out, seen = [], set()
    for m in rx.finditer(text):
        email = m.group(0).lower().strip(".")
        if email not in seen:
            seen.add(email)
            out.append((email, text[max(0, m.start() - window): m.end() + window]))
    return out


def html_to_text(html: str) -> str:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    mailtos = " ".join(a["href"][7:] for a in soup.find_all("a", href=True)
                       if a["href"].lower().startswith("mailto:"))
    return soup.get_text(" ", strip=True) + " " + mailtos


def crawl_site(domain: str, log=print, max_pages: int = 20) -> list[tuple[str, str]]:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    base = None
    for root in (f"https://{domain}/", f"https://www.{domain}/"):
        try:
            r = session.get(root, timeout=HTTP_TIMEOUT)
            if r.ok:
                base = r.url
                break
        except requests.RequestException:
            continue
    if not base:
        log(f"Couldn't reach {domain}'s website")
        return []
    robots = robotparser.RobotFileParser()
    try:
        robots.set_url(urljoin(base, "/robots.txt"))
        robots.read()
    except Exception:
        robots = None
    found: dict[str, str] = {}
    for path in CRAWL_PATHS[:max_pages]:
        url = urljoin(base, path)
        if robots and not robots.can_fetch(USER_AGENT, url):
            continue
        try:
            r = session.get(url, timeout=HTTP_TIMEOUT)
        except requests.RequestException:
            continue
        if r.ok and "html" in r.headers.get("content-type", ""):
            for email, ctx in extract_emails(html_to_text(r.text), domain):
                found.setdefault(email, ctx)
        time.sleep(0.5)
    return list(found.items())


# --------------------------------------------------------------------------
# DNS and local SMTP probe (the last-resort verifier)
# --------------------------------------------------------------------------

def mx_hosts(domain: str) -> list[str]:
    try:
        import dns.resolver
        answers = dns.resolver.resolve(domain, "MX", lifetime=8)
        return [str(r.exchange).rstrip(".") for r in sorted(answers, key=lambda r: r.preference)]
    except Exception:
        return []


def mail_provider(mx: list[str]) -> str:
    joined = " ".join(mx).lower()
    for needle, name in [
        ("google", "Google Workspace"), ("outlook.com", "Microsoft 365"),
        ("zoho", "Zoho Mail"), ("mimecast", "Mimecast"), ("pphosted", "Proofpoint"),
        ("barracuda", "Barracuda"), ("secureserver", "GoDaddy"), ("rediffmail", "Rediffmail"),
    ]:
        if needle in joined:
            return name
    return "other" if mx else "none"


class SmtpProbe:
    """Asks the mail server whether it would accept mail for an address.
    Never sends anything. Returns valid / invalid / unknown, or raises
    ConnectionError when port 25 is blocked."""

    def __init__(self, mx: str, timeout: int = 10):
        self.mx, self.timeout = mx, timeout
        self.server: smtplib.SMTP | None = None

    def _connect(self) -> None:
        try:
            self.server = smtplib.SMTP(self.mx, 25, timeout=self.timeout,
                                       local_hostname=socket.getfqdn() or "localhost")
            self.server.ehlo_or_helo_if_needed()
            self.server.mail("")
        except (OSError, smtplib.SMTPException) as exc:
            self.server = None
            raise ConnectionError(f"port 25 unreachable ({type(exc).__name__})") from exc

    def check(self, email: str) -> str:
        for _ in range(2):
            if self.server is None:
                self._connect()
            try:
                time.sleep(1.0)
                code, msg = self.server.rcpt(email)
                if code in (250, 251):
                    return "valid"
                if code in (550, 551, 553):
                    return "invalid"
                return "unknown"
            except smtplib.SMTPServerDisconnected:
                self.server = None
            except (OSError, smtplib.SMTPException):
                return "unknown"
        return "unknown"

    def is_catch_all(self, domain: str) -> bool | None:
        junk = "".join(random.choices(string.ascii_lowercase + string.digits, k=14))
        res = self.check(f"{junk}@{domain}")
        return None if res == "unknown" else res == "valid"

    def close(self) -> None:
        if self.server:
            try:
                self.server.quit()
            except Exception:
                pass
