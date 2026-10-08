"""Fail-closed private Pages checks; never enable Pages or log API responses."""

import argparse
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import stat
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parent.parent
MAX_RESPONSE_BYTES = 1_048_576
MAX_SITE_BYTES = 55_000_000
SITE_FILES = frozenset({
    "index.html", "styles.css", "app.js", "data-utils.js", "data/report.json",
})


class DeploymentError(ValueError):
    """An intentionally non-sensitive validation failure."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DeploymentError("Redirects are not permitted")


def api_origin(value):
    if not isinstance(value, str) or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise DeploymentError("Invalid API origin")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
            or parsed.port is not None or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment
            or not re.fullmatch(r"api\.github\.com|api\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.ghe\.com",
                                parsed.netloc)):
        raise DeploymentError("Invalid API origin")
    return f"https://{parsed.netloc}"


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DeploymentError("Ambiguous JSON response")
        result[key] = value
    return result


def parse_json(data):
    def invalid_constant(_):
        raise DeploymentError("Invalid JSON")

    return json.loads(data.decode("utf-8"), object_pairs_hook=unique_object,
                      parse_constant=invalid_constant)


def get_json(opener, origin, path, token):
    request = Request(origin + path, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2026-03-10",
        "User-Agent": "copilot-reporting-private-pages-preflight",
    }, method="GET")
    with opener.open(request, timeout=15) as response:
        if response.status != 200 or response.headers.get_content_type() != "application/json":
            raise DeploymentError("Repository metadata unavailable")
        length = response.headers.get("Content-Length")
        if length is not None and (not length.isdecimal() or int(length) > MAX_RESPONSE_BYTES):
            raise DeploymentError("Response exceeds limit")
        payload = response.read(MAX_RESPONSE_BYTES + 1)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise DeploymentError("Response exceeds limit")
    result = parse_json(payload)
    if not isinstance(result, dict):
        raise DeploymentError("Invalid repository metadata")
    return result


def preflight(environ=None, opener=None):
    environ = os.environ if environ is None else environ
    if environ.get("REPORTING_ENABLED") != "true":
        raise DeploymentError("Reporting is disabled")
    repository = environ.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", repository):
        raise DeploymentError("Invalid repository")
    owner, name = repository.split("/")
    if (environ.get("REPORTING_APPROVED_ORGANIZATION") != owner
            or name.lower() == f"{owner.lower()}.github.io" or name in (".", "..")):
        raise DeploymentError("An approved organization project repository is required")
    origin = api_origin(environ.get("GITHUB_API_URL", ""))
    token = environ.get("GITHUB_TOKEN", "")
    if not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise DeploymentError("Repository credential unavailable")
    opener = opener or build_opener(ProxyHandler({}), NoRedirect())
    metadata = get_json(opener, origin, f"/repos/{repository}", token)
    organization = metadata.get("owner")
    full_name = metadata.get("full_name")
    if (metadata.get("private") is not True or metadata.get("visibility") != "private"
            or not isinstance(full_name, str) or full_name.lower() != repository.lower()
            or not isinstance(organization, dict)
            or organization.get("type") != "Organization"
            or not isinstance(organization.get("login"), str)
            or organization["login"].lower() != owner.lower()):
        raise DeploymentError("Repository must be private and organization-owned")
    pages = get_json(opener, origin, f"/repos/{repository}/pages", token)
    if pages.get("public") is not False or pages.get("build_type") != "workflow":
        raise DeploymentError("Existing private workflow-based Pages is required")


def protected_path(value, *, directory):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise DeploymentError("Protected paths must be absolute")
    for item in (path, *path.parents):
        if item.is_symlink():
            raise DeploymentError("Protected paths may not contain symlinks")
        if item.stat().st_mode & 0o022:
            raise DeploymentError("Protected paths have writable ancestors")
    path = path.resolve(strict=True)
    if path == ROOT or ROOT in path.parents:
        raise DeploymentError("Protected paths must be outside the repository")
    info = path.stat()
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected_type(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise DeploymentError("Protected paths must be owner-only")
    if not directory and info.st_nlink != 1:
        raise DeploymentError("Protected files may not have multiple links")
    return path


def collection_paths(config, store, storage_mount, environ=None):
    environ = os.environ if environ is None else environ
    if environ.get("REPORTING_ENCRYPTION_APPROVED") != "true":
        raise DeploymentError("Operator encryption approval is required")
    config = protected_path(config, directory=False)
    store = protected_path(store, directory=True)
    mount = Path(storage_mount)
    if not mount.is_absolute() or ".." in mount.parts:
        raise DeploymentError("An absolute storage mount is required")
    if any(item.is_symlink() for item in (mount, *mount.parents)):
        raise DeploymentError("Storage mount may not contain symlinks")
    mount = mount.resolve(strict=True)
    if (mount == Path("/") or not os.path.ismount(mount)
            or mount not in store.parents or mount not in config.parents):
        raise DeploymentError("Protected configuration and store require the mounted volume")
    if store in config.parents:
        raise DeploymentError("Configuration must be separate from the data store")
    with config.open("rb") as handle:
        data = handle.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise DeploymentError("Configuration exceeds limit")
    settings = parse_json(data)
    if not isinstance(settings, dict):
        raise DeploymentError("Invalid configuration")
    publication = settings.get("publication")
    if (not isinstance(publication, dict) or publication.get("approved") is not True
            or publication.get("audience") != "shared-aggregates"
            or type(publication.get("minimum_cohort")) is not int
            or publication["minimum_cohort"] < 2):
        raise DeploymentError("Explicit shared-aggregate privacy approval is required")
    if api_origin(settings.get("api_origin")) != api_origin(environ.get("GITHUB_API_URL")):
        raise DeploymentError("Collection and deployment API origins must match")


def validate_site(value):
    path = Path(value)
    if any(item.is_symlink() for item in (path.absolute(), *path.absolute().parents)):
        raise DeploymentError("Site paths may not contain symlinks")
    if not path.is_dir():
        raise DeploymentError("Publication directory is missing")
    files, total = set(), 0
    for item in path.rglob("*"):
        relative = item.relative_to(path).as_posix()
        info = item.lstat()
        if relative == "data" and stat.S_ISDIR(info.st_mode):
            continue
        if relative not in SITE_FILES or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DeploymentError("Publication contains unexpected files or links")
        total += info.st_size
        files.add(relative)
    if files != SITE_FILES or total > MAX_SITE_BYTES:
        raise DeploymentError("Publication is incomplete or exceeds limit")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verify an already-private organization project Pages site")
    parser.add_argument("--config")
    parser.add_argument("--store")
    parser.add_argument("--storage-mount")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        protected = (args.config, args.store, args.storage_mount)
        if any(protected):
            if not all(protected):
                raise DeploymentError("All protected paths are required")
            collection_paths(*protected)
        if args.output:
            if not all(protected):
                raise DeploymentError("Publication requires privacy-approved protected configuration")
            validate_site(args.output)
        preflight()
    except (ValueError, TypeError, AttributeError, OSError, HTTPError, URLError,
            HTTPException, RecursionError):
        print("Deployment preflight failed; no data or API details logged.", file=sys.stderr)
        return 1
    print("Private organization project Pages preflight passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
