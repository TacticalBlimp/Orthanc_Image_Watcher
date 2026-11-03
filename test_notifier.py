"""Tests run the real notifier against a fake Orthanc and the mock RIS over real HTTP.
Run them with `python -m unittest -v`; no actual Orthanc is needed."""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests
from mock_ris_server import MockRIS
from orthanc_ris_notifier import load_cursor, poll_once, start_cursor

# Requests to 127.0.0.1 must never go through a proxy that is configured for the whole
# machine, as it is in some corporate and sandboxed environments.
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1"


class FakeOrthanc:
    """Just enough of Orthanc's REST API for the notifier: /changes, /studies and /series.
    Scripted with add_study() and finish(); the changes are numbered from 1."""

    def __init__(self):
        self.changes, self.studies, self.series = [], {}, {}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                url = urlparse(self.path)
                status, body = fake.route(url.path, parse_qs(url.query, keep_blank_values=True))
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def log(self, change_type, resource_id):
        self.changes.append({"Seq": len(self.changes) + 1, "ChangeType": change_type,
                             "ID": resource_id})

    def add_study(self, study_id, series=(3,), stable=True):
        ids = [f"{study_id}.{i}" for i in range(len(series))]
        self.series.update(zip(ids, series))
        self.studies[study_id] = {"stable": stable, "series": ids}
        self.log("NewStudy", study_id)

    def finish(self, study_id):
        self.log("StableStudy", study_id)

    def reset(self):
        self.changes.clear()
        self.studies.clear()
        self.series.clear()

    def route(self, path, query):
        kind, _, key = path.strip("/").partition("/")
        if kind == "changes" and "last" in query:
            return 200, {"Changes": self.changes[-1:], "Done": True, "Last": len(self.changes)}
        if kind == "changes":
            since, limit = int(query["since"][0]), int(query["limit"][0])
            newer = [c for c in self.changes if c["Seq"] > since]
            page = newer[:limit]
            return 200, {"Changes": page, "Done": len(newer) <= limit,
                         "Last": page[-1]["Seq"] if page else since}
        if kind == "studies" and key in self.studies:
            study = self.studies[key]
            tags = {"StudyInstanceUID": f"1.2.{key}", "AccessionNumber": f"ACC-{key}"}
            return 200, {"IsStable": study["stable"], "MainDicomTags": tags,
                         "Series": study["series"]}
        if kind == "series" and key in self.series:
            return 200, {"Instances": [f"{key}.{n}" for n in range(self.series[key])]}
        return 404, {"Message": "Unknown resource"}


class NotifierTest(unittest.TestCase):
    """Each test scripts the fake Orthanc, runs the notifier's polling
    and checks what the mock RIS received."""

    def setUp(self):
        self.fake = FakeOrthanc()
        self.addCleanup(self.fake.stop)
        self.ris = MockRIS().start()
        self.addCleanup(self.ris.stop)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.state = os.path.join(folder.name, "state.txt")

    def start(self, since=None):
        return start_cursor(self.fake.url, self.state, since)

    def poll(self):
        poll_once(self.fake.url, self.ris.url, self.state)

    def received(self):
        return [n["orthanc_study_id"] for n in self.ris.received]

    def test_first_run_ignores_history_then_announces_new_studies(self):
        for name in ("old1", "old2"):
            self.fake.add_study(name)
            self.fake.finish(name)
        self.start()
        self.poll()
        self.assertEqual(self.ris.received, [])
        self.fake.add_study("new", series=(4, 2))
        self.fake.finish("new")
        self.poll()
        self.assertEqual(self.ris.received, [{
            "event_id": f"new:{self.fake.changes[-1]['Seq']}",
            "orthanc_study_id": "new",
            "study_instance_uid": "1.2.new",
            "accession_number": "ACC-new",
            "series_count": 2,
            "instance_count": 6,
        }])

    def test_nothing_is_sent_before_orthanc_logs_a_stable_study(self):
        self.start()
        self.fake.add_study("a")
        self.poll()
        self.assertEqual(self.ris.received, [])
        self.fake.finish("a")
        self.poll()
        self.assertEqual(self.received(), ["a"])

    def test_study_that_is_still_receiving_waits_for_the_next_stable_entry(self):
        self.start()
        self.fake.add_study("a", stable=False)
        self.fake.finish("a")
        self.poll()
        self.assertEqual(self.ris.received, [])
        self.fake.studies["a"]["stable"] = True
        self.fake.finish("a")
        self.poll()
        self.assertEqual(self.received(), ["a"])

    def test_deleted_study_is_skipped(self):
        self.start()
        self.fake.finish("ghost")
        self.poll()
        self.assertEqual(self.ris.received, [])
        self.assertEqual(load_cursor(self.state), self.fake.changes[-1]["Seq"])

    def test_ris_outage_loses_nothing(self):
        self.start()
        self.fake.add_study("a")
        self.fake.finish("a")
        self.ris.fail = True
        with self.assertRaises(requests.HTTPError):
            self.poll()
        self.assertLess(load_cursor(self.state), self.fake.changes[-1]["Seq"])
        self.ris.fail = False
        self.poll()
        self.assertEqual(self.received(), ["a"])

    def test_restart_continues_where_it_stopped(self):
        self.start()
        self.fake.add_study("a")
        self.fake.finish("a")
        self.poll()
        self.fake.add_study("b")
        self.fake.finish("b")
        self.start()
        self.poll()
        self.assertEqual(self.received(), ["a", "b"])

    def test_since_replays_history(self):
        for name in ("a", "b", "c"):
            self.fake.add_study(name)
            self.fake.finish(name)
        self.start(since=0)
        self.poll()
        self.assertEqual(self.received(), ["a", "b", "c"])

    def test_reset_orthanc_is_followed(self):
        self.start()
        self.fake.add_study("x")
        self.fake.finish("x")
        self.poll()
        self.fake.reset()
        self.assertEqual(self.start(), 0)
        self.fake.add_study("y")
        self.fake.finish("y")
        self.poll()
        self.assertEqual(self.received(), ["x", "y"])

    def test_many_changes_are_read_in_pages(self):
        self.start()
        for i in range(120):
            self.fake.add_study(f"s{i}")
            self.fake.finish(f"s{i}")
        self.poll()
        self.assertEqual(len(self.ris.received), 120)


if __name__ == "__main__":
    unittest.main()
