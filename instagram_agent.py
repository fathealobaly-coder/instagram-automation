import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
import sys

MODULE_PATH = Path(__file__).resolve().parents[1] / "trend_agent.py"
spec = importlib.util.spec_from_file_location("trend_agent_under_test", MODULE_PATH)
agent = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = agent
spec.loader.exec_module(agent)


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None, url=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.url = url or "https://archive.org/advancedsearch.php"
    def raise_for_status(self):
        if self.status_code >= 400:
            raise agent.requests.HTTPError(f"HTTP {self.status_code}")
    def json(self):
        return self.payload
    def __enter__(self): return self
    def __exit__(self, *args): return False


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class TrendAgentTests(unittest.TestCase):
    def test_license_recognition_requires_canonical_https_cc_url(self):
        self.assertEqual(agent.recognized_license_url("https://creativecommons.org/licenses/by/4.0/"), "/licenses/by/4.0")
        for bad in ("", "https://example.com/licenses/by/4.0", "http://creativecommons.org/licenses/by/4.0/", "https://creativecommons.org.fake/licenses/by/4.0/"):
            self.assertIsNone(agent.recognized_license_url(bad))

    def test_commercial_gate_accepts_cc0_by_and_by_sa(self):
        for url in (
            "https://creativecommons.org/publicdomain/zero/1.0/",
            "https://creativecommons.org/licenses/by/4.0/",
            "https://creativecommons.org/licenses/by-sa/4.0/",
        ):
            with self.subTest(url=url): self.assertTrue(agent.license_allows_commercial_use(url))

    def test_commercial_gate_rejects_missing_fake_nc_nd_and_public_domain_claim(self):
        for url in (
            "", "https://example.com/license", "https://creativecommons.org/licenses/by-nc/4.0/",
            "https://creativecommons.org/licenses/by-nc-sa/4.0/", "https://creativecommons.org/licenses/by-nd/4.0/",
            "https://creativecommons.org/publicdomain/mark/1.0/",
        ):
            with self.subTest(url=url): self.assertFalse(agent.license_allows_commercial_use(url))
        self.assertFalse(agent.license_allows_commercial_use(""))

    def test_rights_never_claimed_verified_from_license_metadata_alone(self):
        for license_url in ("", "https://example.com/license", "https://creativecommons.org/publicdomain/zero/1.0/", "https://creativecommons.org/licenses/by/4.0/"):
            record = agent.make_record({"video_url":"https://archive.org/download/id/clip.mp4", "title":"Test", "license_url":license_url}, {"ok":True})
            with self.subTest(license_url=license_url):
                self.assertFalse(record["rights_verified"])
                self.assertFalse(record["publication_approved"])

    def test_open_license_recognition_does_not_override_commercial_gate(self):
        nc = "https://creativecommons.org/licenses/by-nc/4.0/"
        self.assertFalse(agent.license_is_open(nc))
        self.assertFalse(agent.license_allows_commercial_use(nc))

    def test_url_validation_rejects_fake_hosts_http_and_page_urls(self):
        self.assertTrue(agent.is_allowed_media_url("https://archive.org/download/id/clip.mp4"))
        for url in ("http://archive.org/download/id/clip.mp4", "https://archive.org/details/id", "https://example.com/clip.mp4"):
            with self.subTest(url=url): self.assertFalse(agent.is_allowed_media_url(url))

    def test_failed_media_probe_rejected(self):
        session = Mock()
        session.get.return_value = FakeResponse({}, 200, {"Content-Type":"text/html"}, "https://archive.org/download/id/clip.mp4")
        result = agent.check_video_url("https://archive.org/download/id/clip.mp4", session=session)
        self.assertFalse(result["ok"])

    def test_archive_search_rejects_noncommercial_license_before_metadata_fetch(self):
        response = FakeResponse({"response":{"docs":[{"identifier":"item1", "title":"test", "licenseurl":"https://creativecommons.org/licenses/by-nc/4.0/"}]}})
        session = FakeSession([response])
        self.assertEqual(agent.search_internet_archive("test", session=session), [])
        self.assertEqual(len(session.calls), 1)

    def test_archive_search_returns_only_direct_mp4_with_commercial_license_metadata(self):
        search_payload = {"response":{"docs":[{"identifier":"item1", "title":"Licensed clip", "description":"test", "licenseurl":"https://creativecommons.org/licenses/by/4.0/"}]}}
        metadata_payload = {"metadata":{"title":"Licensed clip", "licenseurl":"https://creativecommons.org/licenses/by/4.0/", "license":"CC BY 4.0"}, "files":[{"name":"clip.mp4", "size":"1234"}]}
        session = FakeSession([FakeResponse(search_payload), FakeResponse(metadata_payload, url="https://archive.org/metadata/item1")])
        results = agent.search_internet_archive("test", session=session)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["video_url"], "https://archive.org/download/item1/clip.mp4")
        self.assertTrue(agent.license_allows_commercial_use(results[0]["license_url"]))

    def test_archive_search_logs_zero_or_filtered_result_summary(self):
        session = FakeSession([FakeResponse({"response":{"docs":[]}})])
        with self.assertLogs(agent.LOG, level="INFO") as captured:
            results = agent.search_internet_archive("test", session=session)
        self.assertEqual(results, [])
        self.assertTrue(any("[IA_SUMMARY]" in line and "search_docs=0" in line for line in captured.output))

    def test_commons_search_rejects_noncommercial_license(self):
        payload = {"query":{"pages":{"1":{"title":"File:clip.mp4","imageinfo":[{
            "url":"https://upload.wikimedia.org/wikipedia/commons/a/a1/clip.mp4", "mime":"video/mp4", "size":100,
            "extmetadata":{"LicenseShortName":{"value":"CC BY-NC 4.0"},"LicenseUrl":{"value":"https://creativecommons.org/licenses/by-nc/4.0/"}}
        }]}}}}
        session = FakeSession([FakeResponse(payload, url="https://commons.wikimedia.org/w/api.php")])
        with self.assertLogs(agent.LOG, level="INFO") as captured:
            results = agent.search_wikimedia_commons("test", session=session)
        self.assertEqual(results, [])
        self.assertTrue(any("[COMMONS_SUMMARY]" in line and "invalid_license=1" in line for line in captured.output))

    def test_dry_run_is_permanent_and_ignores_environment_setting(self):
        self.assertIs(agent.DRY_RUN, True)

    def test_run_agent_rejects_missing_or_noncommercial_license_before_preview(self):
        old = (agent.SUPABASE_URL, agent.SUPABASE_SERVICE_ROLE_KEY, agent.database_snapshot, agent.discover_candidates, agent.check_video_url, agent.STATE_FILE)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                agent.SUPABASE_URL = "https://supabase.example"
                agent.SUPABASE_SERVICE_ROLE_KEY = "test-only-not-a-real-secret"
                agent.STATE_FILE = Path(tmp) / "state.json"
                agent.database_snapshot = lambda: (set(), 0)
                agent.discover_candidates = lambda: iter([
                    {"video_url":"https://archive.org/download/id/no-license.mp4", "title":"No license", "license_url":""},
                    {"video_url":"https://archive.org/download/id/nc.mp4", "title":"Noncommercial", "license_url":"https://creativecommons.org/licenses/by-nc/4.0/"},
                ])
                agent.check_video_url = Mock(side_effect=AssertionError("probe must not run for unlicensed candidates"))
                with self.assertLogs(agent.LOG, level="INFO") as captured:
                    result = agent.run_agent()
                self.assertEqual(result["previewed"], 0)
                self.assertTrue(any("rejected_license=2" in line for line in captured.output))
                agent.check_video_url.assert_not_called()
                self.assertFalse(agent.STATE_FILE.exists())
        finally:
            (agent.SUPABASE_URL, agent.SUPABASE_SERVICE_ROLE_KEY, agent.database_snapshot, agent.discover_candidates, agent.check_video_url, agent.STATE_FILE) = old

    def test_insert_record_hard_blocked_when_dry_run(self):
        old = agent.DRY_RUN
        try:
            agent.DRY_RUN = True
            with self.assertRaisesRegex(RuntimeError, "permanently disabled"):
                agent.insert_record({"video_id":"test"}, session=Mock())
        finally: agent.DRY_RUN = old

    def test_run_agent_preview_never_inserts_or_saves_state(self):
        old = (agent.SUPABASE_URL, agent.SUPABASE_SERVICE_ROLE_KEY, agent.DRY_RUN, agent.database_snapshot, agent.discover_candidates, agent.check_video_url, agent.insert_record, agent.STATE_FILE)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                agent.SUPABASE_URL = "https://supabase.example"
                agent.SUPABASE_SERVICE_ROLE_KEY = "test-only-not-a-real-secret"
                agent.DRY_RUN = True
                agent.STATE_FILE = Path(tmp) / "state.json"
                agent.database_snapshot = lambda: (set(), 0)
                agent.discover_candidates = lambda: iter([{"video_url":"https://archive.org/download/id/clip.mp4", "title":"Preview", "license_url":"https://creativecommons.org/licenses/by/4.0/"}])
                agent.check_video_url = lambda url: {"ok":True, "status_code":206, "content_type":"video/mp4"}
                agent.insert_record = Mock(side_effect=AssertionError("write attempted"))
                result = agent.run_agent()
                self.assertEqual(result, {"status":"DRY_RUN_PREVIEW", "added":0, "previewed":1, "daily_count":0, "target":3})
                agent.insert_record.assert_not_called()
                self.assertFalse(agent.STATE_FILE.exists())
        finally:
            (agent.SUPABASE_URL, agent.SUPABASE_SERVICE_ROLE_KEY, agent.DRY_RUN, agent.database_snapshot, agent.discover_candidates, agent.check_video_url, agent.insert_record, agent.STATE_FILE) = old

    def test_empty_discovery_returns_zero_preview_without_writes(self):
        old = (agent.SUPABASE_URL, agent.SUPABASE_SERVICE_ROLE_KEY, agent.DRY_RUN, agent.database_snapshot, agent.discover_candidates, agent.STATE_FILE)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                agent.SUPABASE_URL = "https://supabase.example"
                agent.SUPABASE_SERVICE_ROLE_KEY = "test-only-not-a-real-secret"
                agent.DRY_RUN = True
                agent.STATE_FILE = Path(tmp) / "state.json"
                agent.database_snapshot = lambda: (set(), 0)
                agent.discover_candidates = lambda: iter(())
                with self.assertLogs(agent.LOG, level="INFO") as captured:
                    result = agent.run_agent()
                self.assertEqual(result["previewed"], 0)
                self.assertTrue(any("[DISCOVERY_SUMMARY] yielded=0 previewed=0" in line for line in captured.output))
                self.assertFalse(agent.STATE_FILE.exists())
        finally:
            (agent.SUPABASE_URL, agent.SUPABASE_SERVICE_ROLE_KEY, agent.DRY_RUN, agent.database_snapshot, agent.discover_candidates, agent.STATE_FILE) = old

    def test_source_auth_and_rate_limit_stop(self):
        for status in (401, 403, 429):
            with self.subTest(status=status):
                response = FakeResponse({}, status)
                with self.assertRaises(agent.SourceAPIStop): agent.check_source_response(response, "test source")

    def test_daily_target_short_circuits_discovery(self):
        old = (agent.SUPABASE_URL, agent.SUPABASE_SERVICE_ROLE_KEY, agent.database_snapshot, agent.discover_candidates, agent.STATE_FILE)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                agent.SUPABASE_URL = "https://supabase.example"
                agent.SUPABASE_SERVICE_ROLE_KEY = "test-only-not-a-real-secret"
                agent.STATE_FILE = Path(tmp) / "state.json"
                agent.database_snapshot = lambda: (set(), agent.DAILY_TARGET)
                agent.discover_candidates = lambda: (_ for _ in ()).throw(AssertionError("should not search"))
                self.assertEqual(agent.run_agent()["status"], "TARGET_ALREADY_REACHED")
        finally:
            (agent.SUPABASE_URL, agent.SUPABASE_SERVICE_ROLE_KEY, agent.database_snapshot, agent.discover_candidates, agent.STATE_FILE) = old


if __name__ == "__main__": unittest.main(verbosity=2)
