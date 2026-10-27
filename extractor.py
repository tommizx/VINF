import hashlib
import html
import json
import re
from pathlib import Path

HTML_DIR = Path("html")
VISITED_FILE = Path("visited.txt")
OUTPUT_FILE = Path("books.jsonl")

SCRIPT_RE = re.compile(
    r"<script\b[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script\s*>",
    re.I | re.S,
)
TITLE_RE = re.compile(
    r"<h1\b[^>]*data-testid=[\"']bookTitle[\"'][^>]*>(.*?)</h1\s*>", re.I | re.S
)
PUBLICATION_RE = re.compile(
    r"<p\b[^>]*data-testid=[\"']publicationInfo[\"'][^>]*>(.*?)</p\s*>",
    re.I | re.S,
)
DESCRIPTION_RE = re.compile(
    r"data-testid=[\"']contentContainer[\"'][^>]*>(.*?)(?:</div\s*>\s*</div\s*>\s*</div\s*>)",
    re.I | re.S,
)
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
YEAR_RE = re.compile(
    r"\b(?:first\s+published|published)\s+"
    r"(?:[A-Z][a-z]+\s+\d{1,2},?\s+)?(\d{4})\b",
    re.I,
)


def clean_text(value: str) -> str:
    # Remove markup and decode HTML entities from regex
    value = re.sub(r"<\s*br\s*/?\s*>", " ", value, flags=re.I)
    value = TAG_RE.sub(" ", value)
    return SPACE_RE.sub(" ", html.unescape(value)).strip()


def decode_json_string(value: str) -> str:
    value = re.sub(
        r"\\u([0-9a-fA-F]{4})", lambda match: chr(int(match.group(1), 16)), value
    )
    escapes = {
        '"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f",
        "n": "\n", "r": "\r", "t": "\t",
    }
    return re.sub(r'\\(["\\/bfnrt])', lambda match: escapes[match.group(1)], value)


def json_string(block: str, key: str):
    # Find first string-valued JSON-LD property named key
    match = re.search(
        rf'"{re.escape(key)}"\s*:\s*"((?:\\.|[^"\\])*)"', block, re.S
    )
    return html.unescape(decode_json_string(match.group(1))) if match else None


def json_number(block: str, key: str):
    match = re.search(
        rf'"{re.escape(key)}"\s*:\s*(-?\d+(?:\.\d+)?)', block
    )
    if not match:
        return None
    value = match.group(1)
    return float(value) if "." in value else int(value)


def json_authors(block: str):
    author_match = re.search(
        r'"author"\s*:\s*(\[[\s\S]*?\]|\{[\s\S]*?\})', block
    )
    if not author_match:
        return []
    names = re.findall(
        r'"name"\s*:\s*"((?:\\.|[^"\\])*)"', author_match.group(1), re.S
    )
    return [html.unescape(decode_json_string(name)) for name in names]


def json_name_list(block: str, key: str, value_pattern: str):
    array_match = re.search(
        rf'"{re.escape(key)}"\s*:\s*\[(.*?)\]\s*,\s*"details"',
        block,
        re.S,
    )
    if not array_match:
        return []
    names = re.findall(value_pattern, array_match.group(1), re.S)
    return [html.unescape(decode_json_string(name)) for name in names]


def extract_genres(page: str):
    return json_name_list(
        page,
        "bookGenres",
        r'"genre"\s*:\s*\{[^{}]*?"name"\s*:\s*"((?:\\.|[^"\\])*)"',
    )


def extract_series(page: str):
    # Goodreads stores a reference in bookSeries and the matching series title elsewhere in embedded page state
    book_series = re.search(r'"bookSeries"\s*:\s*\[(.*?)\]', page, re.S)
    if not book_series:
        return []
    series_ids = re.findall(r'"__ref"\s*:\s*"Series:([^"]+)"', book_series.group(1))
    titles = []
    for series_id in series_ids:
        match = re.search(
            r'"__typename"\s*:\s*"Series"\s*,\s*"id"\s*:\s*"'
            + re.escape(series_id)
            + r'"\s*,\s*"title"\s*:\s*"((?:\\.|[^"\\])*)"',
            page,
            re.S,
        )
        if match:
            titles.append(html.unescape(decode_json_string(match.group(1))))
    return titles


def extract_record(page: str, source_file: str, url_by_hash: dict):
    book_block = ""
    for script in SCRIPT_RE.findall(page):
        if re.search(r'"@type"\s*:\s*"Book"', script, re.I):
            book_block = script
            break

    title_match = TITLE_RE.search(page)
    title = clean_text(title_match.group(1)) if title_match else json_string(book_block, "name")

    description_match = DESCRIPTION_RE.search(page)
    description = clean_text(description_match.group(1)) if description_match else None
    if not description:
        meta_match = re.search(
            r'<meta\b[^>]*name=["\']description["\'][^>]*content=["\']([^"\']*)["\']',
            page,
            re.I | re.S,
        )
        description = html.unescape(meta_match.group(1)).strip() if meta_match else None

    publication_match = PUBLICATION_RE.search(page)
    publication_text = clean_text(publication_match.group(1)) if publication_match else ""
    year_match = YEAR_RE.search(publication_text)
    if year_match:
        year = int(year_match.group(1))
    else:
        published_date = json_string(book_block, "datePublished")
        year_match = re.search(r"\d{4}", published_date or "")
        year = int(year_match.group(0)) if year_match else None

    digest = Path(source_file).stem
    return {
        "source_file": source_file,
        "url": url_by_hash.get(digest),
        "title": title,
        "authors": json_authors(book_block),
        "year": year,
        "language": json_string(book_block, "inLanguage"),
        "isbn": json_string(book_block, "isbn"),
        "format": json_string(book_block, "bookFormat"),
        "description": description,
        "rating": json_number(book_block, "ratingValue"),
        "rating_count": json_number(book_block, "ratingCount"),
        "review_count": json_number(book_block, "reviewCount"),
        "page_count": json_number(book_block, "numberOfPages"),
        "genres": extract_genres(page),
        "series": extract_series(page),
    }


def load_url_map(visited_file: Path = VISITED_FILE):
    # Map the crawler's html filenames back to source urls
    urls = {}
    if visited_file.exists():
        for url in visited_file.read_text(encoding="utf-8", errors="replace").splitlines():
            url = url.strip()
            if url:
                urls[hashlib.sha1(url.encode("utf-8")).hexdigest()] = url
    return urls


def prepare_jsonl_for_append(path: Path):
    if not path.exists() or path.stat().st_size == 0:
        return
    with path.open("rb+") as output:
        output.seek(0, 2)
        end = output.tell()
        output.seek(end - 1)
        if output.read(1) == b"\n":
            return
        output.seek(0)
        content = output.read()
        last_newline = content.rfind(b"\n")
        output.truncate(last_newline + 1 if last_newline >= 0 else 0)


def load_processed_sources(path: Path):
    processed = set()
    if not path.exists():
        return processed
    with path.open("r", encoding="utf-8", errors="replace") as source:
        for line in source:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("source_file"):
                processed.add(record["source_file"])
    return processed


def extract_books(
    html_dir: Path = HTML_DIR,
    output_file: Path = OUTPUT_FILE,
    visited_file: Path = VISITED_FILE,
):
    # Extract book records to jsonl, skips previously written records
    if not html_dir.exists():
        print(f"HTML directory not found: {html_dir}")
        return

    prepare_jsonl_for_append(output_file)
    processed = load_processed_sources(output_file)
    url_by_hash = load_url_map(visited_file)
    pages = sorted(html_dir.glob("*.html"))
    remaining = [page for page in pages if page.name not in processed]
    print(f"Found {len(pages)} HTML pages; {len(remaining)} need extraction.")

    completed = 0
    with output_file.open("a", encoding="utf-8", newline="\n") as output:
        for page_path in remaining:
            try:
                page = page_path.read_text(encoding="utf-8", errors="replace")
                record = extract_record(page, page_path.name, url_by_hash)
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
                output.flush()
            except (OSError, UnicodeError) as error:
                print(f"[extractor] could not read {page_path.name}: {error}")
                continue

            completed += 1
            if completed % 100 == 0 or completed == len(remaining):
                print(f"[{completed}/{len(remaining)}] extracted")

    print(f"Finished. Records written to {output_file}.")


if __name__ == "__main__":
    extract_books()
