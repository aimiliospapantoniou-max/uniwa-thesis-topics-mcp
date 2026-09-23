import io
import os
import re
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader
from fastmcp import FastMCP

mcp = FastMCP("UNIWA Thesis Topics")
DOMAINS = ("uniwa.gr", "eee.uniwa.gr", "polynoe.lib.uniwa.gr", "edml.uniwa.gr")


def official(u: str) -> bool:
    h = urlparse(u).netloc.lower().split(":")[0]
    return any(h == d or h.endswith("." + d) for d in DOMAINS)


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def status(title: str, body: str) -> str:
    t = (title + " " + body).lower()
    cur = ["διαθέσιμο", "διαθέσιμη", "προτεινόμενο θέμα", "θέμα προς εκπόνηση",
           "available topic", "proposed topic", "προς εκπόνηση"]
    old = ["ολοκληρώθηκε", "ολοκληρωμένη", "περατώθηκε", "παρουσιάστηκε",
           "completed thesis", "date of presentation"]
    if any(x in t for x in cur) and not any(x in t for x in old):
        return "confirmed_available"
    if any(x in t for x in old):
        return "completed_or_historical"
    return "proposed_or_unconfirmed"


async def fetch(u: str):
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=25,
        headers={"User-Agent": "UNIWA-Thesis-Topics-MCP/1.1"}
    ) as c:
        r = await c.get(u)
        r.raise_for_status()
        ct = r.headers.get("content-type", "").lower()
        if "pdf" in ct or u.lower().endswith(".pdf"):
            rd = PdfReader(io.BytesIO(r.content))
            return "pdf", clean("\n".join((p.extract_text() or "") for p in rd.pages[:20]))
        s = BeautifulSoup(r.text, "html.parser")
        for x in s(["script", "style", "noscript"]):
            x.decompose()
        return "html", clean(s.get_text(" "))


def search_bing(q: str, n: int = 8):
    url = "https://www.bing.com/search?q=" + quote(q)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"}
    with httpx.Client(follow_redirects=True, timeout=20, headers=headers) as c:
        r = c.get(url)
        r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for item in soup.select("li.b_algo"):
        a = item.select_one("h2 a")
        if not a or not a.get("href"):
            continue
        snippet = item.select_one(".b_caption p")
        out.append({
            "title": clean(a.get_text(" ")),
            "url": a["href"],
            "snippet": clean(snippet.get_text(" ")) if snippet else ""
        })
        if len(out) >= n:
            break
    return out


def search_ddg(q: str, n: int = 8):
    try:
        from duckduckgo_search import DDGS
        with DDGS() as d:
            return [{"title": clean(x.get("title", "")), "url": x.get("href", ""),
                     "snippet": clean(x.get("body", ""))}
                    for x in d.text(q, max_results=n) if x.get("href")]
    except Exception:
        return []


def search(q: str, n: int = 8):
    try:
        r = search_bing(q, n)
        if r:
            return r
    except Exception:
        pass
    return search_ddg(q, n)


@mcp.tool
async def search_thesis_topics(professor: str = "", query: str = "", max_results: int = 10) -> dict[str, Any]:
    """Find diploma-thesis topics from official UNIWA/EEE sources."""
    professor, query = clean(professor), clean(query)
    n = max(1, min(max_results, 20))
    if not professor and not query:
        return {"status": "needs_input", "message": "Provide professor or topic query."}

    term = professor or query
    qs = [
        f'"{term}" site:eee.uniwa.gr διπλωματική',
        f'"{term}" site:eee.uniwa.gr "θέμα" διπλωματικής',
        f'"{term}" site:edml.uniwa.gr διπλωματική',
        f'"{term}" site:edml.uniwa.gr thesis',
        f'"{term}" site:uniwa.gr διπλωματική',
        f'"{term}" site:uniwa.gr "θέμα διπλωματικής"',
        f'"{term}" site:uniwa.gr filetype:pdf διπλωματική',
        f'"{term}" site:polynoe.lib.uniwa.gr διπλωματική',
    ]
    if query and professor:
        qs += [
            f'"{professor}" "{query}" site:eee.uniwa.gr',
            f'"{professor}" "{query}" site:edml.uniwa.gr',
            f'"{professor}" "{query}" site:uniwa.gr',
        ]

    raw, seen = [], set()
    for q in qs:
        try:
            results = search(q, 8)
        except Exception:
            results = []
        for x in results:
            if not x.get("url") or x["url"] in seen or not official(x["url"]):
                continue
            seen.add(x["url"])
            raw.append(x)
            if len(raw) >= n * 4:
                break
        if len(raw) >= n * 4:
            break

    out = []
    for x in raw:
        try:
            kind, body = await fetch(x["url"])
        except Exception:
            kind, body = "unreadable", ""
        title = x.get("title") or "Untitled source"
        m = re.search(r"(?:τίτλος|title|θέμα|topic)\s*[:\-]\s*(.{10,250})", body, re.I)
        if m:
            title = clean(m.group(1))
        out.append({
            "title": title,
            "professor": professor or "Not identified",
            "availability": status(title, body),
            "source_url": x["url"],
            "source_type": kind,
            "evidence": (x.get("snippet") or body[:600])[:600],
        })
        if len(out) >= n:
            break

    return {
        "status": "ok",
        "search": {"professor": professor, "query": query, "official_domains_only": True},
        "results": out,
        "warning": "Historical/completed theses are never presented as currently available; uncertain availability is marked proposed_or_unconfirmed."
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
