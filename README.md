# Goodreads Book Search

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install firefox
```

## Collect and extract books

In `main.py`, uncomment the step you want to run and comment out the other:

```python
crawl(max_pages=3000)  # download HTML pages into html/
# extract_books()      # extract records into books.jsonl
```

Then run:

```powershell
python main.py
```

Run the extractor after crawling. Both steps can be resumed: the crawler keeps its queue and visited URLs, and the extractor skips pages it has already written to `books.jsonl`.

## Build and search

```powershell
python indexer.py build
python indexer.py search "fantasy dragons" --method classic
python indexer.py search "artificial intelligence" --method smoothed
```

The index is saved to `search_index.json`. Search supports `classic` and `smoothed` IDF methods; `--top 20` changes the number of results.
