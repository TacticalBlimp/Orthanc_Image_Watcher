#!/usr/bin/env python3
"""Tell a RIS when Orthanc has fully received a study.

Polls Orthanc's /changes log. Orthanc writes a StableStudy entry once a study has received
no new image for `StableAge` seconds (60 by default); we treat that as "fully received"
and POST a short JSON notification to the RIS.
"""
import argparse
import logging
import os
import time

import requests

log = logging.getLogger("notifier")


def get(url, **params):
    """GET a JSON document from Orthanc and return it. Gives up after 30 s so a dead
    server cannot hang us, and raises requests.HTTPError for any error status."""
    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def newest_change(orthanc):
    """Return the number of the newest entry in Orthanc's change log (0 if it is empty).
    Used to start at "now" instead of replaying the whole history."""
    data = get(f"{orthanc}/changes?last")
    return data.get("Last", max((c["Seq"] for c in data["Changes"]), default=0))


def load_cursor(path):
    """Return the saved cursor: the number of the last change we have handled.
    The state file holds just that number, so it is easy to read or edit by hand."""
    with open(path) as f:
        return int(f.read())


def save_cursor(path, cursor):
    """Save the cursor. It is written to a temporary file and then renamed, so a crash
    can never leave a half-written state file behind."""
    with open(path + ".tmp", "w") as f:
        f.write(str(cursor))
    os.replace(path + ".tmp", path)


def start_cursor(orthanc, state_file, since=None):
    """Choose the change number to continue after, save it, and return it.

    Order: `since`, then the saved cursor, then the newest change (a first run must not
    announce the existing studies). A saved cursor beyond the newest change means Orthanc's
    database was reset, so the newest change is used instead.
    """
    newest = newest_change(orthanc)
    if since is not None:
        cursor = since
    elif os.path.exists(state_file):
        cursor = min(load_cursor(state_file), newest)
    else:
        cursor = newest
    save_cursor(state_file, cursor)
    return cursor


def describe(orthanc, change):
    """Build the RIS notification for a StableStudy change, or return None to skip it.

    Skips studies deleted since the change was logged, and studies that received more
    images afterwards (Orthanc then logs a new StableStudy once they settle). The event_id
    is the same on every retry, so the RIS can recognise duplicates.
    """
    study_id = change["ID"]
    try:
        study = get(f"{orthanc}/studies/{study_id}")
        if not study.get("IsStable", True):
            log.info("Study %s is still receiving images; waiting for the next StableStudy",
                     study_id)
            return None
        series = [get(f"{orthanc}/series/{s}") for s in study["Series"]]
    except requests.HTTPError as error:
        if error.response.status_code != 404:
            raise
        log.info("Study %s no longer exists; skipping", study_id)
        return None
    tags = study["MainDicomTags"]
    return {
        "event_id": f"{study_id}:{change['Seq']}",
        "orthanc_study_id": study_id,
        "study_instance_uid": tags.get("StudyInstanceUID"),
        "accession_number": tags.get("AccessionNumber"),
        "series_count": len(series),
        "instance_count": sum(len(s["Instances"]) for s in series),
    }


def poll_once(orthanc, ris_url, state_file):
    """Handle every change after the saved cursor, saving the cursor after each one.

    If Orthanc or the RIS fails, the exception propagates with the cursor still in front of
    the failed change, so the next poll retries it. Nothing is skipped; a duplicate is
    possible if the RIS accepted a notification but we never saw its reply.
    """
    cursor = load_cursor(state_file)
    while True:
        page = get(f"{orthanc}/changes", since=cursor, limit=100)
        for change in page["Changes"]:
            if change["ChangeType"] == "StableStudy":
                payload = describe(orthanc, change)
                if payload:
                    requests.post(ris_url, json=payload, timeout=30).raise_for_status()
                    log.info("RIS notified: study %s, %d instances",
                             payload["orthanc_study_id"], payload["instance_count"])
            cursor = change["Seq"]
            save_cursor(state_file, cursor)
        if page["Done"] or not page["Changes"]:
            return


def main():
    """Parse the options, choose where to start and poll until Ctrl+C.
    A failed poll (Orthanc or RIS unreachable) is logged and retried after the interval."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orthanc-url", default="http://10.0.40.80/orthanc-container",
                        help="root of the Orthanc REST API")
    parser.add_argument("--ris-url", default="http://127.0.0.1:8081/api/ris/study-ready",
                        help="RIS endpoint that receives the POST")
    parser.add_argument("--state-file", default="notifier_state.txt",
                        help="where the position in Orthanc's change log is kept")
    parser.add_argument("--interval", type=float, default=5, help="seconds between polls")
    parser.add_argument("--since", type=int, metavar="N",
                        help="handle the changes after number N (replays history)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        cursor = start_cursor(args.orthanc_url, args.state_file, args.since)
    except requests.RequestException as error:
        raise SystemExit(f"Cannot reach Orthanc at {args.orthanc_url}: {error}")
    log.info("Watching %s after change #%d", args.orthanc_url, cursor)
    log.info("Studies already in Orthanc are not announced; use --since N to replay them")
    try:
        while True:
            try:
                poll_once(args.orthanc_url, args.ris_url, args.state_file)
            except requests.RequestException as error:
                log.warning("Poll failed, will retry: %s", error)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log.info("Stopped")


if __name__ == "__main__":
    main()
