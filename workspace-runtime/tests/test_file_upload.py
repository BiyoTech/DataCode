"""Workspace file upload tests (POST /api/v1/files/upload).

What is pinned here is the CONTRACT the explorer's context menu relies on:

* bytes land byte-exact — an image or archive must survive the round trip,
  which is exactly why the write path is ``write_bytes`` and not the text
  ``write_file`` (a lossy decode/re-encode would corrupt it);
* the client-supplied filename is reduced to a BARE name — it is
  client-controlled text, and the traversal guard must never see ``../../x``;
* the target DIRECTORY is traversal-guarded like every other write;
* oversize payloads are refused with 413 before touching the disk.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api.rest import files as files_api  # noqa: E402
from src.config import settings  # noqa: E402

_BASE = "base"
_COPY_PARTS = ("t1", "ws-t1-shop", "u1", "envs", "dev")


@pytest.fixture()
def workspace_path(tmp_path, monkeypatch):
    base = tmp_path / _BASE
    target = base.joinpath(*_COPY_PARTS)
    target.mkdir(parents=True)
    (target / "seeds").mkdir()
    monkeypatch.setattr(settings, "workspace_base_path", str(base))
    return str(target)


@pytest.fixture()
def client(workspace_path):
    from fastapi.responses import JSONResponse

    from src.utils.errors import ServiceError

    app = FastAPI()
    app.include_router(files_api.router)

    @app.exception_handler(ServiceError)
    async def _service_error(_request, exc: ServiceError):
        return JSONResponse(
            status_code=exc.status_code, content=exc.to_response_dict()
        )

    with TestClient(app) as test_client:
        yield test_client


def _upload(
    client: TestClient,
    workspace_path: str,
    filename: str,
    content: bytes,
    directory: str = "",
):
    # workspace_path rides the QUERY string, not the form: the gateway reads it
    # for authorization and lease routing, and it cannot inspect a multipart
    # body for either. Only the file and its target directory stay in the form.
    return client.post(
        "/api/v1/files/upload",
        params={"workspace_path": workspace_path},
        data={"directory": directory},
        files={"file": (filename, content)},
    )


def test_upload_into_directory_lands_with_exact_content(client, workspace_path):
    resp = _upload(client, workspace_path, "orders.csv", b"id,name\n1,foo\n", "seeds")

    assert resp.status_code == 200, resp.text
    assert resp.json()["path"] == "seeds/orders.csv"
    assert (Path(workspace_path) / "seeds" / "orders.csv").read_bytes() == b"id,name\n1,foo\n"


def test_upload_preserves_binary_content_byte_exact(client, workspace_path):
    # A tiny "image": every byte value 0-255, including ones no UTF-8 round
    # trip would survive. This is the case that justifies write_bytes.
    payload = bytes(range(256)) * 4
    resp = _upload(client, workspace_path, "logo.png", payload, "design")

    assert resp.status_code == 200, resp.text
    assert (Path(workspace_path) / "design" / "logo.png").read_bytes() == payload


def test_upload_to_workspace_root_when_directory_is_empty(client, workspace_path):
    resp = _upload(client, workspace_path, "notes.txt", b"hello")

    assert resp.status_code == 200, resp.text
    assert resp.json()["path"] == "notes.txt"
    assert (Path(workspace_path) / "notes.txt").read_bytes() == b"hello"


def test_upload_overwrites_an_existing_file(client, workspace_path):
    target = Path(workspace_path) / "seeds" / "orders.csv"
    target.write_bytes(b"old")

    resp = _upload(client, workspace_path, "orders.csv", b"new", "seeds")

    assert resp.status_code == 200, resp.text
    assert target.read_bytes() == b"new"


def test_upload_strips_path_components_from_the_filename(client, workspace_path):
    # The filename field is client-controlled text; both separator styles must
    # be reduced to a bare name inside the target directory.
    resp = _upload(client, workspace_path, "../../evil.sh", b"nope", "seeds")
    assert resp.status_code == 200, resp.text
    assert (Path(workspace_path) / "seeds" / "evil.sh").read_bytes() == b"nope"
    assert not (Path(workspace_path).parent / "evil.sh").exists()

    resp = _upload(client, workspace_path, "C:\\temp\\evil.cmd", b"nope", "seeds")
    assert resp.status_code == 200, resp.text
    assert (Path(workspace_path) / "seeds" / "evil.cmd").exists()


@pytest.mark.parametrize("bad", [".", "..", "///"])
def test_upload_rejects_a_nameless_file(client, workspace_path, bad):
    resp = _upload(client, workspace_path, bad, b"x", "seeds")
    # Stripping every separator leaves nothing — a client error, never a 500.
    assert resp.status_code == 400, resp.text


def test_upload_rejects_an_empty_filename(client, workspace_path):
    # Refused one layer earlier: httpx encodes an empty filename as a plain form
    # field, so FastAPI's own validation answers 422 before the handler runs.
    resp = _upload(client, workspace_path, "", b"x", "seeds")
    assert resp.status_code in (400, 422), resp.text


def test_upload_rejects_a_traversing_directory(client, workspace_path):
    resp = _upload(client, workspace_path, "x.txt", b"x", "../outside")
    assert resp.status_code == 400, resp.text
    assert not (Path(workspace_path).parent / "outside" / "x.txt").exists()


def test_upload_rejects_oversize_content(client, workspace_path):
    too_big = files_api.MAX_UPLOAD_BYTES + 1
    resp = _upload(client, workspace_path, "big.bin", b"x" * too_big, "seeds")

    assert resp.status_code == 413, resp.text
    assert not (Path(workspace_path) / "seeds" / "big.bin").exists()
