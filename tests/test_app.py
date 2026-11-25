"""Automated backend and security tests for pi-web-app.

Runs hermetically with PI_WEB_TESTING=1 and a mocked PiProcess so tests
never spawn a real `pi` subprocess or modify projects-<port>.json.
"""

import importlib.util
import json
import os
import queue
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

# Must be set before importing app so module import doesn't spawn pi.
os.environ["PI_WEB_TESTING"] = "1"

import app as web_app

XHR_HEADERS = {"X-Requested-With": "XMLHttpRequest"}


class FakePiProcess:
    """In-memory stub for PiProcess used by route tests."""

    def __init__(self, cwd: str, session_file: str | None = None):
        self.cwd = cwd
        self.alive = True
        self.stopped = False
        self.session_file = session_file
        self.subscribers: list[queue.Queue] = []
        self.subscribers_lock = threading.Lock()
        self.rpc_calls: list[dict] = []

    def is_alive(self) -> bool:
        return self.alive

    def rpc_request(self, cmd: dict, timeout: float = 30.0) -> dict:
        self.rpc_calls.append(cmd)
        if cmd.get("type") == "get_state":
            return {"success": True, "data": {"sessionFile": self.session_file}}
        return {"success": True, "data": {}}

    def run_bash(self, command: str, exclude_from_context: bool, cmd_id: str | None = None) -> dict:
        return {"success": True, "id": cmd_id or "test-id"}

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=100)
        with self.subscribers_lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.subscribers_lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def broadcast(self, event: dict) -> None:
        with self.subscribers_lock:
            for q in list(self.subscribers):
                q.put_nowait(event)

    def stop(self) -> None:
        self.stopped = True
        self.alive = False


class BaseAppTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_root = Path(self._tmp.name).resolve()
        self.workspace = self.tmp_root / "workspace"
        self.workspace.mkdir()
        self.project_dir = self.workspace / "proj"
        self.project_dir.mkdir()
        self.outside_dir = self.tmp_root / "outside"
        self.outside_dir.mkdir()
        self.outside_file = self.outside_dir / "secret.txt"
        self.outside_file.write_text("top-secret")

        self.session_dir = self.tmp_root / "sessions"
        self.session_dir.mkdir()
        self.session_file = self.session_dir / "s1.jsonl"
        self.session_file.write_text('{"type":"session"}\n')

        self.registry_path = self.tmp_root / "projects-test.json"
        self.project_entry = {
            "id": "proj0001",
            "name": "proj",
            "path": str(self.project_dir),
        }
        self.registry_path.write_text(json.dumps([self.project_entry]))

        self.orig_workspace = web_app.WORKSPACE_ROOT
        self.orig_registry = web_app.REGISTRY_PATH
        self.orig_current = dict(web_app.current)

        web_app.WORKSPACE_ROOT = self.workspace
        web_app.REGISTRY_PATH = self.registry_path
        self.fake_pi = FakePiProcess(str(self.project_dir), str(self.session_file))
        web_app.current["project"] = dict(self.project_entry)
        web_app.current["pi"] = self.fake_pi

        self.client = web_app.app.test_client()

    def tearDown(self):
        web_app.WORKSPACE_ROOT = self.orig_workspace
        web_app.REGISTRY_PATH = self.orig_registry
        web_app.current.clear()
        web_app.current.update(self.orig_current)
        self._tmp.cleanup()


class TestOriginAndSecurityHeaders(BaseAppTest):
    def test_post_requires_csrf_header(self):
        r = self.client.post("/abort", json={})
        self.assertEqual(r.status_code, 403)
        r_ok = self.client.post("/abort", json={}, headers=XHR_HEADERS)
        self.assertEqual(r_ok.status_code, 200)

    def test_cross_site_blocked_except_navigate_and_valid_raw_subresource(self):
        (self.project_dir / "app.js").write_text("window.SECRET = 1;\n")
        (self.project_dir / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n")

        # Cross-site API request -> 403
        r = self.client.get("/api/projects", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors"})
        self.assertEqual(r.status_code, 403)

        # Cross-site /raw with wrong token -> 403
        r = self.client.get(
            "/raw/wrong-token/app.js",
            headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "script"},
        )
        self.assertEqual(r.status_code, 403)

        # Cross-site /raw with valid token but cors mode -> 403
        r = self.client.get(
            f"/raw/{web_app.RAW_TOKEN}/app.js",
            headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"},
        )
        self.assertEqual(r.status_code, 403)

        # Cross-site /raw with valid token and passive no-cors subresource -> 200
        with self.client.get(
            f"/raw/{web_app.RAW_TOKEN}/pic.png",
            headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "image"},
        ) as r:
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers.get("Content-Security-Policy"), "sandbox allow-scripts allow-forms")

        # Cross-site top-level GET navigate -> allowed past origin check
        r = self.client.get(
            "/api/projects",
            headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"},
        )
        self.assertEqual(r.status_code, 200)

    def test_raw_endpoint_requires_valid_token_even_same_origin(self):
        (self.project_dir / "hello.txt").write_text("hi")
        self.assertEqual(self.client.get("/raw/badtoken/hello.txt").status_code, 403)
        with self.client.get(f"/raw/{web_app.RAW_TOKEN}/hello.txt") as r:
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.data, b"hi")

    def test_trusted_hosts_rejects_unknown_host(self):
        r = self.client.get("/api/projects", headers={"Host": "evil.example:5000"})
        self.assertEqual(r.status_code, 400)
        r_ok = self.client.get("/api/projects", headers={"Host": "localhost:5000"})
        self.assertEqual(r_ok.status_code, 200)

    def test_security_headers_and_raw_token_exposure(self):
        r = self.client.get("/api/projects")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r.headers.get("Referrer-Policy"), "no-referrer")
        self.assertEqual(r.headers.get("Cache-Control"), "no-store")
        self.assertEqual(r.get_json().get("rawToken"), web_app.RAW_TOKEN)

        rs = self.client.get("/state")
        self.assertEqual(rs.status_code, 200)
        self.assertEqual(rs.get_json().get("rawToken"), web_app.RAW_TOKEN)


class TestFilesystemAndSymlinks(BaseAppTest):
    def test_traversal_and_external_symlink_rejected(self):
        (self.project_dir / "ext_link").symlink_to(self.outside_file)
        for url in [
            "/api/file?path=../outside/secret.txt",
            "/api/file?path=ext_link",
            f"/raw/{web_app.RAW_TOKEN}/ext_link",
            "/download/ext_link",
            "/api/diff?path=ext_link",
        ]:
            r = self.client.get(url)
            self.assertEqual(r.status_code, 403, f"expected 403 for {url}, got {r.status_code}")

    def test_delete_unlinks_symlinks_without_touching_targets(self):
        target_dir = self.project_dir / "keep_dir"
        target_dir.mkdir()
        keep_file = target_dir / "important.txt"
        keep_file.write_text("do not delete")

        dir_link = self.project_dir / "dir_link"
        dir_link.symlink_to(target_dir)
        file_link = self.project_dir / "file_link"
        file_link.symlink_to(keep_file)
        dangling_link = self.project_dir / "dangling"
        dangling_link.symlink_to(self.project_dir / "nonexistent")

        for name in ("dir_link", "file_link", "dangling"):
            r = self.client.post("/api/file/delete", json={"path": name}, headers=XHR_HEADERS)
            self.assertEqual(r.status_code, 200, f"failed deleting {name}: {r.data}")
            self.assertFalse((self.project_dir / name).exists())
            self.assertFalse((self.project_dir / name).is_symlink())

        self.assertTrue(target_dir.is_dir())
        self.assertEqual(keep_file.read_text(), "do not delete")

    def test_delete_rejects_root_and_escaped_parent(self):
        ext_dir_link = self.project_dir / "ext_dir"
        ext_dir_link.symlink_to(self.outside_dir)

        for bad in ("", ".", "/"):
            r = self.client.post("/api/file/delete", json={"path": bad}, headers=XHR_HEADERS)
            self.assertEqual(r.status_code, 403)

        r = self.client.post("/api/file/delete", json={"path": "ext_dir/secret.txt"}, headers=XHR_HEADERS)
        self.assertEqual(r.status_code, 403)
        self.assertTrue(self.outside_file.is_file())

    def test_git_status_map_handles_external_and_internal_symlinks(self):
        subprocess.run(["git", "init", "-b", "main"], cwd=self.project_dir, check=True, capture_output=True)
        tracked = self.project_dir / "real.txt"
        tracked.write_text("hello\n")
        subprocess.run(["git", "add", "real.txt"], cwd=self.project_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init"],
            cwd=self.project_dir,
            check=True,
            capture_output=True,
        )
        # Untracked symlink pointing OUTSIDE repo and another pointing INSIDE repo
        (self.project_dir / "outside_link").symlink_to(self.outside_file)
        (self.project_dir / "internal_link").symlink_to(tracked)

        r = self.client.get("/api/list")
        self.assertEqual(r.status_code, 200)
        by_name = {e["name"]: e for e in r.get_json()["entries"]}
        self.assertEqual(by_name["real.txt"]["git"], "clean")
        self.assertEqual(by_name["internal_link"]["git"], "untracked")


class TestInputValidationAndDiffLimits(BaseAppTest):
    def test_non_dict_json_and_bad_types_return_400(self):
        for ep in [
            "/bash",
            "/api/file/delete",
            "/api/file/create",
            "/api/file/save",
            "/api/projects",
            "/set_model",
            "/set_thinking",
        ]:
            r = self.client.post(
                ep,
                data="[1, 2, 3]",
                content_type="application/json",
                headers=XHR_HEADERS,
            )
            self.assertEqual(r.status_code, 400, f"{ep} returned {r.status_code} on list JSON")

        self.assertEqual(
            self.client.post("/api/projects", json={"path": 123}, headers=XHR_HEADERS).status_code,
            400,
        )
        self.assertEqual(
            self.client.post("/api/projects", json={"gitUrl": 123}, headers=XHR_HEADERS).status_code,
            400,
        )
        self.assertEqual(
            self.client.post("/set_model", json={"provider": 1, "modelId": "x"}, headers=XHR_HEADERS).status_code,
            400,
        )
        self.assertEqual(
            self.client.post("/set_thinking", json={"level": 5}, headers=XHR_HEADERS).status_code,
            400,
        )

    def test_null_bytes_return_400(self):
        self.assertEqual(self.client.get("/api/file?path=foo%00bar").status_code, 400)
        self.assertEqual(self.client.get("/api/dirs?path=foo%00bar").status_code, 400)
        self.assertEqual(
            self.client.post("/api/file/delete", json={"path": "foo\x00bar"}, headers=XHR_HEADERS).status_code,
            400,
        )
        self.assertEqual(
            self.client.post("/switch_session", json={"path": "foo\x00.jsonl"}, headers=XHR_HEADERS).status_code,
            400,
        )

    def test_diff_rejects_oversized_and_handles_non_utf8(self):
        big = self.project_dir / "big.txt"
        big.write_bytes(b"a" * (web_app.MAX_PREVIEW_BYTES + 1))
        r = self.client.get("/api/diff?path=big.txt")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertIsNone(body["diff"])
        self.assertIn("too large", body["error"])

        latin = self.project_dir / "latin.txt"
        latin.write_bytes(b"caf\xe9\n")
        r2 = self.client.get("/api/diff?path=latin.txt")
        self.assertEqual(r2.status_code, 200)
        self.assertIsNone(r2.get_json()["error"])


class TestConcurrencyAndResources(BaseAppTest):
    def test_session_name_cache_concurrent_prune(self):
        with web_app._session_name_cache_lock:
            web_app._session_name_cache.clear()
            for i in range(web_app._SESSION_NAME_CACHE_MAX + 50):
                web_app._session_name_cache[f"/fake/{i}.jsonl"] = (1.0, 10, f"n{i}")

        errors: list[Exception] = []

        def worker(idx: int):
            try:
                for j in range(25):
                    web_app._prune_name_cache({f"/fake/{idx}.jsonl"})
                    with web_app._session_name_cache_lock:
                        web_app._session_name_cache[f"/fake/t{idx}_{j}.jsonl"] = (1.0, 10, "x")
            except (KeyError, RuntimeError) as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

    def test_failed_git_clone_never_deletes_existing_dir(self):
        existing = self.workspace / "existing-repo"
        existing.mkdir()
        marker = existing / "precious.txt"
        marker.write_text("keep")

        # Simulate a race where `existing` appears while `git clone` runs and fails.
        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(args[0], 128, "", "fatal: clone failed")

        target = self.workspace / "race-repo"
        with mock.patch.object(web_app.subprocess, "run", side_effect=fake_run):
            # Create target right as clone fails via another side effect
            def race_run(*args, **kwargs):
                target.mkdir(exist_ok=True)
                (target / "keep.txt").write_text("safe")
                return fake_run(*args, **kwargs)

            with mock.patch.object(web_app.subprocess, "run", side_effect=race_run):
                r = self.client.post(
                    "/api/projects",
                    json={"gitUrl": "https://example.invalid/org/race-repo.git"},
                    headers=XHR_HEADERS,
                )
        self.assertEqual(r.status_code, 400)
        self.assertTrue((target / "keep.txt").is_file())
        self.assertTrue(marker.is_file())

    def test_sse_stream_terminates_on_terminal_events(self):
        for ev_type in ("stream_overflow", "project_switched", "pi_exited"):
            # Pre-queue the terminal event once subscribed.
            orig_sub = self.fake_pi.subscribe

            def sub_with_event(et=ev_type, sub=orig_sub):
                q = sub()
                q.put_nowait({"type": et})
                return q

            with mock.patch.object(self.fake_pi, "subscribe", side_effect=sub_with_event):
                r = self.client.get("/events")
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.headers.get("Cache-Control"), "no-cache")
                self.assertEqual(r.headers.get("X-Accel-Buffering"), "no")
                body = r.get_data(as_text=True)
                self.assertIn(f'"type": "{ev_type}"', body)

    def test_open_project_stops_new_pi_on_save_failure(self):
        other = self.workspace / "other"
        other.mkdir()
        other_entry = {"id": "proj0002", "name": "other", "path": str(other)}
        self.registry_path.write_text(json.dumps([self.project_entry, other_entry]))

        spawned: list[FakePiProcess] = []

        def fake_spawn(cwd):
            p = FakePiProcess(cwd, str(self.session_file))
            spawned.append(p)
            return p

        with (
            mock.patch.object(web_app, "PiProcess", side_effect=fake_spawn),
            mock.patch.object(web_app, "save_projects", side_effect=OSError("disk full")),
            self.assertLogs(web_app.app.logger, level="ERROR"),
        ):
            r = self.client.post("/api/projects/proj0002/open", headers=XHR_HEADERS)
        self.assertEqual(r.status_code, 500)

        self.assertEqual(len(spawned), 1)
        self.assertTrue(spawned[0].stopped)
        self.assertEqual(web_app.current["project"]["id"], "proj0001")


class TestGunicornConfig(unittest.TestCase):
    def test_resolve_binds(self):
        spec = importlib.util.spec_from_file_location("gunicorn_conf", Path(web_app.APP_ROOT) / "gunicorn.conf.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        self.assertEqual(mod.resolve_binds("127.0.0.1", 5000), ["127.0.0.1:5000"])
        self.assertEqual(mod.resolve_binds("localhost", 5000), ["localhost:5000"])
        self.assertEqual(mod.resolve_binds("0.0.0.0", 5001), ["0.0.0.0:5001"])
        self.assertEqual(
            mod.resolve_binds("192.168.1.50", 5002),
            ["192.168.1.50:5002", "127.0.0.1:5002"],
        )


if __name__ == "__main__":
    unittest.main()
