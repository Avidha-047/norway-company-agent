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
             "www.bing.com": 0.6, "w2.brreg.no": 0.2}


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

    def remaining(self):
        return self.max_requests - self.used

    def _take(self):
        with self.lock:
            if self.used >= self.max_requests:
                raise BudgetExceeded("request budget exhausted")
            if self.deadline and time.time() > self.deadline:
                raise BudgetExceeded("time limit reached")
            self.used += 1

    def get(self, url, accept="application/json", max_bytes=4_000_000, retries=2, timeout=None):
        key = (url, accept)
        if key in self.cache:
            return self.cache[key]
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
            self.cache[key] = res
            return res
