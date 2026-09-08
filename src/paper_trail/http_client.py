# src/paper_trail/http_client.py
import requests

DEFAULT_HEADERS = {
    "User-Agent": "paper-trail/1.0 (Research Automator; contact@example.com)"
}


def get_http_session(timeout: int = 30) -> requests.Session:
    """Creates a configured requests session with default headers."""
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    return session
