"""Small GitHub API client shared verbatim by the app and infra repositories."""
import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
IMAGE = re.compile(r"^ghcr\.io/([a-z0-9_.-]+/[a-z0-9_.-]+):"
                   r"(backend|frontend)-([a-f0-9]{40})-([1-9][0-9]*)-([1-9][0-9]*)"
                   r"@sha256:([a-f0-9]{64})$")
RECEIPT_KEYS = {"schema", "app", "current", "previous", "release_id", "source_sha",
                "infra_sha", "infra_run_id", "infra_run_attempt", "deployed_at"}


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    """GitHub artifact downloads redirect to signed blob URLs; never forward tokens."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise ValueError("Refusing an insecure redirect")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and urllib.parse.urlsplit(req.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            redirected.remove_header("Authorization")
        return redirected


def image_parts(image, app=None, repository=None):
    match = IMAGE.fullmatch(image or "")
    if not match:
        raise ValueError("Expected a prefixed immutable GHCR tag and sha256 digest")
    repo, component, sha, run, attempt, digest = match.groups()
    if app and app != component:
        raise ValueError("Image belongs to another app")
    if repository and repository.lower() != repo:
        raise ValueError("Image belongs to another repository")
    return repo, component, sha, run, attempt, "sha256:" + digest


def validate_receipt(value, app, image=None):
    if not isinstance(value, dict) or set(value) != RECEIPT_KEYS or value["schema"] != 1 or value["app"] != app:
        raise ValueError("Invalid deployment receipt schema")
    repo, _, sha, _, _, _ = image_parts(value["current"], app)
    if image and value["current"] != image:
        raise ValueError("Receipt does not describe the requested image")
    if value["previous"] is not None:
        image_parts(value["previous"], app, repo)
        if value["previous"] == value["current"]:
            raise ValueError("Previous and current releases must differ")
    if value["release_id"] != value["current"] or value["source_sha"] != sha:
        raise ValueError("Receipt release identity mismatch")
    if not re.fullmatch(r"[a-f0-9]{40}", value["infra_sha"]):
        raise ValueError("Invalid infra commit")
    for key in ("infra_run_id", "infra_run_attempt"):
        if not re.fullmatch(r"[1-9][0-9]*", str(value[key])):
            raise ValueError("Invalid workflow identity")
    from datetime import datetime
    timestamp = datetime.fromisoformat(value["deployed_at"].replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("Deployment timestamp must include its timezone")
    return value


class GitHub:
    def __init__(self, token, repository):
        if not token or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("A GitHub token and owner/repository are required")
        self.token, self.repository = token, repository
        self.opener = urllib.request.build_opener(SafeRedirect())

    def request(self, path, method="GET", data=None, raw=False):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Expected a relative GitHub API path")
        headers = {"Authorization": "Bearer " + self.token,
                   "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10",
                   "User-Agent": "agrozanjir-release"}
        body = None if data is None else json.dumps(data).encode()
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(API + path, data=body, headers=headers, method=method)
        for attempt in range(4):
            try:
                with self.opener.open(request, timeout=30) as response:
                    result = response.read()
                return result if raw else (json.loads(result) if result else None)
            except urllib.error.HTTPError as error:
                if method == "GET" and error.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise

    @property
    def base(self):
        return "/repos/" + self.repository

    def pages(self, path, key=None):
        result = []
        separator = "&" if "?" in path else "?"
        for page in range(1, 10001):
            data = self.request(f"{path}{separator}per_page=100&page={page}")
            items = data[key] if key else data
            result.extend(items)
            if len(items) < 100:
                return result
        raise RuntimeError("Pagination limit exceeded; nothing will be deleted")

    def file(self, path):
        result = self.request(self.base + "/contents/" + path + "?ref=main")
        return base64.b64decode(result["content"]).decode(), result["sha"]

    def update_file(self, path, content, message, expected=None):
        """Retry unrelated commits; expected protects rollback/state compare-and-swap."""
        for attempt in range(6):
            try:
                current, sha = self.file(path)
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
                current, sha = None, None
            if expected is not None and current != expected and current != content:
                raise RuntimeError("Desired state changed concurrently; retry the operation")
            if current == content:
                query = urllib.parse.urlencode({"sha": "main", "path": path, "per_page": 1})
                return self.request(self.base + "/commits?" + query)[0]["sha"]
            payload = {"message": message, "branch": "main",
                       "content": base64.b64encode(content.encode()).decode()}
            if sha:
                payload["sha"] = sha
            try:
                return self.request(self.base + "/contents/" + path, "PUT", payload)["commit"]["sha"]
            except urllib.error.HTTPError as error:
                if error.code not in (409, 422) or attempt == 5:
                    raise
                time.sleep(attempt + 1)
        raise RuntimeError("Could not update infra state")


def desired_line(app, image):
    image_parts(image, app)
    return f"{app.upper()}_IMAGE={image}\n"


def read_desired(client, app):
    text, _ = client.file(f"apps/{app}/image.env")
    prefix = f"{app.upper()}_IMAGE="
    if not text.startswith(prefix) or len(text.splitlines()) != 1:
        raise ValueError("Expected exactly one image assignment in image.env")
    image = text[len(prefix):].strip()
    image_parts(image, app)
    return image
