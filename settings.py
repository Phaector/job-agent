"""Loads config.yaml and secrets from environment variables.

Locally, secrets come from a .env file. On GitHub they come from
repository Secrets (Settings -> Secrets and variables -> Actions).
"""
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

with open(ROOT / "config.yaml", encoding="utf-8") as f:
    CONFIG = yaml.safe_load(f)


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
NOTION_API_KEY = _env("NOTION_API_KEY")
NOTION_DATABASE_ID = _env("NOTION_DATABASE_ID")
NOTION_PROFILE_PAGE_ID = _env("NOTION_PROFILE_PAGE_ID")      # optional
ADZUNA_APP_ID = _env("ADZUNA_APP_ID")
ADZUNA_APP_KEY = _env("ADZUNA_APP_KEY")
SERPAPI_KEY = _env("SERPAPI_KEY")                            # optional
GMAIL_ADDRESS = _env("GMAIL_ADDRESS")                        # optional
GMAIL_APP_PASSWORD = _env("GMAIL_APP_PASSWORD")              # optional

MODEL_SCREEN = CONFIG["models"]["screen"]
MODEL_DEEP = CONFIG["models"]["deep"]

STATE_FILE = ROOT / "state" / "seen.json"
MAIL_STATE_FILE = ROOT / "state" / "seen_emails.json"        # newsletter emails already read
PROFILE_FILE = ROOT / "profile.md"
