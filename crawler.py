import argparse
import hashlib
import html
import random
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import requests
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

# CONFIG
BASE = "https://www.goodreads.com"
QUEUE_FILE = Path("queue.txt")
VISITED_FILE = Path("visited.txt")
HTML_DIR = Path("html")

# Emulating Firefox
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:157.0) Gecko/20100101 Firefox/157.0"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}
TIMEOUT = (5, 20)  # (connect, read)
MIN_SLEEP, MAX_SLEEP = 3.0, 5.0
MAX_RETRIES = 3
MAX_CONSECUTIVE_ERRORS = 5  # after this many failed pages in a row the crawler stops
DROP_STATUSES = (404, 410)  # removed from queue for good, other failures go to the end of the queue
BOOK_LINK_WAIT_MS = 3000  # allow asynchronously rendered book links to appear

# Initial websites
SEEDS = [
    f"{BASE}/book/show/243333352-hell-to-pay",
    f"{BASE}/author/show/346732.George_R_R_Martin",
    f"{BASE}/choiceawards/best-books-2025",
    f"{BASE}/book/popular_by_date/2026/9",
    f"{BASE}/genres/fantasy",
]

# URLs to be added into queue
ALLOWED_PATHS = ("/book/show/", "/author/show/", "/genres/", "/choiceawards/", "/book/popular_by_date/",
                 "/list/show/", "/author/list/")

# Unwanted extensions
SKIP_EXT = re.compile(r"\.(jpg|jpeg|png|gif|svg|css|js|ico|pdf|zip|xml|json)$", re.I)
BOOK_PAGE_PATH = re.compile(r"^/book/show/\d+(?:[-.]|$)", re.I)

# Links
HREF_RE = re.compile(r'href\s*=\s*["\']([^"\']+)["\']', re.I)


# HELPER FUNCTIONS
def normalize(url: str) -> str:
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path.rstrip("/"), "", "", ""))


def is_wanted(url: str) -> bool:
    p = urlparse(url)
    if p.scheme not in ("http", "https") or p.netloc != urlparse(BASE).netloc:
        return False
    if SKIP_EXT.search(p.path):
        return False
    if p.path.startswith("/book/show/") and not BOOK_PAGE_PATH.match(p.path):
        return False
    return not ALLOWED_PATHS or p.path.startswith(ALLOWED_PATHS)


def is_book_page(url: str) -> bool:
    return bool(BOOK_PAGE_PATH.match(urlparse(url).path))


def extract_links(page: str, page_url: str):
    for raw in HREF_RE.findall(page):
        url = normalize(urljoin(page_url, html.unescape(raw)))
        if is_wanted(url):
            yield url


def read_lines(path: Path):
    return path.read_text(encoding="utf-8").split() if path.exists() else []


def write_lines(path: Path, lines):
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def append_line(path: Path, line: str):
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_robots(session):
    try:
        r = session.get(f"{BASE}/robots.txt", timeout=TIMEOUT)
    except requests.RequestException as e:
        print(f"[robots] request failed: {e}")
        return None

    rp = RobotFileParser()
    if r.status_code == 200:
        rp.parse(r.text.splitlines())
        return rp
    if r.status_code == 404:
        rp.parse([])
        return rp
    print(f"[robots] unexpected status {r.status_code}")
    return None


def download(session, url):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, timeout=TIMEOUT)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(5 * 2 ** attempt)
                continue
            return r.status_code, r.text
        except requests.RequestException:
            time.sleep(2 ** attempt)
    return None, None


def download_rendered(page, url):
    # Load a book page in Firefox so its JavaScript rendered links are available
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            if response is None:
                raise PlaywrightError("navigation returned no response")

            if response.status in (429, 500, 502, 503, 504):
                time.sleep(5 * 2 ** attempt)
                continue

            if response.status == 200:
                # Give JavaScript a short chance to add more book links after the
                # initial document is ready. If there are none, still save the page
                initial_count = page.locator('a[href*="/book/show/"]').count()
                try:
                    page.wait_for_function(
                        "initial => document.querySelectorAll('a[href*=\"/book/show/\"]').length > initial",
                        arg=initial_count,
                        timeout=BOOK_LINK_WAIT_MS,
                    )
                except PlaywrightTimeoutError:
                    pass

            return response.status, page.content()
        except PlaywrightError as e:
            if attempt == MAX_RETRIES:
                print(f"[browser] request failed for {url}: {e}")
            else:
                time.sleep(2 ** attempt)
    return None, None


# Main crawler function
def crawl(max_pages: int = 3000):
    HTML_DIR.mkdir(exist_ok=True)

    session = requests.Session()
    session.headers.update(HEADERS)
    robots = load_robots(session)
    if robots is None:
        print("Could not load robots.txt reliably (blocked or network problem). Stopping.")
        return
    delay = float(robots.crawl_delay(USER_AGENT) or robots.crawl_delay("*") or 0)
    lo, hi = max(MIN_SLEEP, delay), max(MAX_SLEEP, delay + 1)

    collected_books = 0  # book pages saved in THIS run only
    errors_in_row = 0
    with sync_playwright() as playwright:
        browser = playwright.firefox.launch()
        browser_page = browser.new_page(user_agent=USER_AGENT, locale="en-US")
        try:
            while collected_books < max_pages:
                # 1. load state from files
                visited = set(read_lines(VISITED_FILE))
                queue = [u for u in read_lines(QUEUE_FILE) if u not in visited]
                for s in SEEDS:  # only book seeds are fetched by this crawler
                    s = normalize(s)
                    if is_book_page(s) and s not in visited and s not in queue:
                        queue.append(s)

                # Discovery URLs from an older queue may appear before book pages.
                # Skip them and select the next book URL wherever it occurs.
                book_index = next((i for i, candidate in enumerate(queue)
                                   if is_book_page(candidate)), None)
                if book_index is None:
                    print("No book/show pages left in queue.")
                    break

                # 2. remove the selected book URL, leaving other queued URLs intact
                url = queue.pop(book_index)
                rest = queue

                if not robots.can_fetch(USER_AGENT, url):
                    print(f"[robots] skipping {url}")
                    write_lines(QUEUE_FILE, rest)
                    continue

                status, page = download_rendered(browser_page, url)

                if status == 200 and page:
                    errors_in_row = 0
                    (HTML_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".html")).write_text(
                        page, encoding="utf-8"
                    )
                    collected_books += 1

                    # Successful book pages are marked visited and kept as corpus data.
                    append_line(VISITED_FILE, url)
                    visited.add(url)
                    # Keep newly found book pages in the queue; discovery hubs are
                    # deliberately not fetched by this book-only crawler.
                    known = visited | set(rest)
                    for link in extract_links(page, url):
                        if is_book_page(link) and link not in known:
                            known.add(link)
                            rest.append(link)
                    write_lines(QUEUE_FILE, rest)
                    print(f"[{collected_books}/{max_pages}] BOOK  {url}  (queue: {len(rest)}, visited: {len(visited)})")
                else:
                    errors_in_row += 1
                    print(f"[error] {url} status={status} ({errors_in_row}/{MAX_CONSECUTIVE_ERRORS} in a row)")
                    if status not in DROP_STATUSES:
                        rest.append(url)  # not lost, will be retried at the end of the queue
                    write_lines(QUEUE_FILE, rest)
                    if errors_in_row >= MAX_CONSECUTIVE_ERRORS:
                        print("Too many consecutive errors - probably blocked. Stopping.")
                        break

                time.sleep(random.uniform(lo, hi))
        finally:
            browser.close()

    print(f"Finished. Book pages collected this run: {collected_books}")


