import argparse
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

BOOKS_FILE = Path("books.jsonl")
INDEX_FILE = Path("search_index.json")

# Searchable fields
SEARCH_FIELDS = (
    "title",
    "authors",
    "description",
    "genres",
    "series",
    "language",
    "format",
    "isbn",
    "year",
)

# Boost fields that are especially useful for identifying a book
FIELD_WEIGHTS = {
    "title": 8.0,
    "authors": 4.0,
    "description": 0.5,
    "genres": 1.25,
    "series": 1.0,
    "language": 0.5,
    "format": 0.5,
    "isbn": 2.0,
    "year": 4.0,
}

TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
YEAR_FILTER_RE = re.compile(
    # Filters require an explicit delimiter. "year 2000" stays full text
    # "year:2000" and "year=2000" are metadata filters
    r"\byear\s*[:=]\s*(\d{4})\b", re.I
)
IDF_METHODS = ("classic", "smoothed")
EXACT_TITLE_BONUS = 0.5
TITLE_PHRASE_BONUS = 0.15


def tokenize(value: str):
    # Return case-insensitive Unicode word and number tokens
    return TOKEN_RE.findall(value.casefold())


def field_text(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(str(part) for part in value if part is not None)
    return str(value)


def idf_value(document_count: int, document_frequency: int, method: str):
    if document_count <= 0 or document_frequency <= 0:
        return 0.0
    if method == "classic":
        # Classic IDF: ln(N / df). Terms in every document get weight zero.
        return math.log(document_count / document_frequency)
    if method == "smoothed":
        # Smoothed IDF: avoids zero weights for terms in every document.
        return math.log(1.0 + document_count / document_frequency)
    raise ValueError(f"Unknown IDF method: {method}")


def tf_weight(frequency: float):
    # Normalize term frequency, diminishing returns for repetition
    return 1.0 + math.log(frequency) if frequency > 0 else 0.0


def weighted_term_frequency(field_frequencies: dict, field_weights: dict):
    # Combine field frequencies with field boosts from weights
    return sum(
        count * field_weights.get(field, 1.0)
        for field, count in field_frequencies.items()
    )


def read_books(path: Path):
    records = []
    with path.open("r", encoding="utf-8", errors="replace") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                print(f"[indexer] skipping malformed JSONL line {line_number}: {error}")
                continue
            if not isinstance(record, dict):
                print(f"[indexer] skipping non-object JSONL line {line_number}")
                continue
            records.append(record)
    return records


def build_index(books_file: Path = BOOKS_FILE, index_file: Path = INDEX_FILE):
    # Build inverted index and save it as JSON
    if not books_file.exists():
        raise FileNotFoundError(f"Book records not found: {books_file}")

    records = read_books(books_file)
    postings = defaultdict(dict)

    for document_id, record in enumerate(records):
        for field in SEARCH_FIELDS:
            terms = tokenize(field_text(record.get(field)))
            for term, frequency in Counter(terms).items():
                postings[term].setdefault(str(document_id), {})[field] = frequency

    document_count = len(records)
    document_norms = {method: [0.0] * document_count for method in IDF_METHODS}
    for term, term_postings in postings.items():
        document_frequency = len(term_postings)
        for method in IDF_METHODS:
            idf = idf_value(document_count, document_frequency, method)
            for document_id, field_frequencies in term_postings.items():
                frequency = weighted_term_frequency(field_frequencies, FIELD_WEIGHTS)
                weight = tf_weight(frequency) * idf
                document_norms[method][int(document_id)] += weight * weight
    for method in IDF_METHODS:
        document_norms[method] = [math.sqrt(value) for value in document_norms[method]]

    index = {
        "format_version": 3,
        "document_count": document_count,
        "search_fields": list(SEARCH_FIELDS),
        "field_weights": FIELD_WEIGHTS,
        "ranking": "field-weighted cosine TF-IDF with title phrase bonuses",
        "idf_methods": {
            "classic": "ln(N / df)",
            "smoothed": "ln(1 + N / df)",
        },
        "documents": records,
        "postings": {term: postings[term] for term in sorted(postings)},
        "document_norms": document_norms,
    }

    index_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = index_file.with_suffix(index_file.suffix + ".tmp")
    with temporary_file.open("w", encoding="utf-8", newline="\n") as output:
        json.dump(index, output, ensure_ascii=False, separators=(",", ":"))
        output.write("\n")
        output.flush()
    temporary_file.replace(index_file)

    print(
        f"Built index for {document_count} books with {len(postings)} unique terms."
    )
    print(f"Saved index to {index_file}.")
    return index


def load_index(path: Path = INDEX_FILE):
    with path.open("r", encoding="utf-8") as source:
        index = json.load(source)
    if index.get("format_version") != 3:
        raise ValueError(f"Unsupported index format in {path}")
    return index


def search(index: dict, query: str, method: str = "classic", limit: int = 10):
    # Return cosine TF-IDF results
    if method not in IDF_METHODS:
        raise ValueError(f"method must be one of: {', '.join(IDF_METHODS)}")
    if limit <= 0:
        return []

    year_match = YEAR_FILTER_RE.search(query)
    year_filter = int(year_match.group(1)) if year_match else None
    text_query = YEAR_FILTER_RE.sub(" ", query, count=1) if year_match else query
    ordered_query_terms = tokenize(text_query)
    query_terms = Counter(ordered_query_terms)
    if not query_terms and year_filter is None:
        return []

    document_count = index["document_count"]
    postings = index["postings"]
    field_weights = index["field_weights"]
    document_norms = index["document_norms"][method]
    documents = index["documents"]
    scores = defaultdict(float)
    query_norm_squared = 0.0
    if year_filter is not None:
        for document_id, book in enumerate(documents):
            if book.get("year") == year_filter:
                scores[document_id] = 0.0

    for term, query_frequency in query_terms.items():
        term_postings = postings.get(term)
        if not term_postings:
            continue
        idf = idf_value(document_count, len(term_postings), method)
        query_weight = tf_weight(query_frequency) * idf
        query_norm_squared += query_weight * query_weight

        for document_id, field_frequencies in term_postings.items():
            numeric_id = int(document_id)
            if year_filter is not None and documents[numeric_id].get("year") != year_filter:
                continue
            document_weight = tf_weight(
                weighted_term_frequency(field_frequencies, field_weights)
            ) * idf
            scores[numeric_id] += document_weight * query_weight

    query_norm = math.sqrt(query_norm_squared)
    if query_norm > 0:
        for document_id in list(scores):
            document_norm = document_norms[document_id]
            scores[document_id] = (
                scores[document_id] / (document_norm * query_norm)
                if document_norm > 0
                else 0.0
            )

    # Give an exact title or title phrase a small boost after cosine scoring.
    if ordered_query_terms:
        query_length = len(ordered_query_terms)
        for document_id, book in enumerate(documents):
            if year_filter is not None and book.get("year") != year_filter:
                continue
            title_terms = tokenize(field_text(book.get("title")))
            is_exact_title = title_terms == ordered_query_terms
            has_title_phrase = any(
                title_terms[start : start + query_length] == ordered_query_terms
                for start in range(max(0, len(title_terms) - query_length + 1))
            )
            if is_exact_title:
                scores[document_id] += EXACT_TITLE_BONUS
            elif has_title_phrase:
                scores[document_id] += TITLE_PHRASE_BONUS

    ranked = list(scores.items())
    ranked.sort(
        key=lambda item: (
            -item[1],
            -float(documents[item[0]].get("rating") or 0),
            field_text(documents[item[0]].get("title")).casefold(),
        )
    )
    return [
        {"score": score, "book": documents[document_id]}
        for document_id, score in ranked[:limit]
    ]


def print_results(results, query: str, method: str):
    if not results:
        print(f'No results for "{query}" using {method} IDF.')
        return
    for position, result in enumerate(results, start=1):
        book = result["book"]
        title = field_text(book.get("title")) or "Untitled"
        authors_value = book.get("authors") or []
        if not isinstance(authors_value, (list, tuple)):
            authors_value = [authors_value]
        authors = ", ".join(str(author) for author in authors_value if author)
        year = book.get("year")
        details = " — ".join(part for part in (authors, str(year) if year else "") if part)
        print(f"{position}. {title}  [score={result['score']:.4f}]")
        if details:
            print(f"   {details}")
        genres = book.get("genres") or []
        if genres:
            print(f"   Genres: {', '.join(map(str, genres))}")
        if book.get("url"):
            print(f"   {book['url']}")


def main():
    parser = argparse.ArgumentParser(description="Build and search the book index.")
    commands = parser.add_subparsers(dest="command", required=True)

    build_parser = commands.add_parser("build", help="build the index from books.jsonl")
    build_parser.add_argument("--input", type=Path, default=BOOKS_FILE)
    build_parser.add_argument("--output", type=Path, default=INDEX_FILE)

    search_parser = commands.add_parser("search", help="rank books for a text query")
    search_parser.add_argument("query")
    search_parser.add_argument("--index", type=Path, default=INDEX_FILE)
    search_parser.add_argument("--method", choices=IDF_METHODS, default="classic")
    search_parser.add_argument("--top", type=int, default=10)

    args = parser.parse_args()
    if args.command == "build":
        build_index(args.input, args.output)
    else:
        index = load_index(args.index)
        results = search(index, args.query, args.method, args.top)
        print_results(results, args.query, args.method)


if __name__ == "__main__":
    main()
