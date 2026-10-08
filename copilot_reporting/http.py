"""Small, bounded HTTPS transport. No API credentials ever accompany downloads."""

from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import (
    HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener,
)


class SourceError(RuntimeError):
    """An intentionally static, safe-to-display collection failure."""

    def __init__(self, reason="Source request failed.", *, unavailable=False,
                 status=None, missing_days=None):
        super().__init__(reason)
        self.unavailable = unavailable
        self.status = status
        self.missing_days = missing_days or []


def positive_limit(value, default, maximum):
    value = default if value is None else value
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError("Invalid collection limit.")
    return value


def _hostname(host):
    if not isinstance(host, str) or len(host) > 253 or host != host.lower():
        raise ValueError("Invalid HTTPS host.")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host):
        raise ValueError("Invalid HTTPS host.")
    labels = host.split(".")
    if len(labels) < 2 or any(
        not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
        for label in labels
    ):
        raise ValueError("Invalid HTTPS host.")
    if (host.endswith((".localhost", ".local", ".internal", ".test", ".invalid"))
            or labels[-1].isdigit()):
        raise ValueError("Private or IP hosts are prohibited.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise ValueError("Private or IP hosts are prohibited.")


def _https_url(url):
    if (not isinstance(url, str) or not url or len(url) > 16384
            or any(ord(char) <= 32 or ord(char) == 127 for char in url)
            or "\\" in url):
        raise ValueError("Invalid HTTPS URL.")
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.username is not None
                or parts.password is not None or parts.port not in (None, 443)
                or parts.fragment or not parts.hostname):
            raise ValueError
        _hostname(parts.hostname)
        if parts.netloc not in (parts.hostname, parts.hostname + ":443"):
            raise ValueError
    except ValueError:
        raise ValueError("Invalid HTTPS URL.") from None
    return parts


def validate_api_origin(origin):
    parts = _https_url(origin)
    if (parts.path not in ("", "/") or parts.query
            or not (parts.hostname == "api.github.com"
                    or re.fullmatch(r"api\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.ghe\.com",
                                    parts.hostname))):
        raise ValueError("Unsupported GitHub API origin.")
    return "https://" + parts.hostname


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    """Resolve once, reject non-public addresses, then pin the TLS connection."""

    def connect(self):
        if self._tunnel_host:
            raise OSError("Proxy tunnels are disabled.")
        addresses = socket.getaddrinfo(
            self.host, self.port, type=socket.SOCK_STREAM
        )
        if not addresses or any(
            not ipaddress.ip_address(item[4][0]).is_global
            or ipaddress.ip_address(item[4][0]).is_multicast for item in addresses
        ):
            raise OSError("Non-public destination.")
        last_error = None
        for item in addresses:
            sock = None
            try:
                sock = socket.socket(item[0], item[1], item[2])
                sock.settimeout(self.timeout)
                sock.connect(item[4])
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
                return
            except OSError as error:
                last_error = error
                if sock is not None:
                    sock.close()
        raise OSError("Connection failed.") from last_error


class _PublicHTTPSHandler(HTTPSHandler):
    def https_open(self, req):
        return self.do_open(
            _PublicHTTPSConnection, req, context=ssl.create_default_context()
        )


def _reject_constant(_value):
    raise ValueError("Invalid JSON number.")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key.")
        result[key] = value
    return result


def parse_json(data):
    """Preserve source decimal values as strings, rejecting ambiguous JSON."""
    try:
        return json.loads(
            data, parse_float=str, parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise SourceError("Source returned invalid JSON.") from None


@dataclass(frozen=True)
class Response:
    body: bytes
    headers: dict
    status: int

    def json(self):
        return parse_json(self.body)


class HttpClient:
    MAX_WAIT = 60
    MAX_TOTAL_WAIT = 120

    def __init__(self, *, api_origin="https://api.github.com", api_version="2026-03-10",
                 download_hosts=(), max_response_bytes=16 * 1024 * 1024,
                 max_attempts=3):
        self.api_origin = validate_api_origin(api_origin)
        if not isinstance(api_version, str) or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}", api_version):
            raise ValueError("Invalid API version.")
        self.api_version = api_version
        if not isinstance(download_hosts, (list, tuple)):
            raise ValueError("Download hosts must be an explicit list.")
        self.download_hosts = frozenset(_hostname(host) for host in download_hosts)
        self.max_response_bytes = positive_limit(
            max_response_bytes, 16 * 1024 * 1024, 256 * 1024 * 1024
        )
        self.max_attempts = positive_limit(max_attempts, 3, 5)
        self._opener = build_opener(
            ProxyHandler({}), _NoRedirect(), _PublicHTTPSHandler()
        )

    def api(self, path, token, *, query=None, method="GET", payload=None):
        if (not isinstance(path, str) or not path.startswith("/enterprises/")
                or not re.fullmatch(r"/[A-Za-z0-9_/-]+", path)
                or "//" in path or ".." in path):
            raise ValueError("Invalid API path.")
        if method not in ("GET", "POST") or (
            method == "POST" and not re.fullmatch(
                r"/enterprises/[A-Za-z0-9-]+/settings/billing/reports", path
            )
        ):
            raise ValueError("Only read-only reporting operations are allowed.")
        if not isinstance(token, str) or not token:
            raise SourceError("Credential is unavailable.", unavailable=True)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", token):
            raise SourceError("Credential format is invalid.", unavailable=True)
        headers = {
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": self.api_version,
            "User-Agent": "copilot-reporting",
            "Accept-Encoding": "identity",
        }
        body = None
        if payload is not None:
            if method != "POST":
                raise ValueError("Invalid request body.")
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, allow_nan=False).encode("utf-8")
        url = self.api_origin + path
        if query:
            url += "?" + urlencode(query)
        return self._request(url, headers, method, body)

    def download(self, url):
        try:
            parts = _https_url(url)
            if parts.hostname not in self.download_hosts:
                raise ValueError
        except ValueError:
            raise SourceError("Download destination is not approved.") from None
        return self._request(
            url, {"User-Agent": "copilot-reporting", "Accept-Encoding": "identity"},
            "GET", None,
        ).body

    def _retry_delay(self, headers, attempt):
        delays = [min(2 ** attempt, self.MAX_WAIT)]
        retry_after = headers.get("retry-after")
        if retry_after is not None:
            try:
                if re.fullmatch(r"\d+", retry_after):
                    delays.append(int(retry_after))
                else:
                    retry_date = parsedate_to_datetime(retry_after)
                    if retry_date.tzinfo is None:
                        retry_date = retry_date.replace(tzinfo=timezone.utc)
                    delays.append(max(0, retry_date.timestamp() - time.time()))
            except (ValueError, TypeError, OverflowError):
                raise SourceError("Invalid rate-limit response.") from None
        if headers.get("x-ratelimit-remaining") == "0":
            try:
                delays.append(max(0, int(headers["x-ratelimit-reset"]) - time.time()))
            except (KeyError, ValueError, OverflowError):
                raise SourceError("Invalid rate-limit response.") from None
        delay = max(delays)
        if delay > self.MAX_WAIT:
            raise SourceError("Source rate limit requires a later retry.")
        return delay

    def _request(self, url, headers, method, body):
        # POST has no idempotency key: never replay it after an ambiguous failure.
        attempts = self.max_attempts if method == "GET" else 1
        waited = 0
        for attempt in range(attempts):
            status, response_headers = None, {}
            try:
                request = Request(url, data=body, headers=headers, method=method)
                with self._opener.open(request, timeout=30) as response:
                    status = response.status
                    response_headers = {
                        key.lower(): value for key, value in response.headers.items()
                    }
                    if status not in (200, 202):
                        raise SourceError("Unexpected source response.", status=status)
                    if response_headers.get("content-encoding", "identity") != "identity":
                        raise SourceError("Compressed source responses are unsupported.")
                    length = response_headers.get("content-length")
                    if length is not None:
                        if not length.isdigit() or int(length) > self.max_response_bytes:
                            raise SourceError("Source response exceeds the byte limit.")
                    data = response.read(self.max_response_bytes + 1)
                    if len(data) > self.max_response_bytes:
                        raise SourceError("Source response exceeds the byte limit.")
                    if length is not None and len(data) != int(length):
                        raise SourceError("Source response was incomplete.")
                    return Response(data, response_headers, status)
            except HTTPError as error:
                status = error.code
                response_headers = {
                    key.lower(): value for key, value in error.headers.items()
                } if error.headers else {}
                error.close()
            except (URLError, OSError, http.client.HTTPException, ValueError):
                pass
            retryable = (
                status is None or status in (429, 500, 502, 503, 504)
                or (status == 403 and (
                    "retry-after" in response_headers
                    or response_headers.get("x-ratelimit-remaining") == "0"
                ))
            )
            if retryable and attempt + 1 < attempts:
                delay = self._retry_delay(response_headers, attempt)
                waited += delay
                if waited > self.MAX_TOTAL_WAIT:
                    raise SourceError("Source retry wait limit reached.")
                time.sleep(delay)
                continue
            if 300 <= (status or 0) < 400:
                raise SourceError("Source redirects are prohibited.", status=status)
            raise SourceError(
                "Source access is unavailable." if status in (401, 403, 404)
                else "Source request failed.",
                unavailable=status in (401, 403, 404), status=status,
            ) from None
