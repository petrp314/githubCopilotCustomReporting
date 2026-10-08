"""Offline standard-library tests; fixtures contain no enterprise credentials."""

from datetime import date
import io
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import stat
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
import uuid

from copilot_reporting.collector import Collector, _prepare_state_directory
from copilot_reporting.http import (
    HttpClient, Response, SourceError, _NoRedirect, _PublicHTTPSConnection,
    parse_json, validate_api_origin,
)
from copilot_reporting.token_import import (
    TokenImportError, UnsupportedTokenSchema, read_tokens, read_tokens_bytes,
)


DAY = "2026-10-01"
NEXT_DAY = "2026-10-02"
CSV = b"date,model,username,input,output,cache_read,cache_write\n2026-10-01,model-a,person,10,2,3,0\n"
HOST = "reports.example.com"
REPORT_ID = "8f84e8a4-42fd-4f69-8150-ff413bd88bb0"


def response(payload, headers=None, status=200):
    return Response(json.dumps(payload).encode(), headers or {}, status)


def job(status="processing", **overrides):
    return {
        "id": REPORT_ID, "status": status, "report_type": "ai_credit",
        "start_date": DAY, "end_date": DAY,
        **overrides,
    }


class WireResponse(io.BytesIO):
    def __init__(self, body=b"{}", status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}


class LocalFixture(unittest.TestCase):
    def setUp(self):
        self.directory = Path.cwd() / (".collector-tests-" + uuid.uuid4().hex)
        self.directory.mkdir(mode=0o700)
        self.addCleanup(shutil.rmtree, self.directory)


class TokenImportTests(LocalFixture):
    def test_documented_csv_canonicalizes_exact_integer_strings(self):
        rows = read_tokens_bytes(CSV, DAY, DAY)
        self.assertEqual(rows, [{
            "day": DAY, "model": "model-a", "username": "person", "user_id": None,
            "cost_center_id": None, "cost_center_name": None,
            "input": "10", "output": "2", "cache_read": "3", "cache_write": "0",
        }])

    def test_missing_categories_are_none_not_zero(self):
        row = read_tokens_bytes(
            b"day,model,input,output\n2026-10-01,m,00001,\n", DAY, DAY
        )[0]
        self.assertEqual(row["input"], "1")
        self.assertIsNone(row["output"])
        self.assertIsNone(row["cache_read"])
        self.assertIsNone(row["cache_write"])

    def test_local_path_uses_same_parser(self):
        path = self.directory / "report.csv"
        path.write_bytes(CSV)
        self.assertEqual(read_tokens(path, DAY, DAY), read_tokens_bytes(CSV, DAY, DAY))

    def test_nonregular_imports_and_symlinks_are_rejected(self):
        report = self.directory / "regular.csv"
        report.write_bytes(CSV)
        link = self.directory / "link.csv"
        link.symlink_to(report)
        fifo = self.directory / "fifo.csv"
        os.mkfifo(fifo)
        for path in (link, fifo, self.directory):
            with self.subTest(path=path.name), self.assertRaises(TokenImportError):
                read_tokens(path, DAY, DAY)

    def test_optional_documented_billing_columns_are_not_tokens(self):
        rows = read_tokens_bytes(
            b"date,model,input,quantity,net_amount,sku,cost_center_name\n"
            b"2026-10-01,m,42,9999,12.25,sku,Finance\n", DAY, DAY
        )
        self.assertEqual(rows[0]["input"], "42")
        self.assertEqual(rows[0]["cost_center_name"], "Finance")
        self.assertNotIn("quantity", rows[0])
        self.assertIsNone(rows[0]["output"])

    def test_unknown_duplicate_and_alias_colliding_headers_fail(self):
        for header in (
            "day,model,input,input", "day,date,model,input", "Date,model,input",
            "day,model,input,made_up", "day,model,input,", "day,model, input",
        ):
            with self.subTest(header=header), self.assertRaises(TokenImportError):
                read_tokens_bytes(header.encode() + b"\n", DAY, DAY)

    def test_no_token_columns_or_all_missing_are_unavailable(self):
        for data in (
            b"day,model,quantity\n2026-10-01,m,300\n",
            b"day,model,input,output\n2026-10-01,m,,\n",
            b"date,sku,input\n2026-10-01,x,12\n",
        ):
            with self.subTest(data=data), self.assertRaises(UnsupportedTokenSchema):
                read_tokens_bytes(data, DAY, DAY)

    def test_header_only_supported_schema_is_valid_empty_report(self):
        self.assertEqual(read_tokens_bytes(b"day,model,input\n", DAY, DAY), [])

    def test_bad_counts_do_not_coerce(self):
        for value in ("-1", "1.0", "1e3", "+2", "NaN", "inf", " 2", "2 ", "١", "=1+1"):
            with self.subTest(value=value), self.assertRaises(TokenImportError):
                read_tokens_bytes(
                    ("day,model,input\n2026-10-01,m," + value + "\n").encode(), DAY, DAY
                )

    def test_malformed_and_missing_cells_fail(self):
        for data in (
            b"", b"\xff", b"day,model,input\n2026-10-01,m\n",
            b"day,model,input\n2026-10-01,m,1,2\n",
            b'day,model,input\n2026-10-01,"unterminated,1\n',
            b"day,model,input\n2026-10-01,,1\n",
            b"day,model,input\n2026-10-01,m\x00,1\n",
        ):
            with self.subTest(data=data), self.assertRaises(TokenImportError):
                read_tokens_bytes(data, DAY, DAY)

    def test_period_and_duplicate_grains_fail(self):
        for data in (
            b"day,model,input\n2026-10-02,m,1\n",
            b"day,model,input\n2026-02-30,m,1\n",
            b"day,model,input\n2026-10-01,m,1\n2026-10-01,m,2\n",
        ):
            with self.subTest(data=data), self.assertRaises(TokenImportError):
                read_tokens_bytes(data, DAY, DAY)

    def test_oversize_bytes_rows_and_invalid_period_fail(self):
        with self.assertRaises(TokenImportError):
            read_tokens_bytes(CSV, DAY, DAY, max_bytes=10)
        with self.assertRaises(TokenImportError):
            read_tokens_bytes(CSV + b"2026-10-01,other,person,10,2,3,0\n",
                              DAY, DAY, max_rows=1)
        with self.assertRaises(TokenImportError):
            read_tokens_bytes(CSV, NEXT_DAY, DAY)
        with self.assertRaises(TokenImportError):
            read_tokens_bytes(CSV, "20261001", DAY)

    def test_errors_never_include_employee_values_or_path(self):
        private_path = self.directory / "private-person-name.csv"
        with self.assertRaises(TokenImportError) as caught:
            read_tokens(private_path, DAY, DAY)
        self.assertNotIn("private-person", str(caught.exception))


class HttpTests(unittest.TestCase):
    def make_client(self, **overrides):
        client = HttpClient(download_hosts=[HOST], max_attempts=1, **overrides)
        client._opener = Mock()
        return client

    def test_origin_restricted_to_github_cloud_and_explicit_residency(self):
        self.assertEqual(validate_api_origin("https://api.github.com/"), "https://api.github.com")
        self.assertEqual(validate_api_origin("https://api.acme.ghe.com"),
                         "https://api.acme.ghe.com")
        for origin in (
            "http://api.github.com", "https://github.com", "https://api.github.com.evil.com",
            "https://api.github.com@evil.com", "https://api.github.com/path",
            "https://api.github.com/?a=b", "https://api.github.com/#secret",
            "https://api.github.com:8443", "https://127.0.0.1", "https://localhost",
            "https://api.ghe.com", "https://api.github.com\\@evil.com",
        ):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                validate_api_origin(origin)

    def test_api_uses_authorization_and_pinned_version(self):
        client = self.make_client()
        client._opener.open.return_value = WireResponse()
        client.api("/enterprises/acme/copilot/billing/seats", "fixture_credential",
                   query={"page": 1, "per_page": 100})
        request = client._opener.open.call_args.args[0]
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["authorization"].split(), ["Bearer", "fixture_credential"])
        self.assertEqual(headers["x-github-api-version"], "2026-03-10")
        self.assertEqual(parse_qs(urlsplit(request.full_url).query),
                         {"page": ["1"], "per_page": ["100"]})

    def test_download_never_sends_api_headers(self):
        client = self.make_client()
        client._opener.open.return_value = WireResponse(b"report")
        self.assertEqual(client.download("https://" + HOST + "/part?sig=fixture"), b"report")
        headers = {
            key.lower(): value for key, value in
            client._opener.open.call_args.args[0].header_items()
        }
        self.assertNotIn("authorization", headers)
        self.assertNotIn("cookie", headers)
        self.assertNotIn("x-github-api-version", headers)

    def test_download_exact_allowlist_no_userinfo_no_ip_or_private_names(self):
        client = self.make_client()
        for url in (
            "http://" + HOST + "/file", "https://evil." + HOST + "/file",
            "https://" + HOST + ".evil.com/file", "https://person@" + HOST + "/file",
            "https://127.0.0.1/file", "https://[::1]/file",
            "https://internal.local/file", "https://" + HOST + ":444/file",
            "https://" + HOST + "/file\nheader", "https://" + HOST + "/file#fragment",
        ):
            with self.subTest(url=url), self.assertRaises(SourceError):
                client.download(url)
        client._opener.open.assert_not_called()
        for host in ("*.example.com", "127.0.0.1", "internal.local", "localhost"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                HttpClient(download_hosts=[host])

    def test_redirects_are_disabled_and_error_does_not_leak_url(self):
        client = self.make_client()
        url = "https://" + HOST + "/private?sig=DO_NOT_LOG"
        client._opener.open.side_effect = HTTPError(
            url, 302, "DO_NOT_LOG", {"Location": "https://evil.com"}, io.BytesIO()
        )
        with self.assertRaises(SourceError) as caught:
            client.download(url)
        self.assertNotIn("DO_NOT_LOG", str(caught.exception))
        self.assertEqual(client._opener.open.call_count, 1)
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, url))

    def test_size_content_length_and_encoding_bounds(self):
        for body, headers in (
            (b"123456", {}), (b"123", {"Content-Length": "6"}),
            (b"12", {"Content-Length": "3"}), (b"{}", {"Content-Encoding": "gzip"}),
        ):
            with self.subTest(body=body, headers=headers):
                client = self.make_client(max_response_bytes=5)
                client._opener.open.return_value = WireResponse(body, headers=headers)
                with self.assertRaises(SourceError):
                    client.download("https://" + HOST + "/file")

    def test_retry_after_is_respected(self):
        client = HttpClient(download_hosts=[HOST], max_attempts=2)
        client._opener = Mock()
        client._opener.open.side_effect = [
            HTTPError("https://" + HOST, 429, "secret", {"Retry-After": "7"}, io.BytesIO()),
            WireResponse(b"done"),
        ]
        with patch("copilot_reporting.http.time.sleep") as sleep:
            self.assertEqual(client.download("https://" + HOST), b"done")
        sleep.assert_called_once_with(7)

    def test_long_retry_wait_fails_without_early_request(self):
        client = HttpClient(download_hosts=[HOST], max_attempts=3)
        client._opener = Mock()
        client._opener.open.side_effect = HTTPError(
            "https://" + HOST, 429, "secret", {"Retry-After": "3600"}, io.BytesIO()
        )
        with patch("copilot_reporting.http.time.sleep") as sleep, self.assertRaises(SourceError):
            client.download("https://" + HOST)
        sleep.assert_not_called()
        self.assertEqual(client._opener.open.call_count, 1)

    def test_rate_limit_reset_and_http_date_retry_after(self):
        client = self.make_client()
        with patch("copilot_reporting.http.time.time", return_value=10):
            self.assertEqual(client._retry_delay(
                {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "20"}, 0), 10)
            self.assertEqual(client._retry_delay(
                {"retry-after": "Thu, 01 Jan 1970 00:00:30 GMT"}, 0), 20)

    def test_failed_post_is_not_retried(self):
        client = HttpClient(max_attempts=5)
        client._opener = Mock()
        client._opener.open.side_effect = URLError("secret signed URL")
        with self.assertRaises(SourceError) as caught:
            client.api("/enterprises/acme/settings/billing/reports", "fixture_credential",
                       method="POST", payload={})
        self.assertEqual(client._opener.open.call_count, 1)
        self.assertNotIn("secret", str(caught.exception))

    def test_only_reporting_post_and_safe_paths(self):
        client = self.make_client()
        for path, method in (
            ("/enterprises/acme/settings/billing/cost-centers", "POST"),
            ("/enterprises/acme/copilot/billing/seats", "DELETE"),
            ("/enterprises/acme/../seats", "GET"),
            ("/enterprises/acme/seats?token=bad", "GET"),
            ("https://evil.com/enterprises/acme", "GET"),
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                client.api(path, "fixture_credential", method=method)
        client._opener.open.assert_not_called()

    def test_no_private_dns_connections_and_pin_public_dns_result(self):
        for address in (
            "127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1", "224.0.0.1",
        ):
            with self.subTest(address=address):
                connection = _PublicHTTPSConnection(HOST)
                with patch("copilot_reporting.http.socket.getaddrinfo",
                           return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                                          (address, 443))]), \
                        patch("copilot_reporting.http.socket.socket") as create_socket, \
                        self.assertRaises(OSError):
                    connection.connect()
                create_socket.assert_not_called()
        context = Mock(spec=ssl.SSLContext)
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        connection = _PublicHTTPSConnection(HOST, context=context)
        with patch("copilot_reporting.http.socket.getaddrinfo",
                   return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                                  ("140.82.114.6", 443))]), \
                patch("copilot_reporting.http.socket.socket") as create_socket:
            connection.connect()
        create_socket.return_value.connect.assert_called_once_with(("140.82.114.6", 443))
        context.wrap_socket.assert_called_once_with(create_socket.return_value,
                                                   server_hostname=HOST)

    def test_json_preserves_decimals_and_rejects_duplicate_keys_and_nonfinite(self):
        self.assertEqual(parse_json(b'{"amount":0.1234567890123456789}')["amount"],
                         "0.1234567890123456789")
        for data in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b"broken"):
            with self.subTest(data=data), self.assertRaises(SourceError):
                parse_json(data)


class CollectorTests(LocalFixture):
    def setUp(self):
        super().setUp()
        self.env = patch.dict(os.environ, {"COPILOT_REPORTING_TOKEN": "fixture_credential"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.state_patch = patch("copilot_reporting.collector._prepare_state_directory",
                                 return_value=self.directory)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.collector = Collector(
            {"enterprise": "acme", "download_hosts": [HOST], "poll_attempts": 1},
            self.directory,
        )
        self.collector.http.api = Mock(side_effect=AssertionError("Unexpected API request"))
        self.collector.http.download = Mock(side_effect=AssertionError("Unexpected download"))

    def empty_other_sources(self):
        for method in ("_seats", "_cost_centers", "_usage", "_billing", "_billing_centers"):
            setattr(self.collector, method, Mock(return_value=[]))

    def test_full_collection_routes_pagination_and_all_report_shards(self):
        api_calls = []
        seats = [
            {"assignee": {"id": 1}, "organization": {"id": number}}
            for number in range(100)
        ]
        downloads = {
            "https://" + HOST + "/users-a": b'{"day":"2026-10-01","user_id":1}\n',
            "https://" + HOST + "/users-b": b'{"day":"2026-10-01","user_id":2}\n',
            "https://" + HOST + "/enterprise": b'{"day":"2026-10-01","daily_active_users":2}\n',
            "https://" + HOST + "/tokens-a": CSV,
            "https://" + HOST + "/tokens-b": CSV.replace(b"model-a", b"model-b"),
        }

        def api(path, token, *, query=None, method="GET", payload=None):
            api_calls.append((path, query, method, payload, token))
            if path.endswith("/copilot/billing/seats"):
                if query["page"] == 1:
                    return response({"total_seats": 1, "seats": seats}, {
                        "link": '<https://evil.com/steal>; rel="next"'
                    })
                return response({"total_seats": 1, "seats": [{"assignee": None}]})
            if path.endswith("/cost-centers"):
                return response({"costCenters": [{
                    "id": query["state"], "name": query["state"], "state": query["state"]
                }]})
            if "/cost-centers/" in path:
                center = path.rsplit("/", 1)[-1]
                return response({
                    "id": center, "name": center, "state": center,
                    "resources": [{"type": "user", "name": center + str(query["page"])}],
                    "has_next_page": center == "active" and query["page"] == 1,
                })
            if path.endswith("/users-1-day"):
                self.assertEqual(query, {"day": DAY})
                return response({"report_day": DAY, "download_links": [
                    "https://" + HOST + "/users-a", "https://" + HOST + "/users-b",
                ]})
            if path.endswith("/enterprise-1-day"):
                return response({"report_day": DAY,
                                 "download_links": ["https://" + HOST + "/enterprise"]})
            if path.endswith("/ai_credit/usage"):
                self.assertEqual(
                    {key: value for key, value in query.items() if key != "cost_center_id"},
                    {"year": 2026, "month": 10, "day": 1},
                )
                self.assertLessEqual(set(query), {"year", "month", "day", "cost_center_id"})
                return response({"enterprise": "acme", "timePeriod": {
                    "year": 2026, "month": 10, "day": 1,
                },
                                 "usageItems": [{"unitType": "AI credits", "netAmount": "0.12"}]})
            if path.endswith("/reports") and method == "POST":
                self.assertEqual(payload, {
                    "report_type": "ai_credit", "start_date": DAY,
                    "end_date": DAY, "send_email": False,
                })
                return response(job(), status=202)
            if path.endswith("/reports/" + REPORT_ID):
                return response(job("completed", download_urls=[
                    "https://" + HOST + "/tokens-a", "https://" + HOST + "/tokens-b",
                ]))
            self.fail("Unexpected route")

        self.collector.http.api.side_effect = api
        self.collector.http.download.side_effect = downloads.__getitem__
        snapshot = self.collector.collect(DAY, DAY)
        self.assertEqual(snapshot["schema_version"], 1)
        sources = snapshot["sources"]
        self.assertTrue(all(source["status"] == "ok" for source in sources.values()), sources)
        self.assertEqual(sources["seats"]["data"]["total_seats"], 1)
        self.assertEqual(len(sources["seats"]["data"]["seats"]), 101)
        self.assertEqual(len(sources["cost_centers"]["data"][0]["resources"]), 2)
        self.assertEqual(sources["cost_centers"]["data"][1]["state"], "deleted")
        self.assertEqual(len(sources["usage"]["data"]), 2)
        self.assertEqual(len(sources["tokens"]["data"]), 2)
        self.assertEqual(sources["billing"]["data"][0]["day"], DAY)
        self.assertEqual(sources["billing"]["data"][0]["netAmount"], "0.12")
        self.assertEqual(len(sources["billing"]["data"]), 1)
        self.assertEqual(sources["billing"]["data"][0]["scope"], "enterprise")
        self.assertEqual(sources["billing"]["data"][0]["cost_center_id"], "__enterprise__")
        self.assertIsNone(sources["billing"]["data"][0]["currency"])
        self.assertEqual(sources["billing_centers"]["scope"], "cost_center")
        self.assertEqual(len(sources["billing_centers"]["data"]), 3)
        self.assertEqual(
            {row["cost_center_id"] for row in sources["billing_centers"]["data"]},
            {"active", "deleted", "enterprise-only"},
        )
        self.assertTrue(all(
            row["scope"] == "cost_center" for row in sources["billing_centers"]["data"]
        ))
        self.assertTrue(all(call[0].startswith("/enterprises/acme/") for call in api_calls))
        self.assertEqual(sum(call[2] == "POST" for call in api_calls), 1)
        self.assertTrue(snapshot["collected_at"].endswith("Z"))

    def test_separate_seat_credential_only_used_for_seats(self):
        self.collector.seat_token_env = "SEAT_FIXTURE"
        with patch.dict(os.environ, {"SEAT_FIXTURE": "seat_fixture"}):
            self.collector.http.api.side_effect = [
                response({"total_seats": 0, "seats": []}),
                response({"costCenters": []}), response({"costCenters": []}),
            ]
            self.collector._seats()
            self.collector._cost_centers()
        tokens = [call.args[1] for call in self.collector.http.api.call_args_list]
        self.assertEqual(tokens, ["seat_fixture", "fixture_credential", "fixture_credential"])

    def test_missing_credentials_are_unavailable_not_zero_or_submitted(self):
        self.collector.http = HttpClient(download_hosts=[HOST])
        with patch.dict(os.environ, {}, clear=True):
            snapshot = self.collector.collect(DAY, DAY)
        for source in snapshot["sources"].values():
            self.assertEqual(source["status"], "unavailable")
            self.assertNotIn("data", source)
        self.assertEqual(list(self.directory.glob("*.json")), [])

    def test_failed_seat_page_discards_partial_grants(self):
        self.collector.http.api.side_effect = [
            response({"total_seats": 1, "seats": [{"assignee": {"id": 1}}]},
                     {"link": '<https://api.github.com/page2>; rel="next"'}),
            SourceError("Source request failed."),
        ]
        with self.assertRaises(SourceError):
            self.collector._seats()

    def test_seat_pagination_uses_grants_not_unique_total_and_is_bounded(self):
        self.collector.max_pages = 1
        self.collector.http.api.side_effect = [
            response({"total_seats": 1, "seats": [{"assignee": {"id": 1}}] * 100})
        ]
        with self.assertRaisesRegex(SourceError, "page limit"):
            self.collector._seats()

    def test_changed_seat_total_or_repeated_page_fails(self):
        for second in (
            {"total_seats": 2, "seats": []},
            {"total_seats": 1, "seats": [{"assignee": {"id": 1}}]},
        ):
            self.collector.http.api.side_effect = [
                response({"total_seats": 1, "seats": [{"assignee": {"id": 1}}]},
                         {"link": '<ignored>; rel="next"'}),
                response(second),
            ]
            with self.assertRaises(SourceError):
                self.collector._seats()

    def test_cost_center_id_injection_never_requested(self):
        self.collector.http.api.side_effect = [
            response({"costCenters": [{"id": "../reports?x=1"}]}),
            response({"costCenters": []}),
        ]
        with self.assertRaises(SourceError):
            self.collector._cost_centers()
        self.assertEqual(self.collector.http.api.call_count, 2)

    def test_cost_center_detail_failure_discards_source(self):
        self.collector.http.api.side_effect = [
            response({"costCenters": [{"id": "center"}]}),
            response({"costCenters": []}),
            response({"id": "center", "resources": [{"type": "user", "name": "a"}],
                      "has_next_page": True}),
            SourceError("Source request failed."),
        ]
        with self.assertRaises(SourceError):
            self.collector._cost_centers()

    def test_usage_failed_shard_is_atomic_and_marks_missing_day(self):
        self.collector.http.api.side_effect = [
            response({"report_day": DAY, "download_links": ["a", "b"]}),
        ]
        self.collector.http.download.side_effect = [
            b'{"day":"2026-10-01","user_id":1}\n', SourceError("Download failed."),
        ]
        with self.assertRaises(SourceError) as caught:
            self.collector._usage(date.fromisoformat(DAY), date.fromisoformat(DAY), "users")
        self.assertEqual(caught.exception.missing_days, [DAY])

    def test_usage_missing_days_are_not_a_zero_success(self):
        self.empty_other_sources()
        self.collector._usage.side_effect = SourceError(
            "Daily usage reports are incomplete.", unavailable=True, missing_days=[DAY]
        )
        self.collector._tokens = Mock(return_value=[])
        snapshot = self.collector.collect(DAY, DAY)
        for name in ("usage", "enterprise_usage"):
            self.assertEqual(snapshot["sources"][name]["missing_days"], [DAY])
            self.assertEqual(snapshot["sources"][name]["status"], "unavailable")
            self.assertEqual(snapshot["sources"][name]["error_code"], "missing_days")
            self.assertNotIn("data", snapshot["sources"][name])

    def test_source_error_codes_are_static_and_match_store_diagnostics(self):
        self.empty_other_sources()
        cases = [
            (SourceError("Source access is unavailable.", status=401, unavailable=True),
             "access_denied", "unavailable"),
            (SourceError("Source access is unavailable.", status=403, unavailable=True),
             "access_denied", "unavailable"),
            (SourceError("Source access is unavailable.", status=404, unavailable=True),
             "unsupported_or_missing", "unavailable"),
            (SourceError("Source request failed.", status=429), "rate_limited", "error"),
            (SourceError("Source request failed.", status=500), "collection_failed", "error"),
            (SourceError("Source request failed."), "collection_failed", "error"),
            (UnsupportedTokenSchema("Report has no usable token columns."),
             "unsupported_schema", "unavailable"),
            (TokenImportError("Malformed report."), "invalid_import", "error"),
            (ValueError("Never expose this value."), "collection_failed", "error"),
        ]
        for error, code, status in cases:
            with self.subTest(code=code, status=getattr(error, "status", None)):
                self.collector._tokens = Mock(side_effect=error)
                source = self.collector.collect(DAY, DAY)["sources"]["tokens"]
                self.assertEqual(source["error_code"], code)
                self.assertEqual(source["status"], status)
                self.assertNotIn("data", source)
                self.assertNotIn("Never expose", source["reason"])

    def test_valid_empty_source_has_no_error_diagnostic(self):
        self.empty_other_sources()
        self.collector._tokens = Mock(return_value=[])
        source = self.collector.collect(DAY, DAY)["sources"]["tokens"]
        self.assertEqual(source["status"], "ok")
        self.assertEqual(source["data"], [])
        self.assertNotIn("error_code", source)
        self.assertNotIn("reason", source)

    def test_usage_duplicate_or_wrong_day_or_invalid_ndjson_fails(self):
        for content in (
            b'{"day":"2026-10-01","user_id":1}\n{"day":"2026-10-01","user_id":1}\n',
            b'{"day":"2026-10-02","user_id":1}\n',
            b'{"day":"2026-10-01"}\nnot-json\n',
        ):
            self.collector.http.api.side_effect = [
                response({"report_day": DAY, "download_links": ["part"]})
            ]
            self.collector.http.download.side_effect = [content]
            with self.subTest(content=content), self.assertRaises(SourceError):
                self.collector._usage_day(DAY, "users")

    def test_usage_row_limit_applies_across_shards_and_days(self):
        self.collector.max_rows = 1
        self.collector.http.api.side_effect = [
            response({"report_day": DAY, "download_links": ["a", "b"]})
        ]
        self.collector.http.download.side_effect = [
            b'{"day":"2026-10-01","user_id":1}\n', b'{"day":"2026-10-01","user_id":2}\n'
        ]
        with self.assertRaises(SourceError):
            self.collector._usage_day(DAY, "users")
        self.collector._usage_day = Mock(side_effect=[[{"day": DAY}], [{"day": NEXT_DAY}]])
        with self.assertRaises(SourceError):
            self.collector._usage(date.fromisoformat(DAY), date.fromisoformat(NEXT_DAY), "users")

    def test_billing_scope_and_period_are_validated(self):
        self.collector.http.api.side_effect = [
            response({"enterprise": "other-enterprise", "timePeriod": {
                "year": 2026, "month": 10, "day": 1,
            }, "usageItems": []})
        ]
        with self.assertRaises(SourceError):
            self.collector._billing(date.fromisoformat(DAY), date.fromisoformat(DAY))

    def test_billing_injects_requested_dimensions_without_losing_decimal_precision(self):
        self.collector.billing_currency = "EUR"
        self.collector.http.api.side_effect = [Response(
            b'{"enterprise":"acme","timePeriod":{"year":2026,"month":10,"day":1},'
            b'"costCenter":{"id":"center","name":"Renamed center"},'
            b'"usageItems":[{"model":"m","unitType":"AI credits",'
            b'"pricePerUnit":0.000000000000000019,"netAmount":0.1234567890123456789}]}',
            {}, 200,
        )]
        row = self.collector._billing(
            date.fromisoformat(DAY), date.fromisoformat(DAY), "center", "Older name"
        )[0]
        self.assertEqual(row["day"], DAY)
        self.assertEqual(row["scope"], "cost_center")
        self.assertEqual(row["cost_center_id"], "center")
        self.assertEqual(row["cost_center_name"], "Renamed center")
        self.assertEqual(row["currency"], "EUR")
        self.assertEqual(row["unitType"], "AI credits")
        self.assertEqual(row["netAmount"], "0.1234567890123456789")
        self.assertEqual(row["pricePerUnit"], "0.000000000000000019")
        self.assertEqual(self.collector.http.api.call_args.kwargs["query"], {
            "year": 2026, "month": 10, "day": 1, "cost_center_id": "center",
        })

    def test_billing_rejects_unexpected_or_mismatched_top_level_cost_center(self):
        for requested in (None, "expected-center", "none"):
            self.collector.http.api.side_effect = [response({
                "enterprise": "acme", "timePeriod": {"year": 2026, "month": 10, "day": 1},
                "costCenter": {"id": "another-center", "name": "Other"},
                "usageItems": [],
            })]
            with self.subTest(requested=requested), self.assertRaises(SourceError):
                self.collector._billing(
                    date.fromisoformat(DAY), date.fromisoformat(DAY), requested
                )

    def test_premium_request_selection_preserves_units_and_leaves_currency_unknown(self):
        collector = Collector({
            "enterprise": "acme", "billing_report": "premium_request", "billing_currency": "Unknown",
        }, self.directory)
        collector.http.api = Mock(return_value=response({
            "enterprise": "acme", "timePeriod": {"year": 2026, "month": 10, "day": 1},
            "usageItems": [{"unitType": "requests", "grossQuantity": "2", "netAmount": "0.5"}],
        }))
        row = collector._billing(date.fromisoformat(DAY), date.fromisoformat(DAY))[0]
        self.assertTrue(collector.http.api.call_args.args[0].endswith("/premium_request/usage"))
        self.assertEqual(row["report_type"], "premium_request")
        self.assertEqual(row["unitType"], "requests")
        self.assertIsNone(row["currency"])

    def test_failed_billing_day_is_atomic_but_other_sources_succeed(self):
        self.empty_other_sources()
        self.collector._billing = Collector._billing.__get__(self.collector)
        self.collector._tokens = Mock(return_value=[])
        self.collector.http.api.side_effect = [
            response({
                "enterprise": "acme", "timePeriod": {"year": 2026, "month": 10, "day": 1},
                "usageItems": [{"unitType": "AI credits", "netAmount": "12.50"}],
            }),
            SourceError("Source request failed."),
        ]
        sources = self.collector.collect(DAY, NEXT_DAY)["sources"]
        self.assertEqual(sources["billing"]["status"], "error")
        self.assertNotIn("data", sources["billing"])
        self.assertTrue(all(
            source["status"] == "ok" for key, source in sources.items() if key != "billing"
        ))

    def test_partition_failure_does_not_invalidate_enterprise_total(self):
        self.empty_other_sources()
        self.collector._billing_centers = Collector._billing_centers.__get__(self.collector)
        self.collector._cost_centers = Mock(return_value=[
            {"id": "active", "name": "Active", "state": "active"},
            {"id": "deleted", "name": "Deleted", "state": "deleted"},
        ])
        self.collector._billing.side_effect = [
            [{"scope": "enterprise", "cost_center_id": "__enterprise__", "netAmount": "20"}],
            [{"scope": "cost_center", "cost_center_id": "active", "netAmount": "10"}],
            SourceError("Source access is unavailable.", unavailable=True),
        ]
        self.collector._tokens = Mock(return_value=[])
        sources = self.collector.collect(DAY, DAY)["sources"]
        self.assertEqual(sources["billing"]["status"], "ok")
        self.assertEqual(len(sources["billing"]["data"]), 1)
        self.assertEqual(sources["billing_centers"]["status"], "unavailable")
        self.assertNotIn("data", sources["billing_centers"])

    def test_missing_cost_center_inventory_does_not_synthesize_partitions(self):
        with self.assertRaisesRegex(SourceError, "complete cost center inventory"):
            self.collector._billing_centers(
                date.fromisoformat(DAY), date.fromisoformat(DAY), {"status": "error"}
            )
        self.collector.http.api.assert_not_called()

    def test_empty_inventory_still_queries_unallocated_partition(self):
        self.collector.http.api.side_effect = [response({
            "enterprise": "acme", "timePeriod": {"year": 2026, "month": 10, "day": 1},
            "usageItems": [{"unitType": "AI credits", "netAmount": "1"}],
        })]
        rows = self.collector._billing_centers(
            date.fromisoformat(DAY), date.fromisoformat(DAY), {"status": "ok", "data": []}
        )
        self.assertEqual(rows[0]["cost_center_id"], "enterprise-only")
        self.assertEqual(rows[0]["cost_center_name"], "Enterprise Only")
        self.assertEqual(rows[0]["scope"], "cost_center")
        self.assertEqual(self.collector.http.api.call_args.kwargs["query"]["cost_center_id"], "none")

    def test_billing_config_validation_rejects_unsupported_sources_and_currency(self):
        for extra in (
            {"billing_report": "summary"}, {"billing_report": "../usage"},
            {"billing_currency": "not-currency"}, {"billing_currency": 42},
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                Collector({"enterprise": "acme", **extra}, self.directory)

    def test_complete_billing_rows_match_domain_normalization_contract(self):
        from copilot_reporting.domain import normalize

        usage_item = {
            "product": "Copilot", "sku": "copilot-ai-credits", "model": "model-a",
            "unitType": "AI credits", "pricePerUnit": "0.01",
            "grossQuantity": "2.001", "discountQuantity": "1.001", "netQuantity": "1",
            "grossAmount": "0.02001", "discountAmount": "0.01001", "netAmount": "0.01",
        }
        self.collector.http.api.side_effect = [
            response({
                "enterprise": "acme", "timePeriod": {"year": 2026, "month": 10, "day": 1},
                "usageItems": [usage_item],
            }),
            response({
                "enterprise": "acme", "timePeriod": {"year": 2026, "month": 10, "day": 1},
                "costCenter": {"id": "center", "name": "Finance"}, "usageItems": [usage_item],
            }),
        ]
        enterprise_rows = self.collector._billing(
            date.fromisoformat(DAY), date.fromisoformat(DAY)
        )
        center_rows = self.collector._billing(
            date.fromisoformat(DAY), date.fromisoformat(DAY), "center", "Finance"
        )
        enterprise = normalize("billing", enterprise_rows)[0]
        center = normalize("billing_centers", center_rows)[0]
        self.assertEqual(enterprise["cost_center_id"], "__enterprise__")
        self.assertEqual(center["cost_center_id"], "center")
        self.assertEqual(center["cost_center_name"], "Finance")
        self.assertEqual(center["gross_amount"], "0.02001")
        self.assertEqual(center["currency"], "Unknown")
        self.assertEqual(center["unit"], "AI credits")

    def test_completed_export_rows_reused_without_post_or_download(self):
        self.collector.http.api.side_effect = [
            response(job(), status=202),
            response(job("completed", download_urls=["https://" + HOST + "/tokens"])),
        ]
        self.collector.http.download.side_effect = [CSV]
        rows = self.collector._export(DAY, DAY)
        self.assertEqual(self.collector._export(DAY, DAY), rows)
        self.assertEqual(self.collector.http.api.call_count, 2)
        self.assertEqual(self.collector.http.download.call_count, 1)
        persisted = list(self.directory.glob("*.json"))[0]
        self.assertRegex(persisted.name, r"^export-[0-9a-f]{64}\.json$")
        state = json.loads(persisted.read_text())
        self.assertEqual(state["status"], "completed")
        self.assertGreater(state["expires_at"], state["created_at"])
        self.assertNotIn("https://", persisted.read_text())
        self.assertEqual(stat.S_IMODE(persisted.stat().st_mode), 0o600)

    def test_pending_job_persists_across_collector_instances_without_second_post(self):
        self.collector.http.api.side_effect = [
            response(job(), status=202), response(job())
        ]
        with self.assertRaisesRegex(SourceError, "still processing"):
            self.collector._export(DAY, DAY)
        resumed = Collector({"enterprise": "acme", "poll_attempts": 1}, self.directory)
        resumed.http.api = Mock(return_value=response(
            job("completed", download_urls=["https://" + HOST + "/tokens"])
        ))
        resumed.http.download = Mock(return_value=CSV)
        self.assertEqual(len(resumed._export(DAY, DAY)), 1)
        self.assertEqual(resumed.http.api.call_count, 1)
        self.assertEqual(resumed.http.api.call_args.kwargs["method"], "GET")
        self.assertTrue(resumed.http.api.call_args.args[0].endswith(REPORT_ID))

    def test_poll_attempts_are_bounded(self):
        self.collector.poll_attempts = 3
        self.collector.http.api.side_effect = [
            response(job(), status=202), response(job()), response(job()), response(job())
        ]
        with patch("copilot_reporting.collector.time.sleep") as sleep, \
                self.assertRaisesRegex(SourceError, "still processing"):
            self.collector._export(DAY, DAY)
        self.assertEqual(self.collector.http.api.call_count, 4)
        self.assertEqual(sleep.call_count, 2)

    def test_ambiguous_submission_is_reconciled_not_reposted(self):
        self.collector.http.api.side_effect = [SourceError("Source request failed.")]
        with self.assertRaises(SourceError):
            self.collector._export(DAY, DAY)
        self.collector.http.api.reset_mock()
        self.collector.http.api.side_effect = [
            response({"usage_report_exports": [job(
                "completed", created_at="2099-10-01T00:00:00Z",
                download_urls=["https://" + HOST + "/tokens"],
            )]})
        ]
        self.collector.http.download.side_effect = [CSV]
        self.assertEqual(len(self.collector._export(DAY, DAY)), 1)
        self.assertEqual(self.collector.http.api.call_count, 1)
        self.assertEqual(self.collector.http.api.call_args.kwargs["method"], "GET")

    def test_unresolved_submission_and_failed_export_do_not_repeat_post(self):
        self.collector.http.api.side_effect = [
            SourceError("Source request failed."),
            response({"usage_report_exports": []}),
        ]
        with self.assertRaises(SourceError):
            self.collector._export(DAY, DAY)
        with self.assertRaisesRegex(SourceError, "unresolved"):
            self.collector._export(DAY, DAY)
        self.assertEqual(self.collector.http.api.call_count, 2)

    def test_failed_export_cached_until_expiry(self):
        self.collector.http.api.side_effect = [response(job("failed"), status=202)]
        with self.assertRaisesRegex(SourceError, "failed"):
            self.collector._export(DAY, DAY)
        with self.assertRaisesRegex(SourceError, "failed"):
            self.collector._export(DAY, DAY)
        self.assertEqual(self.collector.http.api.call_count, 1)

    def test_expired_state_allows_one_fresh_export(self):
        self.collector.http.api.side_effect = [
            response(job("completed", download_urls=["https://" + HOST + "/tokens"])),
            response(job("completed", download_urls=["https://" + HOST + "/tokens"])),
        ]
        self.collector.http.download.side_effect = [CSV, CSV]
        self.collector._export(DAY, DAY)
        state_path = list(self.directory.glob("*.json"))[0]
        state = json.loads(state_path.read_text())
        state["expires_at"] = 0
        state_path.write_text(json.dumps(state))
        self.collector._export(DAY, DAY)
        self.assertEqual(self.collector.http.api.call_count, 2)

    def test_completed_cache_refreshes_corrected_period_after_exact_24_hour_ttl(self):
        self.collector.http.api.side_effect = [
            response(job("completed", download_urls=["part"])),
            response(job("completed", download_urls=["part"])),
        ]
        self.collector.http.download.side_effect = [CSV, CSV.replace(b",10,2,", b",99,2,")]
        with patch("copilot_reporting.collector.time.time", return_value=1000):
            self.assertEqual(self.collector._export(DAY, DAY)[0]["input"], "10")
        with patch("copilot_reporting.collector.time.time", return_value=87399):
            self.assertEqual(self.collector._export(DAY, DAY)[0]["input"], "10")
        self.assertEqual(self.collector.http.api.call_count, 1)
        with patch("copilot_reporting.collector.time.time", return_value=87400):
            self.assertEqual(self.collector._export(DAY, DAY)[0]["input"], "99")
        self.assertEqual(self.collector.http.api.call_count, 2)
        self.assertEqual(self.collector.http.download.call_count, 2)

    def test_old_inflight_id_resumes_without_post_even_after_cache_ttl(self):
        self.collector.http.api.side_effect = [
            response(job()), response(job()),
            response(job("completed", download_urls=["part"])),
        ]
        self.collector.http.download.side_effect = [CSV]
        with patch("copilot_reporting.collector.time.time", return_value=1000), \
                self.assertRaisesRegex(SourceError, "still processing"):
            self.collector._export(DAY, DAY)
        persisted = next(self.directory.glob("export-*.json"))
        state = json.loads(persisted.read_text())
        self.assertIsNone(state["expires_at"])
        # Older checkpoints may have recorded a now-expired pending TTL.
        state["expires_at"] = 2000
        persisted.write_text(json.dumps(state))
        with patch("copilot_reporting.collector.time.time", return_value=1000000):
            self.assertEqual(len(self.collector._export(DAY, DAY)), 1)
        methods = [call.kwargs["method"] for call in self.collector.http.api.call_args_list]
        self.assertEqual(methods, ["POST", "GET", "GET"])

    def test_ambiguous_submission_never_automatically_expires_into_second_post(self):
        self.collector.http.api.side_effect = [
            SourceError("Source request failed."),
            response({"usage_report_exports": []}),
        ]
        with patch("copilot_reporting.collector.time.time", return_value=1000), \
                self.assertRaises(SourceError):
            self.collector._export(DAY, DAY)
        with patch("copilot_reporting.collector.time.time", return_value=1000000), \
                self.assertRaisesRegex(SourceError, "operator reconciliation"):
            self.collector._export(DAY, DAY)
        methods = [call.kwargs["method"] for call in self.collector.http.api.call_args_list]
        self.assertEqual(methods, ["POST", "GET"])
        state = json.loads(next(self.directory.glob("export-*.json")).read_text())
        self.assertEqual(state["status"], "submitting")
        self.assertIsNone(state["expires_at"])

    def test_export_every_part_must_succeed_and_duplicates_fail(self):
        self.collector.http.api.side_effect = [
            response(job("completed", download_urls=["a", "b"]))
        ]
        self.collector.http.download.side_effect = [CSV, CSV]
        with self.assertRaisesRegex(TokenImportError, "Duplicate"):
            self.collector._export(DAY, DAY)
        state = json.loads(list(self.directory.glob("*.json"))[0].read_text())
        self.assertEqual(state["status"], "processing")
        self.assertNotIn("rows", state)

    def test_unsupported_token_schema_is_visible_unavailable_without_data(self):
        self.empty_other_sources()
        self.collector.http.api.side_effect = [
            response(job("completed", download_urls=["part"]))
        ]
        self.collector.http.download.side_effect = [
            b"date,model,quantity\n2026-10-01,m,100\n"
        ]
        source = self.collector.collect(DAY, DAY)["sources"]["tokens"]
        self.assertEqual(source["status"], "unavailable")
        self.assertNotIn("data", source)
        self.assertIn("CSV import", source["reason"])

    def test_unknown_export_schemas_and_failed_jobs_are_errors_without_data(self):
        self.empty_other_sources()
        cases = [
            (job("completed", download_urls=["part"]),
             b"date,model,input,new_token_column\n2026-10-01,m,1,2\n"),
            (job("completed", download_urls=["part"]),
             b"date,model,input\n2026-10-01,m,not-an-integer\n"),
            (job("completed", download_urls=[]), None),
            (job("completed", download_urls=["part"], report_type="detailed"), None),
            (job("unknown-status"), None),
            (job("failed"), None),
        ]
        for index, (payload, csv_data) in enumerate(cases):
            with self.subTest(case=index):
                state_dir = self.directory / str(index)
                state_dir.mkdir(mode=0o700)
                self.collector.state_dir = state_dir
                self.collector.http.api.side_effect = [response(payload)]
                self.collector.http.download.side_effect = [csv_data]
                source = self.collector.collect(DAY, DAY)["sources"]["tokens"]
                self.assertEqual(source["status"], "error")
                self.assertNotIn("data", source)
                persisted = json.loads(next(state_dir.glob("export-*.json")).read_text())
                self.assertNotEqual(persisted["status"], "completed")
                self.assertNotIn("rows", persisted)

    def test_export_range_split_to_maximum_31_days(self):
        self.collector._export = Mock(return_value=[])
        self.collector._tokens(date(2026, 9, 1), date(2026, 10, 8))
        self.assertEqual(
            [call.args for call in self.collector._export.call_args_list],
            [("2026-09-01", "2026-10-01"), ("2026-10-02", "2026-10-08")],
        )

    def test_injected_enterprise_and_config_credentials_rejected(self):
        for config in (
            {"enterprise": "../acme"}, {"enterprise": "acme?secret=x"},
            {"enterprise": "acme", "token": "do-not-accept"},
            {"enterprise": "acme", "token_env": "TOKEN\nheader"},
            {"enterprise": "acme", "max_pages": -1},
        ):
            with self.subTest(config=config), self.assertRaises(ValueError):
                Collector(config, self.directory)

    def test_repository_state_is_refused(self):
        with self.assertRaisesRegex(ValueError, "outside Git"):
            _prepare_state_directory(Path.cwd())

    def test_error_reason_redacts_unexpected_exception_details(self):
        self.empty_other_sources()
        self.collector._tokens = Mock(side_effect=ValueError(
            "https://private.example.com/signed?secret=employee-record"
        ))
        snapshot = self.collector.collect(DAY, DAY)
        self.assertEqual(snapshot["sources"]["tokens"]["reason"],
                         "Source collection failed safely.")


if __name__ == "__main__":
    unittest.main()
