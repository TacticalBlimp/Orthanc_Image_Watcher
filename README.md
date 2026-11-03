# Orthanc → RIS notifier

Tells a RIS when Orthanc has fully received a study. It only reads from Orthanc.
Needs Python 3 and `requests` (`pip install -r requirements.txt`); tested on Python 3.13.

## Run

```bash
python mock_ris_server.py        # a fake RIS on http://127.0.0.1:8081
python orthanc_ris_notifier.py   # watch Orthanc and notify the RIS
```

The defaults are the Orthanc at `http://10.0.40.80/orthanc-container` (the REST root) and the
mock RIS above. Change them with `--orthanc-url` and `--ris-url`; `--help` lists the rest.

## How it works

DICOM has no "study complete" marker, so we use Orthanc's own signal: a `StableStudy` entry in
its `/changes` log, written when a study got no new image for `StableAge` seconds (60 by default).
The notifier polls that log, re-reads the study (skipping it if images arrived meanwhile) and
POSTs the notification below. It saves its position in `notifier_state.txt`, so a restart carries on.

## Notification

```json
{"event_id": "<study id>:<change number>", "orthanc_study_id": "...",
 "study_instance_uid": "1.2.840...", "accession_number": "...",
 "series_count": 3, "instance_count": 412}
```

## Demo tips

The first run starts at the newest change, so the existing studies are not announced. Upload a
study and wait about 65 s (`StableAge` plus the 5 s poll), or replay old changes with `--since N`
(`--since 0` replays the whole log, which is slow if the log is long).

## Limitations

- "Fully received" means "quiet for `StableAge` seconds". If a sender pauses longer, the RIS gets
  an early notification and then another with a larger `instance_count`: treat the latest as truth.
- A notification the RIS keeps rejecting is retried forever and holds back the ones behind it.
- No login is sent to Orthanc or the RIS, and Orthanc may lose a `StableStudy` entry if it
  restarts during the `StableAge` window.

## Tests

`python -m unittest -v` runs the notifier against a fake Orthanc and the mock RIS.
No real PACS is needed, and the whole suite takes about two seconds.
