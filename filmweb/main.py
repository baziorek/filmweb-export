"""filmweb

Usage:
    filmweb details [--format=<fileformat>]... [--stdout] [--workers=<n>] [--debug] <id>...
    filmweb search [--format=<fileformat>]... [--stdout] [--type=<titletype>]... [--limit=<n>] [--page=<n>] [--strict] [--embed-images] [--workers=<n>] [--debug] <query>...
    filmweb [--format=<fileformat>]... [--stdout] [--type=<titletype>]... [--workers=<n>] [--pages=<range>] [--since=<date>] [--debug] <username> <cookie>

Options:
    -h --help                     Show this screen
    -f --format=<fileformat>      Output file format: json (default), csv, letterboxd
    -o --stdout                   Print result to stdout instead of file
    -t --type=<titletype>         Title type: movie, tv_show, game (default: all)
    -w --workers=<n>              Number of parallel workers (default: CPU count)
    -p --pages=<range>            Fetch only given pages of ratings list (100 titles per page),
                                  e.g. 3 or 2-5 or 4- (default: all pages)
    -s --since=<date>             Export only titles rated (or re-rated) on or after
                                  given date, inclusive, format YYYY-MM-DD
                                  (default: all titles)
    -l --limit=<n>                Search: max number of titles to return (default: 10)
    --page=<n>                    Search: first page of results to read (default: 1)
    -S --strict                   Search: keep only titles whose matched title contains
                                  all words of the query (default: any word)
    -e --embed-images             Search: embed posters in json as base64 data uri
                                  (field poster_image, json format only)
    -d --debug                    Debug prints

Source:
    https://github.com/ppatrzyk/filmweb-export
"""

import itertools
import json
import logging
import multiprocessing
import re
from math import ceil

import tqdm
from docopt import docopt

from . import getter, parser, writer

logger = logging.getLogger(__name__)

CPU_COUNT = multiprocessing.cpu_count()
MOVIES_PER_PAGE = 100
MAX_SEARCH_PAGES = 5  # max requests made by one search, see search()


def _get_details(ids: tuple[int], pool: multiprocessing.Pool):
    """Fetch details for given ids"""
    details = tuple(
        json.loads(el)
        for el in tqdm.tqdm(
            pool.imap_unordered(getter.get_preview, ids), total=len(ids)
        )
    )
    details = tuple(parser.parse_details(d) for d in details)
    return details


def details(args: dict, pool: multiprocessing.Pool):
    """Get extended info about titles with given ids"""
    ids = parser.parse_ids(args["<id>"])
    logger.info("Fetching info about titles [1/2]...")
    details = _get_details(ids, pool)
    logger.info("Fetching global rating for titles [2/2]...")
    global_rating = tuple(
        json.loads(el)
        for el in tqdm.tqdm(
            pool.imap_unordered(getter.get_global_rating, ids), total=len(ids)
        )
    )
    return parser.merge_data(
        ids,
        [details, global_rating],
    )


def search(args: dict, pool: multiprocessing.Pool):
    """Search titles by text, then get info (incl. poster) about found titles"""
    query = " ".join(args["<query>"]).strip()
    assert query, "Empty search query"
    title_types = parser.normalize_title_types(args["--type"])
    limit = parser.parse_positive_int(args["--limit"], "--limit", 10)
    page = parser.parse_positive_int(args["--page"], "--page", 1)
    strict_query = query if args["--strict"] else None
    logger.info(f"Searching for '{query}' [1/2]...")
    # Search also returns people, characters etc., and titles of other types than
    # requested are dropped, so one page of results can hold fewer than `limit`
    # titles. Next pages are read (up to MAX_SEARCH_PAGES requests) to fill it up.
    hits = []
    total = 0
    for current_page in range(page, page + MAX_SEARCH_PAGES):
        response = getter.search(query, current_page, limit)
        total = response.get("total", 0)
        page_hits = parser.parse_search_hits(
            response,
            title_types,
            exclude_ids={hit["movie_id"] for hit in hits},
            strict_query=strict_query,
        )
        hits.extend(page_hits)
        no_more_results = (
            not response.get("searchHits") or current_page * limit >= total
        )
        if len(hits) >= limit or no_more_results:
            break
    hits = tuple(hits[:limit])
    assert hits, (
        f"No titles found for '{query}' (starting from page {page}; "
        f"search found {total} entries of all kinds)"
    )
    ids = tuple(hit["movie_id"] for hit in hits)
    logger.info(f"Fetching info about {len(ids)} titles [2/2]...")
    details = _get_details(ids, pool)
    movies = parser.merge_data(ids, [hits, details])
    if args["--embed-images"]:
        logger.info("Downloading posters...")
        images = tuple(
            tqdm.tqdm(
                pool.imap(
                    getter.get_image_data_uri, [m.get("poster_url") for m in movies]
                ),
                total=len(movies),
            )
        )
        movies = tuple({**m, "poster_image": image} for m, image in zip(movies, images))
        missing = sum(1 for m in movies if m["poster_image"] is None)
        if missing:
            logger.warning(f"No poster image for {missing} of {len(movies)} titles")
    return query, movies


def user_export(args: dict, pool: multiprocessing.Pool):
    """
    Export user data
    """
    user = args["<username>"]
    cookie = args["<cookie>"]
    assert all([user, cookie]), "Empty arguments provided"
    title_types = parser.normalize_title_types(args["--type"])
    first_page, last_page = parser.parse_pages(args["--pages"])
    raw_since = args["--since"]
    since_ms = parser.parse_since(raw_since)
    cookie = re.sub("Cookie:", "", cookie).strip()
    logged_in_user = getter.auth_check(cookie)
    friend_query = user != logged_in_user
    user_ratings_by_type = []
    logger.info("Fetching list of rated titles [1/3]...")
    for title_type in title_types:
        votes_total = getter.get_votes_count(user, title_type)
        pages = ceil(votes_total / MOVIES_PER_PAGE)
        if pages == 0:
            logger.info(f"Skipping {title_type}, no ratings found")
            continue
        end_page = pages if last_page is None else min(last_page, pages)
        if first_page > end_page:
            logger.info(
                f"Skipping {title_type}, requested pages start after last page ({pages})"
            )
            continue
        pages_to_fetch = end_page - first_page + 1
        logger.info(
            f"Fetching list of {title_type} ratings (pages {first_page}-{end_page} of {pages})..."
        )
        get_titles_page_args = (
            (cookie, user, friend_query, title_type, page)
            for page in range(first_page, end_page + 1)
        )
        user_ratings_raw = tuple(
            tqdm.tqdm(
                pool.imap_unordered(getter.get_titles_page, get_titles_page_args),
                total=pages_to_fetch,
            )
        )
        user_ratings_by_type.append(
            tuple(
                itertools.chain.from_iterable(json.loads(el) for el in user_ratings_raw)
            )
        )
    user_ratings = tuple(
        el
        for el in itertools.chain.from_iterable(user_ratings_by_type)
        if el.get("timestamp", 0) >= since_ms
    )
    total_titles = len(user_ratings)
    since_msg = "" if raw_since is None else f" since {raw_since}"
    logger.info(f"User {user} has {total_titles} rated titles{since_msg}...")
    assert total_titles, "No rated titles available"
    ids = tuple(el.get("movie_id") for el in user_ratings)
    logger.info("Fetching info about titles [2/3]...")
    details = _get_details(ids, pool)
    logger.info("Fetching global rating for titles [3/3]...")
    global_rating = tuple(
        json.loads(el)
        for el in tqdm.tqdm(
            pool.imap_unordered(getter.get_global_rating, ids), total=total_titles
        )
    )
    return user, parser.merge_data(ids, [user_ratings, details, global_rating])


def main():
    args = docopt(__doc__)
    if args["--debug"]:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)
    workers = parser.resolve_workers(args["--workers"], CPU_COUNT)
    pool = multiprocessing.Pool(processes=workers)
    try:
        formats = {f.lower() for f in (args["--format"] or ("json",))}
        write_stdout = args["--stdout"]
        if write_stdout:
            assert len(formats) == 1, (
                f"Only one format can be written to stdout, passed: {formats}"
            )
        supported_formats = {"csv", "json", "letterboxd"}
        assert formats and formats.issubset(supported_formats), (
            f"Supported file formats: {supported_formats}"
        )
        if args["search"]:
            assert formats.issubset({"csv", "json"}), (
                "Search supports file formats: csv, json"
            )
            embed_images = args["--embed-images"]
            assert not embed_images or formats == {"json"}, (
                "--embed-images works only with json format"
            )
            query, movies = search(args, pool)
            writer.write_search(movies, query, write_stdout, formats, embed_images)
        elif args["details"]:
            movies = details(args, pool)
            writer.write_details(movies, write_stdout, formats)
        else:
            user, movies = user_export(args, pool)
            writer.write_user_export(movies, user, write_stdout, formats)
    except Exception as e:  # noqa
        logger.error(f"Program error: {e!s}")
    finally:
        pool.close()


if __name__ == "__main__":
    main()
