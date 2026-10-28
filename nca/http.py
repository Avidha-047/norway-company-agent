"""Polite HTTP client with a hard request budget, deadline, retries and an in-run cache.

Every network attempt (including retries) counts against the budget, so the
daily 2,000-request limit can never be exceeded.
"""
import json
import random
import threading
import time
import urllib.error
import urllib.request

import os
import urllib.parse

USER_AGENT = "NorwayCompanyAgent/1.1 (open-data company research; contact: {})".format(
    os.environ.get("NCA_CONTACT", "see project repository"))
# Minimum seconds between requests to the same host (keeps us inside fair-use limits).
HOST_PACE = {"www.wikidata.org": 1.0, "query.wikidata.org": 2.0, "api.gleif.org": 0.25,
             "www.bing.com": 0.6, "w2.brreg.no": 0.5, "ws.geonorge.no": 0.1, "api.gdeltproject.org": 5.5}


class BudgetExceeded(Exception):
    pass


class Response:
    def __init__(self, url, status, body=b"", headers=None, final_url=None, error=None, ms=0):
        self.url = url
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.final_url = final_url or url
        self.error = error
        self.ms = ms

    @property
    def ok(self):
        return self.status == 200

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except Exception:
            return None

    def text(self):
        ctype = self.headers.get("Content-Type", "") or ""
        enc = "utf-8"
        if "charset=" in ctype:
            enc = ctype.split("charset=")[-1].split(";")[0].strip() or "utf-8"
        try:
            return self.body.decode(enc, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


class Http:
    def __init__(self, max_requests=2000, deadline=None, timeout=20):
        self.max_requests = max_requests
        self.deadline = deadline
        self.timeout = timeout
        self.used = 0
        self.lock = threading.Lock()
        self.cache = {}
        self.log = []
        self._next_slot = {}
        self.reserve = 0          # requests kept back for companies still waiting in the batch
        self.seen = {}            # url -> last response, used to hash and quote evidence
        self._local = threading.local()

    def _pace(self, url):
        host = urllib.parse.urlsplit(url).hostname or ""
        gap = HOST_PACE.get(host)
        if not gap:
            return
        with self.lock:
            now = time.time()
            slot = max(now, self._next_slot.get(host, 0))
            self._next_slot[host] = slot + gap
        if slot > now:
            time.sleep(slot - now)

    def begin_company(self):
        """Start tracking the responses fetched for one company (per thread). `got` holds this company's own
        references, so freeing shared bodies in another thread can never remove evidence this company needs."""
        self._local.urls = set()
        self._local.got = {}

    def company_lookup(self, url):
        got = getattr(self._local, "got", None)
        if got and url in got:
            return got[url]
        return self.seen.get(url)

    def end_company(self):
        """Free the response bodies of the company just finished (keeps memory flat in big batches)."""
        urls = getattr(self._local, "urls", None) or set()
        self._local.urls = None
        self._local.got = None
        if getattr(self, "keep_bodies", False):
            return
        with self.lock:
            for u in urls:
                self.seen.pop(u, None)
            for k in [k for k in list(self.cache) if k[0] in urls]:
                self.cache.pop(k, None)

    def forget(self, url):
        """Drop a cached response so the next get() fetches the URL again."""
        with self.lock:
            for k in [k for k in self.cache if k[0] == url]:
                self.cache.pop(k, None)
            self.seen.pop(url, None)
        got = getattr(self._local, "got", None)
        if got:
            got.pop(url, None)

    def remaining(self):
        return self.max_requests - self.used

    def spare(self, n=1):
        """True when n optional requests can be spent without starving later companies."""
        return self.remaining() - n >= self.reserve and not (self.deadline and time.time() > self.deadline - 120)

    def _take(self):
        with self.lock:
            if self.used >= self.max_requests:
                raise BudgetExceeded("request budget exhausted")
            if self.deadline and time.time() > self.deadline:
                raise BudgetExceeded("time limit reached")
            self.used += 1

    def get(self, url, accept="application/json", max_bytes=4_000_000, retries=2, timeout=None):
        key = (url, accept)
        with self.lock:
            res = self.cache.get(key)
        if res is not None:
            with self.lock:
                self.seen.setdefault(url, res)
            got = getattr(self._local, "got", None)
            if got is not None:
                got[url] = res
                if getattr(res, "final_url", None):
                    got.setdefault(res.final_url, res)
            return res
        attempt = 0
        while True:
            self._take()
            self._pace(url)
            t0 = time.time()
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                    body = r.read(max_bytes)
                    res = Response(url, r.status, body, dict(r.headers), r.geturl())
            except urllib.error.HTTPError as e:
                try:
                    body = e.read(200_000)
                except Exception:
                    body = b""
                res = Response(url, e.code, body, dict(e.headers or {}))
            except Exception as e:  # DNS, TLS, timeout, reset
                res = Response(url, 0, error=f"{type(e).__name__}: {e}")
            res.ms = int((time.time() - t0) * 1000)
            with self.lock:
                self.log.append({"url": url, "status": res.status, "ms": res.ms})
            if res.status in (0, 429, 500, 502, 503, 504) and attempt < retries:
                attempt += 1
                wait = res.headers.get("Retry-After")
                try:
                    wait = min(float(wait), 20.0)
                except (TypeError, ValueError):
                    wait = (3.0 if res.status == 429 else 0.8) * (2 ** attempt) + random.random()
                time.sleep(wait)
                continue
            with self.lock:
                self.cache[key] = res
            res.truncated = len(res.body or b"") >= max_bytes
            mine = getattr(self._local, "urls", None)
            if mine is not None:
                mine.update({url, res.final_url})
            got = getattr(self._local, "got", None)
            if got is not None:
                got[url] = res
                if res.final_url:
                    got.setdefault(res.final_url, res)
            with self.lock:
                self.seen[url] = res
                if res.final_url and res.final_url != url:
                    self.seen.setdefault(res.final_url, res)
            return res
