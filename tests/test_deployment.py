import contextlib
from email.message import Message
from http.client import IncompleteRead
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from copilot_reporting import deployment


ENVIRONMENT = {
    "REPORTING_ENABLED": "true",
    "REPORTING_APPROVED_ORGANIZATION": "approved-org",
    "REPORTING_ENCRYPTION_APPROVED": "true",
    "GITHUB_REPOSITORY": "approved-org/reporting",
    "GITHUB_API_URL": "https://api.github.com",
    "GITHUB_TOKEN": "test-repository-credential",
}
REPOSITORY = {
    "private": True, "visibility": "private", "full_name": "approved-org/reporting",
    "owner": {"login": "approved-org", "type": "Organization"},
}
PAGES = {"public": False, "build_type": "workflow"}
CONFIG = {
    "api_origin": "https://api.github.com",
    "publication": {"approved": True, "audience": "shared-aggregates", "minimum_cohort": 5},
}


class Response(io.BytesIO):
    def __init__(self, data, *, status=200, headers=None):
        super().__init__(json.dumps(data).encode() if not isinstance(data, bytes) else data)
        self.status = status
        self.headers = Message()
        for key, value in (headers or {"Content-Type": "application/json"}).items():
            self.headers[key] = value
        self.read_limits = []

    def read(self, size=-1):
        self.read_limits.append(size)
        return super().read(size)


class Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, *, timeout):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class PreflightTests(unittest.TestCase):
    def opener(self, repository=None, pages=None):
        return Opener(Response(REPOSITORY if repository is None else repository),
                      Response(PAGES if pages is None else pages))

    def test_only_repository_scoped_get_requests(self):
        opener = self.opener()
        deployment.preflight(ENVIRONMENT, opener)
        self.assertEqual([item.full_url for item, _ in opener.requests], [
            "https://api.github.com/repos/approved-org/reporting",
            "https://api.github.com/repos/approved-org/reporting/pages",
        ])
        for request, timeout in opener.requests:
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.get_header("Authorization"),
                             "Bearer " + ENVIRONMENT["GITHUB_TOKEN"])
            self.assertEqual(timeout, 15)

    def test_ghe_origin_is_supported(self):
        opener = self.opener()
        deployment.preflight({**ENVIRONMENT, "GITHUB_API_URL": "https://api.company.ghe.com/"}, opener)
        self.assertTrue(opener.requests[0][0].full_url.startswith("https://api.company.ghe.com/"))

    def test_opt_in_organization_project_and_credentials_required_before_network(self):
        cases = [
            {"REPORTING_ENABLED": ""}, {"REPORTING_ENABLED": "True"},
            {"REPORTING_APPROVED_ORGANIZATION": ""},
            {"REPORTING_APPROVED_ORGANIZATION": "other-org"},
            {"GITHUB_REPOSITORY": "approved-org/APPROVED-ORG.github.io"},
            {"GITHUB_REPOSITORY": "approved-org/.."},
            {"GITHUB_REPOSITORY": "approved-org/reporting?leak=1"},
            {"GITHUB_REPOSITORY": "approved-org/reporting/extra"},
            {"GITHUB_TOKEN": ""}, {"GITHUB_TOKEN": "invalid\ncredential"},
        ]
        for case in cases:
            with self.subTest(case=case):
                opener = self.opener()
                with self.assertRaises(deployment.DeploymentError):
                    deployment.preflight({**ENVIRONMENT, **case}, opener)
                self.assertFalse(opener.requests)

    def test_untrusted_origins_never_receive_a_credential(self):
        origins = [
            "http://api.github.com", "https://api.github.com.evil.example",
            "https://evil.example", "https://api.github.com@evil.example",
            "https://user@api.github.com", "https://api.github.com:8443",
            "https://api.github.com/path", "https://api.github.com?q=1",
            "https://api.github.com#fragment", "https://api.github.com\\@evil.example",
            "https://api.company.ghe.com.evil.example", "https://api..ghe.com",
            "https://api.-company.ghe.com", "\nhttps://api.github.com",
            "https://127.0.0.1", "", None,
        ]
        for origin in origins:
            with self.subTest(origin=origin):
                opener = self.opener()
                with self.assertRaises(ValueError):
                    deployment.preflight({**ENVIRONMENT, "GITHUB_API_URL": origin}, opener)
                self.assertFalse(opener.requests)

    def test_actual_repository_metadata_must_be_private_organization(self):
        cases = [
            {"private": False}, {"private": 1}, {"private": None},
            {"visibility": "internal"}, {"visibility": "public"}, {"visibility": None},
            {"full_name": "other/reporting"}, {"full_name": None},
            {"owner": {"type": "User", "login": "approved-org"}},
            {"owner": {"type": "Organization", "login": "other"}},
            {"owner": {"type": "Organization"}}, {"owner": None},
        ]
        for case in cases:
            with self.subTest(case=case):
                opener = self.opener(repository={**REPOSITORY, **case})
                with self.assertRaises(deployment.DeploymentError):
                    deployment.preflight(ENVIRONMENT, opener)
                self.assertEqual(len(opener.requests), 1)

    def test_actual_pages_must_already_be_private_and_workflow_based(self):
        for pages in ({}, {"public": False}, {"build_type": "workflow"},
                      {"public": 0, "build_type": "workflow"},
                      {"public": True, "build_type": "workflow"},
                      {"public": False, "build_type": "legacy"}):
            with self.subTest(pages=pages), self.assertRaises(deployment.DeploymentError):
                deployment.preflight(ENVIRONMENT, self.opener(pages=pages))

    def test_bounded_and_strict_json_responses(self):
        invalid = [
            Response(b"not JSON"), Response(b"\xff"), Response([]),
            Response(b'{"private":true,"private":false}'),
            Response(b'{"private":NaN}'),
            Response(b"x" * (deployment.MAX_RESPONSE_BYTES + 1)),
            Response(REPOSITORY, status=302),
            Response(REPOSITORY, headers={"Content-Type": "text/html"}),
            Response(REPOSITORY, headers={"Content-Type": "application/json", "Content-Length": "-1"}),
            Response(REPOSITORY, headers={"Content-Type": "application/json",
                                         "Content-Length": str(deployment.MAX_RESPONSE_BYTES + 1)}),
        ]
        for response in invalid:
            with self.subTest(response=response), self.assertRaises(ValueError):
                deployment.preflight(ENVIRONMENT, Opener(response))
            self.assertTrue(all(size == deployment.MAX_RESPONSE_BYTES + 1
                                for size in response.read_limits))

    def test_redirect_handler_never_follows_signed_or_other_urls(self):
        with self.assertRaises(deployment.DeploymentError):
            deployment.NoRedirect().redirect_request(
                None, None, 302, "redirect", {}, "https://other.example/secret-link")

    def test_cli_logs_only_generic_error_on_api_failure(self):
        failures = [
            HTTPError("https://example.invalid/sensitive", 403, "sensitive", {}, None),
            URLError("sensitive"),
            ValueError("sensitive"),
            IncompleteRead(b"sensitive"),
            RecursionError("sensitive"),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure)), patch.dict(os.environ, ENVIRONMENT, clear=True):
                output = io.StringIO()
                with patch.object(deployment, "preflight", side_effect=failure):
                    with contextlib.redirect_stderr(output):
                        self.assertEqual(deployment.main([]), 1)
                self.assertEqual(output.getvalue(),
                                 "Deployment preflight failed; no data or API details logged.\n")

    def test_cli_runs_local_checks_before_network(self):
        with patch.object(deployment, "preflight") as network:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(deployment.main(["--output", "dist"]), 1)
                self.assertEqual(deployment.main(["--config", "/missing"]), 1)
            network.assert_not_called()

    def test_cli_success_has_no_metadata(self):
        output = io.StringIO()
        with patch.object(deployment, "preflight"), contextlib.redirect_stdout(output):
            self.assertEqual(deployment.main([]), 0)
        self.assertEqual(output.getvalue(), "Private organization project Pages preflight passed.\n")


class FilesystemTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix=".deployment-tests-", dir=Path.cwd())
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir(mode=0o700)
        self.root_patch = patch.object(deployment, "ROOT", self.checkout)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.volume = self.root / "mounted"
        self.volume.mkdir(mode=0o700)
        self.store = self.volume / "store"
        self.store.mkdir(mode=0o700)
        self.config = self.volume / "approved.json"
        self.write_config(CONFIG)
        self.mount_patch = patch.object(deployment.os.path, "ismount",
                                       side_effect=lambda value: Path(value) == self.volume)
        self.mount_patch.start()
        self.addCleanup(self.mount_patch.stop)

    def write_config(self, settings):
        self.config.write_text(json.dumps(settings), encoding="utf-8")
        self.config.chmod(0o600)

    def validate_paths(self, **overrides):
        return deployment.collection_paths(
            overrides.get("config", self.config), overrides.get("store", self.store),
            overrides.get("storage_mount", self.volume), overrides.get("environ", ENVIRONMENT))

    def test_protected_config_store_and_mounted_volume_are_accepted(self):
        self.validate_paths()

    def test_operator_encryption_approval_is_required(self):
        with self.assertRaises(deployment.DeploymentError):
            self.validate_paths(environ={**ENVIRONMENT, "REPORTING_ENCRYPTION_APPROVED": "false"})

    def test_absent_mount_is_rejected(self):
        with patch.object(deployment.os.path, "ismount", return_value=False):
            with self.assertRaises(deployment.DeploymentError):
                self.validate_paths()

    def test_both_config_and_store_must_be_on_approved_mount(self):
        outside = self.root / "outside.json"
        outside.write_text(json.dumps(CONFIG), encoding="utf-8")
        outside.chmod(0o600)
        with self.assertRaises(deployment.DeploymentError):
            self.validate_paths(config=outside)
        with self.assertRaises(deployment.DeploymentError):
            self.validate_paths(store=self.root)
        with self.assertRaises(deployment.DeploymentError):
            self.validate_paths(storage_mount="/")

    def test_config_must_be_separate_from_store(self):
        nested = self.store / "approved.json"
        nested.write_text(json.dumps(CONFIG), encoding="utf-8")
        nested.chmod(0o600)
        with self.assertRaises(deployment.DeploymentError):
            self.validate_paths(config=nested)

    def test_paths_must_be_absolute_outside_checkout_and_owner_only(self):
        for changes in ({"store": "relative"}, {"config": "relative"},
                        {"store": self.checkout}, {"store": self.volume / "store" / ".."}):
            with self.subTest(changes=changes), self.assertRaises(deployment.DeploymentError):
                self.validate_paths(**changes)
        for path in (self.config, self.store):
            original = path.stat().st_mode
            path.chmod(0o755)
            with self.subTest(path=path), self.assertRaises(deployment.DeploymentError):
                self.validate_paths()
            path.chmod(original)
        with patch.object(deployment.os, "getuid", return_value=-1):
            with self.assertRaises(deployment.DeploymentError):
                self.validate_paths()

    def test_writable_ancestor_is_rejected(self):
        self.volume.chmod(0o777)
        with self.assertRaises(deployment.DeploymentError):
            self.validate_paths()

    def test_symlink_and_hardlink_paths_are_rejected(self):
        link = self.volume / "linked"
        link.symlink_to(self.store, target_is_directory=True)
        with self.assertRaises(deployment.DeploymentError):
            self.validate_paths(store=link)
        parent_link = self.root / "parent-link"
        parent_link.symlink_to(self.volume, target_is_directory=True)
        with self.assertRaises(deployment.DeploymentError):
            self.validate_paths(config=parent_link / "approved.json")
        os.link(self.config, self.volume / "hardlink.json")
        with self.assertRaises(deployment.DeploymentError):
            self.validate_paths()

    def test_explicit_shared_aggregate_privacy_approval_is_required(self):
        for publication in ({}, {"approved": True},
                            {**CONFIG["publication"], "approved": 1},
                            {**CONFIG["publication"], "audience": "named-users"},
                            {**CONFIG["publication"], "minimum_cohort": True},
                            {**CONFIG["publication"], "minimum_cohort": 1}):
            self.write_config({**CONFIG, "publication": publication})
            with self.subTest(publication=publication), self.assertRaises(deployment.DeploymentError):
                self.validate_paths()

    def test_collection_and_deployment_origins_must_match(self):
        self.write_config({**CONFIG, "api_origin": "https://api.company.ghe.com"})
        with self.assertRaises(deployment.DeploymentError):
            self.validate_paths()

    def test_configuration_json_is_bounded_and_strict(self):
        for data in ('[]', '{"publication":null}', '{"publication":{},"publication":{}}',
                     " " * (deployment.MAX_RESPONSE_BYTES + 1)):
            self.config.write_text(data, encoding="utf-8")
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                self.validate_paths()

    def site(self):
        output = self.checkout / "dist"
        (output / "data").mkdir(parents=True)
        for name in deployment.SITE_FILES:
            (output / name).write_text("{}", encoding="utf-8")
        return output

    def test_site_has_exact_static_allowlist(self):
        deployment.validate_site(self.site())

    def test_unexpected_raw_file_or_directory_is_rejected(self):
        output = self.site()
        for name in ("reporting.sqlite", ".env", "data/raw.csv", "app.js.map"):
            file = output / name
            file.write_text("private", encoding="utf-8")
            with self.subTest(name=name), self.assertRaises(deployment.DeploymentError):
                deployment.validate_site(output)
            file.unlink()
        (output / "unexpected").mkdir()
        with self.assertRaises(deployment.DeploymentError):
            deployment.validate_site(output)

    def test_site_rejects_missing_files_and_excess_size(self):
        output = self.site()
        with patch.object(deployment, "MAX_SITE_BYTES", 1):
            with self.assertRaises(deployment.DeploymentError):
                deployment.validate_site(output)
        (output / "app.js").unlink()
        with self.assertRaises(deployment.DeploymentError):
            deployment.validate_site(output)

    def test_site_rejects_symlinks_hardlinks_and_special_files(self):
        output = self.site()
        target = output / "data/report.json"
        target.unlink()
        target.symlink_to(self.config)
        with self.assertRaises(deployment.DeploymentError):
            deployment.validate_site(output)
        target.unlink()
        os.link(self.config, target)
        with self.assertRaises(deployment.DeploymentError):
            deployment.validate_site(output)
        target.unlink()
        os.mkfifo(target)
        with self.assertRaises(deployment.DeploymentError):
            deployment.validate_site(output)

    def test_site_root_symlink_is_rejected(self):
        output = self.site()
        linked = self.checkout / "linked"
        linked.symlink_to(output, target_is_directory=True)
        with self.assertRaises(deployment.DeploymentError):
            deployment.validate_site(linked)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflows = Path(__file__).resolve().parent.parent / ".github" / "workflows"

    def test_demo_pages_is_isolated_from_private_reporting(self):
        workflow = (self.workflows / "demo-pages.yml").read_text(encoding="utf-8")
        build, deploy = workflow.split("\n  deploy:\n")
        for job in (build, deploy):
            self.assertIn("github.repository == 'petrp314/githubCopilotCustomReporting'", job)
            self.assertIn("github.event.repository.owner.type == 'User'", job)
            self.assertIn("vars.REPORTING_ENABLED != 'true'", job)
            self.assertIn("github.event.repository.default_branch", job)
            self.assertIn("runs-on: ubuntu-24.04", job)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("push:", workflow)
        self.assertIn("npm test", build)
        self.assertIn("npm run build", build)
        self.assertIn('validate_site("dist")', build)
        self.assertIn('report.get("demo") is not True', build)
        self.assertIn("path: dist", build)
        self.assertNotIn("pages: write", build)
        self.assertNotIn("id-token: write", build)
        self.assertIn("needs: build", deploy)
        self.assertIn("name: github-pages", deploy)
        self.assertIn("pages: write", deploy)
        self.assertIn("id-token: write", deploy)
        for forbidden in ("secrets.", "self-hosted", "pull_request", "--config", "--store"):
            self.assertNotIn(forbidden, workflow)

    def test_ci_never_receives_enterprise_secrets_or_publishes_artifacts(self):
        ci = (self.workflows / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("pull_request:", ci)
        self.assertIn("contents: read", ci)
        self.assertIn("runs-on: ubuntu-24.04", ci)
        self.assertIn("npm test", ci)
        self.assertIn("npm run build", ci)
        for forbidden in ("secrets.", "self-hosted", "upload-", "deploy-", "pages: write"):
            self.assertNotIn(forbidden, ci)

    def test_reporting_separates_collection_and_hosted_deployment(self):
        reporting = (self.workflows / "reporting.yml").read_text(encoding="utf-8")
        collection, deployment_job = reporting.split("\n  deploy:\n")
        self.assertIn("workflow_dispatch:", collection)
        self.assertIn("schedule:", collection)
        self.assertNotIn("pull_request", reporting)
        self.assertIn("runs-on: [self-hosted, Linux, copilot-reporting]", collection)
        self.assertIn("environment: reporting-collection", collection)
        self.assertIn("pages: read", collection)
        self.assertNotIn("pages: write", collection)
        self.assertIn("runs-on: ubuntu-24.04", deployment_job)
        self.assertIn("name: github-pages", deployment_job)
        self.assertIn("pages: write", deployment_job)
        self.assertIn("id-token: write", deployment_job)
        self.assertNotIn("secrets.", deployment_job)
        for job in (collection, deployment_job):
            self.assertIn("vars.REPORTING_ENABLED == 'true'", job)
            self.assertIn("github.event.repository.private == true", job)
            self.assertIn("github.event.repository.owner.type == 'Organization'", job)
            self.assertIn("github.repository_owner == vars.REPORTING_APPROVED_ORGANIZATION", job)
            self.assertIn("github.event.repository.default_branch", job)
            self.assertIn("python -m copilot_reporting.deployment", job)
            self.assertIn("GITHUB_TOKEN: ${{ github.token }}", job)
        self.assertLess(collection.index("python -m copilot_reporting.deployment"),
                        collection.index("python -m copilot_reporting collect"))
        self.assertLess(collection.index("--output dist\n", collection.index("Recheck private destination")),
                        collection.index("uses: actions/upload-pages-artifact"))
        self.assertLess(deployment_job.index("python -m copilot_reporting.deployment"),
                        deployment_job.index("uses: actions/deploy-pages"))
        self.assertIn("retention-days: 1", collection)
        self.assertIn("path: dist", collection)
        self.assertIn("if: always()", collection)


if __name__ == "__main__":
    unittest.main()
