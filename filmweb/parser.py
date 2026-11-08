import csv
import json
import logging
from datetime import datetime
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

TITLE_TYPE_OUTPUT_MAPPING = {"film": "movie", "serial": "tv_show", "videogame": "game"}
KEY_MAPPING = {
    "timestamp": "timestamp",
    "favorite": "favorite",
    "rate": "user_rating",
    "global_rate": "global_rating",
    "count": "global_rating_count",
    "originalTitle": "original_title",
    "title": "pl_title",
    "year": "year",
    "entity": "movie_id",
    "type": "title_type",
    "subType": "title_sub_type",
    "url": "url",
    "date": "date",
}
# extended info (api film/<id>/preview), added to export only with --details
DETAILS_KEYS = ("poster_url", "genres", "countries", "duration", "directors")
POSTER_URL_PREFIX = "https://fwcdn.pl/fpo"
POSTER_SIZE = 10  # size used by filmweb in the list of ratings
# TODO? countVote1 and so on keys ingnored for now, histogram info?


def merge_data(ids, user_ratings, global_info, global_rating, details=None):
    """
    Merge all data into one
    """
    all_data = tuple(
        _movie_id_key(el) for el in (user_ratings, global_info, global_rating)
    )
    merged = ({**all_data[0][id], **all_data[1][id], **all_data[2][id]} for id in ids)
    movies = tuple(_fix_keys(entry) for entry in merged)
    if details is None:
        return movies
    details_by_id = {entry["movie_id"]: entry for entry in details}
    return tuple(
        {
            **movie,
            **{key: details_by_id[movie["movie_id"]][key] for key in DETAILS_KEYS},
        }
        for movie in movies
    )


def parse_details(entry):
    """
    Parse response of film preview api into title info
    """
    poster = (entry.get("poster") or {}).get("path")
    title = (entry.get("title") or {}).get("title")
    original_title = (entry.get("originalTitle") or {}).get("title")
    entity_name = entry.get("entityName")
    genres = (
        ((genre or {}).get("name") or {}).get("text")
        for genre in _list(entry, "genres")
    )
    return {
        "movie_id": entry["entity"],
        "original_title": original_title or title,
        "pl_title": title or original_title,
        "year": entry.get("year"),
        "title_type": TITLE_TYPE_OUTPUT_MAPPING.get(entity_name, entity_name),
        "title_sub_type": entry.get("subType"),
        "poster_url": _poster_url(poster),
        "genres": [genre for genre in genres if genre],
        "countries": [
            country["code"]
            for country in _list(entry, "countries")
            if country.get("code")
        ],
        "duration": entry.get("duration"),
        "directors": [
            director["name"]
            for director in _list(entry, "directors")
            if director.get("name")
        ],
    }


def _list(entry, key):
    """
    Value for key that should be a list, empty list if missing
    """
    return entry.get(key) or []


def _poster_url(path):
    """
    Build url of poster image from api path, e.g.
    /39/15/643915/7889447_1.$.jpg -> https://fwcdn.pl/fpo/39/15/643915/7889447_1.10.webp
    """
    if not path:
        return None
    base = path.rpartition(".")[0]
    return f"{POSTER_URL_PREFIX}{base.replace('$', str(POSTER_SIZE))}.webp"


def _movie_id_key(data):
    """
    Parse and reformat data into dict with movie_id as key
    """
    return {entry["entity"]: entry for entry in data}


def _fix_keys(entry):
    """
    Fix keys names for data
    """
    fixed = {new_key: entry.get(old_key) for old_key, new_key in KEY_MAPPING.items()}
    fixed["title_type"] = TITLE_TYPE_OUTPUT_MAPPING.get(
        fixed.get("title_type"), fixed.get("title_type")
    )
    if fixed.get("pl_title") is None:
        fixed["pl_title"] = fixed.get("original_title")
    if fixed.get("original_title") is None:
        fixed["original_title"] = fixed["pl_title"]
    path = quote_plus(
        f"""{(fixed["pl_title"] or "").strip()}-{fixed["year"]}-{fixed["movie_id"]}"""
    )
    fixed["url"] = f"https://www.filmweb.pl/film/{path}"
    fixed["date"] = datetime.fromtimestamp(fixed["timestamp"] / 1000).strftime(
        "%Y-%m-%d"
    )
    return fixed


def _write_csv(file_name, field_names, movies):
    """
    Helper for writing csv
    """
    with open(file_name, "w", encoding="utf-8") as out_file:
        writer = csv.DictWriter(out_file, fieldnames=field_names, dialect="unix")
        writer.writeheader()
        for movie in movies:
            # list values (genres etc.) are joined, csv has no list type
            writer.writerow(
                {
                    key: "; ".join(map(str, value))
                    if isinstance(value, list)
                    else value
                    for key, value in movie.items()
                }
            )
    return True


def _letterbox_entry(entry):
    """
    Format letterboxd column names
    https://letterboxd.com/about/importing-data/
    """
    fixed = {
        "Title": entry.get("original_title"),
        "Year": entry.get("year"),
        "Rating10": entry.get("user_rating"),
        "WatchedDate": entry.get("date"),
    }
    return fixed


def write_data(movies, user, formats):
    """Write export data to requested output formats"""
    assert movies, "no data to write"
    date = datetime.now().strftime("%Y%m%d")
    written_files = []
    if "json" in formats:
        file_name = f"{user}_{date}.json"
        with open(file_name, "w", encoding="utf-8") as out_file:
            out_file.write(json.dumps(movies))
        logger.info(f"{file_name} written!")
        written_files.append(file_name)
    if "csv" in formats:
        file_name = f"{user}_{date}.csv"
        field_names = [*KEY_MAPPING.values()]
        if "poster_url" in movies[0]:
            field_names.extend(DETAILS_KEYS)
        _write_csv(file_name, field_names, movies)
        logger.info(f"{file_name} written!")
        written_files.append(file_name)
    if "letterboxd" in formats:
        file_name = f"{user}_{date}_letterboxd.csv"
        movies_letterbox = tuple(_letterbox_entry(entry) for entry in movies)
        _write_csv(file_name, movies_letterbox[0].keys(), movies_letterbox)
        logger.info(f"{file_name} written!")
        written_files.append(file_name)
    return tuple(written_files)
