"""Build the shared pool of startups that just raised, launched or started hiring.

Sources:
  * YC companies currently hiring (yc-oss public JSON mirror)
  * Startup/funding news RSS feeds → LLM extracts the company behind each story
Add more by writing a function that returns dicts with the Startup fields and
listing it in SOURCES.

Only startups inside the focus (FOCUS_CITIES, FOCUS_STAGES) are kept. Unknown city or
stage means out: a wrong match costs the user more than a missing one.
"""
import re  # noqa: F401  (tests use discovery.re)

import httpx  # noqa: F401  (tests patch discovery.httpx.get: the same httpx module every source uses)

from app.config import get_settings  # noqa: F401
from app.services.discovery.articles import _article_text, article_facts, fill_from_article  # noqa: F401
from app.services.discovery.focus import (INDIA_PLACES, _csv, focus_label, in_focus, keep_in_database,  # noqa: F401
                                          normalize_stage)
from app.services.discovery.news import EXTRACT_PROMPT, EXTRACT_SYSTEM, fetch_news  # noqa: F401
from app.services.discovery.pool import SOURCES, run_discovery, upsert_startups  # noqa: F401
from app.services.discovery.startup_map import _startup_map_rows, fetch_startup_map  # noqa: F401
from app.services.discovery.yc import _yc_stage, fetch_yc_hiring  # noqa: F401
