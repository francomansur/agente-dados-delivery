"""The web page: served at /, with its static files, and talking only to this API."""

import re

import pytest
from fastapi.testclient import TestClient

from delivery_agent.api.routes import WEB_DIR
from delivery_agent.main import app
from evals.cases import CASES

# Without the context manager the lifespan does not run: these routes need no database.
client = TestClient(app)


def test_the_page_is_served_at_the_root() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>Pergunte aos dados · Delivery</title>" in response.text
    assert 'href="/static/styles.css"' in response.text
    assert 'src="/static/app.js"' in response.text


@pytest.mark.parametrize(("path", "kind"), [("/static/styles.css", "css"), ("/static/app.js", "javascript")])
def test_the_static_files_load(path: str, kind: str) -> None:
    response = client.get(path)

    # Some systems call JavaScript text/javascript, others application/javascript.
    assert response.status_code == 200
    assert kind in response.headers["content-type"]


def test_the_page_asks_the_api_for_the_details() -> None:
    script = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    assert 'const ENDPOINT = "/questions?include_details=true";' in script


def test_the_page_loads_nothing_from_other_sites() -> None:
    for name in ("index.html", "styles.css", "app.js"):
        assert not re.search(r"https?://", (WEB_DIR / name).read_text(encoding="utf-8")), name


def test_the_examples_are_not_evaluation_questions() -> None:
    page = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    examples = re.findall(r'<button type="button" class="example">(.+?)</button>', page)

    assert 4 <= len(examples) <= 6
    assert not set(examples) & {case.question for case in CASES}


def test_the_page_stays_out_of_the_api_docs() -> None:
    assert "/" not in client.get("/openapi.json").json()["paths"]


def test_a_pending_card_has_the_approval_buttons() -> None:
    page = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    script = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    assert '<button type="button" class="action__approve">Aprovar</button>' in page
    assert '<button type="button" class="action__reject">Recusar</button>' in page
    assert '`/actions/${encodeURIComponent(actionId)}/${approve ? "approve" : "reject"}`' in script
