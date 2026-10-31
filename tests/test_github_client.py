"""Offline tests for probe.github_client (httpx.MockTransport, no network)."""

import io
import json
import tarfile
from datetime import datetime, timezone

import httpx
import pytest

from probe import github_client as gc
from probe.github_client import (
    GitHubClient,
    GitHubError,
    InvalidURLError,
    NotFoundError,
    RateLimitError,
    parse_github_url,
    safe_extract_tarball,
)

SHA = "a" * 40
HEAD = "b" * 40
BASE = "c" * 40


def make_tarball(entries, top="octo-demo-aaaaaaa"):
    """entries: list of (name, content | None for dir | ('sym', target))."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        root = tarfile.TarInfo(top)
        root.type = tarfile.DIRTYPE
        tar.addfile(root)
        for name, content in entries:
            info = tarfile.TarInfo(name if name.startswith("/") or name.startswith("..") else f"{top}/{name}")
            if content is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif isinstance(content, tuple):
                info.type = tarfile.SYMTYPE
                info.linkname = content[1]
                tar.addfile(info)
            else:
                data = content.encode()
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


ISSUE_JSON = {
    "number": 7,
    "title": "calculate_tax crashes",
    "body": "Calling `calculate_tax(-1)` crashes.",
    "state": "closed",
    "state_reason": "not_planned",
    "labels": [{"name": "invalid"}, {"name": "bug"}],
    "user": {"login": "reporter"},
    "created_at": "2024-03-01T10:00:00Z",
    "closed_at": "2024-03-02T10:00:00Z",
    "html_url": "https://github.com/octo/demo/issues/7",
}


class FakeGitHub:
    """Routes requests and records them."""

    def __init__(self, tarball=None, files_pages=None):
        self.requests: list[httpx.Request] = []
        self.tarball = tarball or make_tarball([("src/billing.py", "def calculate_tax(x):\n    return x\n")])
        self.files_pages = files_pages or [[{"filename": "src/billing.py"}, {"filename": "tests/test_billing.py"}]]
        self.overrides: dict[str, httpx.Response] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path in self.overrides:
            return self.overrides[path]
        if path == "/repos/octo/demo":
            return httpx.Response(200, json={"default_branch": "main"})
        if path == "/repos/octo/demo/issues/7":
            return httpx.Response(200, json=ISSUE_JSON)
        if path == "/repos/octo/demo/issues/8":
            return httpx.Response(200, json={**ISSUE_JSON, "number": 8, "pull_request": {}})
        if path == "/repos/octo/demo/pulls/8":
            if "diff" in request.headers.get("accept", ""):
                return httpx.Response(200, text="diff --git a/src/billing.py b/src/billing.py\n")
            return httpx.Response(
                200, json={"head": {"sha": HEAD}, "base": {"sha": BASE, "ref": "main"}, "merged": True}
            )
        if path == "/repos/octo/demo/pulls/8/files":
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(200, json=self.files_pages[page - 1] if page <= len(self.files_pages) else [])
        if path == "/repos/octo/demo/commits":
            return httpx.Response(200, json=[{"sha": SHA}])
        if path == "/repos/octo/demo/issues/7/comments":
            if request.method == "GET":
                return httpx.Response(200, json=[{"id": 42, "body": "first comment"}])
            if request.method == "POST":
                data = json.loads(request.content)
                return httpx.Response(201, json={"id": 101, "body": data.get("body")})
        if path == "/repos/octo/demo/issues/comments/101" and request.method == "PATCH":
            data = json.loads(request.content)
            return httpx.Response(200, json={"id": 101, "body": data.get("body")})
        if path == "/repos/octo/demo/issues/7/labels" and request.method == "POST":
            data = json.loads(request.content)
            return httpx.Response(200, json=[{"name": label} for label in data.get("labels", [])])
        if path.startswith("/repos/octo/demo/issues/7/labels/") and request.method == "DELETE":
            return httpx.Response(200, json={})
        if path.startswith("/repos/octo/demo/tarball/"):
            return httpx.Response(200, content=self.tarball)
        return httpx.Response(404, json={"message": "Not Found"})


@pytest.fixture
def fake():
    return FakeGitHub()


@pytest.fixture
def client(fake, tmp_path):
    with GitHubClient(token="", transport=httpx.MockTransport(fake), cache_dir=tmp_path / "cache") as c:
        yield c


# ---------------------------------------------------------------- URL parsing

@pytest.mark.parametrize(
    ("url", "kind", "number"),
    [
        ("https://github.com/psf/requests/issues/6000", "issue", 6000),
        ("https://github.com/psf/requests/pull/6963", "pull_request", 6963),
        ("https://github.com/psf/requests/pull/6963/files#diff-1", "pull_request", 6963),
        ("http://www.github.com/a-b/c.d_e/issues/1?x=1", "issue", 1),
        ("  https://github.com/psf/requests/issues/6000  ", "issue", 6000),
    ],
)
def test_parse_github_url_valid(url, kind, number):
    target = parse_github_url(url)
    assert target.kind == kind and target.number == number


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/psf/requests/issues/1",
        "https://github.com/psf/requests",
        "https://github.com/psf/requests/issues/0",
        "https://github.com/psf/requests/issues/abc",
        "https://github.com/psf/../issues/1",
        "https://github.com.evil.com/psf/requests/issues/1",
        "javascript:alert(1)",
        "",
    ],
)
def test_parse_github_url_invalid(url):
    with pytest.raises(InvalidURLError):
        parse_github_url(url)


# ---------------------------------------------------------------- issues and PRs

def test_get_issue(client):
    issue = client.get_issue("octo", "demo", 7)
    assert issue.title == "calculate_tax crashes"
    assert issue.labels == ["bug", "invalid"]
    assert issue.state_reason == "not_planned"
    assert issue.created_at == datetime(2024, 3, 1, 10, tzinfo=timezone.utc)
    assert issue.is_pull_request is False


def test_get_pull_collects_files_and_diff(client):
    pr = client.get_pull("octo", "demo", 8)
    assert pr.is_pull_request is True
    assert pr.head_sha == HEAD and pr.base_sha == BASE and pr.base_ref == "main"
    assert pr.changed_files == ["src/billing.py", "tests/test_billing.py"]
    assert pr.diff.startswith("diff --git")
    assert pr.merged is True


def test_get_pull_paginates_files(tmp_path):
    pages = [[{"filename": f"f{i}.py"} for i in range(100)], [{"filename": "last.py"}]]
    fake = FakeGitHub(files_pages=pages)
    with GitHubClient(token="", transport=httpx.MockTransport(fake), cache_dir=tmp_path) as c:
        pr = c.get_pull("octo", "demo", 8, include_diff=False)
    assert len(pr.changed_files) == 101 and pr.changed_files[-1] == "last.py"
    assert not any("diff" in r.headers.get("accept", "") for r in fake.requests)


def test_commit_at_uses_until_and_default_branch(client, fake):
    sha = client.commit_at("octo", "demo", datetime(2024, 3, 1, 15, 30, tzinfo=timezone.utc))
    assert sha == SHA
    req = [r for r in fake.requests if r.url.path == "/repos/octo/demo/commits"][0]
    assert req.url.params["sha"] == "main"
    assert req.url.params["until"] == "2024-03-01T15:30:00Z"


def test_commit_at_rejects_naive_datetime(client):
    with pytest.raises(ValueError):
        client.commit_at("octo", "demo", datetime(2024, 3, 1), ref="main")


def test_commit_at_with_no_commits(client, fake):
    fake.overrides["/repos/octo/demo/commits"] = httpx.Response(200, json=[])
    with pytest.raises(NotFoundError):
        client.commit_at("octo", "demo", datetime(2000, 1, 1, tzinfo=timezone.utc), ref="main")


# ---------------------------------------------------------------- errors

def test_not_found(client):
    with pytest.raises(NotFoundError):
        client.get_issue("octo", "demo", 999)


def test_rate_limit(client, fake):
    fake.overrides["/repos/octo/demo/issues/7"] = httpx.Response(
        403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1700000000"}, json={}
    )
    with pytest.raises(RateLimitError) as exc:
        client.get_issue("octo", "demo", 7)
    assert exc.value.reset_at == 1700000000


def test_other_http_error(client, fake):
    fake.overrides["/repos/octo/demo/issues/7"] = httpx.Response(502, json={})
    with pytest.raises(GitHubError) as exc:
        client.get_issue("octo", "demo", 7)
    assert exc.value.status == 502


def test_network_error_is_wrapped(tmp_path):
    def boom(request):
        raise httpx.ConnectError("offline", request=request)

    with GitHubClient(token="", transport=httpx.MockTransport(boom), cache_dir=tmp_path) as c:
        with pytest.raises(GitHubError, match="Network error"):
            c.get_issue("octo", "demo", 7)


@pytest.mark.parametrize("owner", ["..", "a/b", "x y", ""])
def test_rejects_bad_owner(client, owner):
    with pytest.raises(ValueError):
        client.get_issue(owner, "demo", 7)


def test_token_header(fake, tmp_path):
    with GitHubClient(token="t0ken", transport=httpx.MockTransport(fake), cache_dir=tmp_path) as c:
        c.get_issue("octo", "demo", 7)
    assert fake.requests[0].headers["authorization"] == "Bearer t0ken"


def test_no_token_means_no_auth_header(client, fake):
    client.get_issue("octo", "demo", 7)
    assert "authorization" not in fake.requests[0].headers


def test_client_only_sends_get_requests(client, fake):
    issue = client.get_issue("octo", "demo", 7)
    client.get_pull("octo", "demo", 8)
    client.snapshot_for_issue(issue)
    assert {r.method for r in fake.requests} == {"GET"}


# ---------------------------------------------------------------- snapshots

def test_snapshot_extracts_and_caches(client, fake):
    snap = client.snapshot("octo", "demo", SHA, resolved_by="test")
    assert (snap.path / "src" / "billing.py").read_text().startswith("def calculate_tax")
    assert snap.path.name == SHA and not snap.from_cache
    downloads = sum(r.url.path.startswith("/repos/octo/demo/tarball/") for r in fake.requests)
    again = client.snapshot("octo", "demo", SHA, resolved_by="test")
    assert again.from_cache and again.path == snap.path
    assert sum(r.url.path.startswith("/repos/octo/demo/tarball/") for r in fake.requests) == downloads


def test_snapshot_for_issue_pins_commit_at_creation(client):
    snap = client.snapshot_for_issue(client.get_issue("octo", "demo", 7))
    assert snap.sha == SHA
    assert snap.resolved_by == "default_branch_at:2024-03-01T10:00:00+00:00"


def test_snapshot_for_pull_uses_head(client):
    snap = client.snapshot_for_pull(client.get_pull("octo", "demo", 8))
    assert snap.sha == HEAD and snap.resolved_by == "pr_head"


@pytest.mark.parametrize("bad", ["main", "a" * 39, "A" * 40, "../" + "a" * 37])
def test_snapshot_requires_full_sha(client, bad):
    with pytest.raises(ValueError):
        client.snapshot("octo", "demo", bad, resolved_by="test")


def test_failed_extraction_leaves_no_cache_entry(tmp_path):
    fake = FakeGitHub(tarball=b"not a tarball")
    with GitHubClient(token="", transport=httpx.MockTransport(fake), cache_dir=tmp_path) as c:
        with pytest.raises(Exception):
            c.snapshot("octo", "demo", SHA, resolved_by="test")
    assert not (tmp_path / "octo" / "demo" / SHA).exists()
    assert list((tmp_path / "octo" / "demo").iterdir()) == []


def test_safe_extract_rejects_path_traversal(tmp_path):
    data = make_tarball([("ok.py", "x = 1\n"), ("../escape.py", "evil\n")])
    safe_extract_tarball(data, tmp_path / "out")  # leading component is stripped, so it stays inside
    assert not (tmp_path / "escape.py").exists()
    assert all(p.resolve().is_relative_to((tmp_path / "out").resolve()) for p in (tmp_path / "out").rglob("*"))


def test_safe_extract_rejects_escaping_symlink(tmp_path):
    data = make_tarball([("link", ("sym", "/etc/passwd"))])
    with pytest.raises(GitHubError, match="Unsafe"):
        safe_extract_tarball(data, tmp_path / "out")


def test_safe_extract_keeps_internal_symlink(tmp_path):
    data = make_tarball([("real.py", "x = 1\n"), ("alias.py", ("sym", "real.py"))])
    safe_extract_tarball(data, tmp_path / "out")
    assert (tmp_path / "out" / "alias.py").read_text() == "x = 1\n"


def test_safe_extract_enforces_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(gc, "MAX_EXTRACTED_BYTES", 5)
    with pytest.raises(GitHubError, match="size limits"):
        safe_extract_tarball(make_tarball([("big.py", "x" * 50)]), tmp_path / "out")


def test_safe_extract_enforces_file_count(tmp_path, monkeypatch):
    monkeypatch.setattr(gc, "MAX_EXTRACTED_FILES", 1)
    with pytest.raises(GitHubError, match="size limits"):
        safe_extract_tarball(make_tarball([("a.py", "1"), ("b.py", "2")]), tmp_path / "out")


def test_models_serialise(client):
    pr = client.get_pull("octo", "demo", 8)
    assert json.loads(pr.model_dump_json())["head_sha"] == HEAD



# ---------------------------------------------------------------- secondary rate limits, streamed downloads

@pytest.mark.parametrize(
    ("headers", "body"),
    [
        ({"retry-after": "60"}, {"message": "slow down"}),
        ({}, {"message": "You have exceeded a secondary rate limit. Please wait a few minutes."}),
    ],
)
def test_secondary_rate_limit_is_rate_limit_error(client, fake, headers, body):
    fake.overrides["/repos/octo/demo/issues/7"] = httpx.Response(403, headers=headers, json=body)
    with pytest.raises(RateLimitError) as exc:
        client.get_issue("octo", "demo", 7)
    assert exc.value.retry_after == (60 if headers else None)


def test_plain_403_is_not_rate_limit(client, fake):
    fake.overrides["/repos/octo/demo/issues/7"] = httpx.Response(403, json={"message": "Resource not accessible"})
    with pytest.raises(GitHubError) as exc:
        client.get_issue("octo", "demo", 7)
    assert not isinstance(exc.value, RateLimitError)


def test_tarball_over_limit_aborts_and_leaves_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(gc, "MAX_TARBALL_BYTES", 100)
    big = make_tarball([(f"f{i}.py", "x = '%s'\n" % ("y" * 500)) for i in range(20)])
    assert len(big) > 100
    fake = FakeGitHub(tarball=big)
    with GitHubClient(token="", transport=httpx.MockTransport(fake), cache_dir=tmp_path) as c:
        with pytest.raises(GitHubError, match="download limit"):
            c.snapshot("octo", "demo", SHA, resolved_by="test")
    assert list((tmp_path / "octo" / "demo").iterdir()) == []


def test_tarball_download_errors_are_typed(tmp_path):
    fake = FakeGitHub()
    fake.overrides[f"/repos/octo/demo/tarball/{SHA}"] = httpx.Response(404, json={})
    with GitHubClient(token="", transport=httpx.MockTransport(fake), cache_dir=tmp_path) as c:
        with pytest.raises(NotFoundError):
            c.snapshot("octo", "demo", SHA, resolved_by="test")
    assert list((tmp_path / "octo" / "demo").iterdir()) == []


def test_safe_extract_accepts_a_path(tmp_path):
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(make_tarball([("m.py", "x = 1\n")]))
    assert safe_extract_tarball(archive, tmp_path / "out") == 1
    assert (tmp_path / "out" / "m.py").read_text() == "x = 1\n"


def test_list_and_post_and_update_comments(client):
    comments = client.list_comments("octo", "demo", 7)
    assert len(comments) == 1
    assert comments[0]["id"] == 42
    assert comments[0]["body"] == "first comment"

    posted = client.post_comment("octo", "demo", 7, "new triage report")
    assert posted["id"] == 101
    assert posted["body"] == "new triage report"

    updated = client.update_comment("octo", "demo", 101, "edited report")
    assert updated["id"] == 101
    assert updated["body"] == "edited report"


def test_add_labels(client):
    labels = client.add_labels("octo", "demo", 7, ["triage:confirmed", "bug"])
    assert len(labels) == 2
    assert labels[0]["name"] == "triage:confirmed"
    assert labels[1]["name"] == "bug"

    empty = client.add_labels("octo", "demo", 7, [])
    assert empty == []


def test_remove_label_and_replace_triage_label(client):
    assert client.remove_label("octo", "demo", 7, "bug") is True
    client.replace_triage_label("octo", "demo", 7, "triage:confirmed")


