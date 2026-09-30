# Shared helpers for the IDMC CI/CD scripts: commit lookup, mapping task runs and pulls.
#
# Optional environment variables:
#   ALLOW_WARNINGS        "true" to accept jobs that complete with errors and pulls that end in WARNING (default false)
#   JOB_POLL_SECONDS      seconds between job status checks (default 30)
#   JOB_TIMEOUT_SECONDS   max seconds to wait for one mapping task (default 3600)
#   PULL_TIMEOUT_SECONDS  max seconds to wait for a pull (default 1800)

import os
import sys
import time
import json
import requests

ALLOW_WARNINGS = os.environ.get('ALLOW_WARNINGS', 'false').lower() == 'true'
JOB_POLL_SECONDS = int(os.environ.get('JOB_POLL_SECONDS', '30'))
JOB_TIMEOUT_SECONDS = int(os.environ.get('JOB_TIMEOUT_SECONDS', '3600'))
PULL_POLL_SECONDS = 10
PULL_TIMEOUT_SECONDS = int(os.environ.get('PULL_TIMEOUT_SECONDS', '1800'))

# activityLog state codes (REST API v2, logs for completed jobs)
JOB_SUCCESS = 1
JOB_COMPLETED_WITH_ERRORS = 2
JOB_FAILED = 3


def fail(message):
    print("Exception caught: " + message)
    sys.exit(99)


def v3_headers(session_id):
    return {"Content-Type": "application/json; charset=utf-8", "INFA-SESSION-ID": session_id}


def v2_headers(session_id):
    return {"Content-Type": "application/json; charset=utf-8", "icSessionId": session_id}


def object_label(obj):
    path = obj.get('path')
    if isinstance(path, list) and path:
        return '/'.join(path)
    return obj.get('name') or obj.get('id') or '?'


def get_commit_changes(url, session_id, commit_hash):
    """Objects changed in a commit: GET /public/core/v3/commit/<hash>."""
    r = requests.get(url + "/public/core/v3/commit/" + commit_hash, headers = v3_headers(session_id))
    if r.status_code != 200:
        fail(r.text)
    changes = r.json().get('changes') or []
    print("Commit " + commit_hash + " changed " + str(len(changes)) + " object(s):")
    for x in changes:
        print("  " + str(x.get('action')) + " " + str(x.get('type')) + " " + object_label(x))
    return changes


def mapping_tasks(changes):
    """Mapping tasks in the commit that still exist (deleted ones have no id to run)."""
    return [x for x in changes
            if x.get('type') == 'MTT' and x.get('action') != 'DELETED' and x.get('id')]


def run_mapping_task(url, session_id, change):
    """Start a mapping task and wait for it to finish. Returns the activityLog state."""
    headers = v2_headers(session_id)
    name = object_label(change)

    # taskFederatedId works for tasks in any folder; taskId only for the Default folder
    body = {"@type": "job", "taskFederatedId": change['id'], "taskType": "MTT"}
    t = requests.post(url + "/api/v2/job", headers = headers, json = body)
    if t.status_code != 200:
        fail("could not start " + name + ": " + t.text)

    job = t.json()
    run_id = job['runId']
    task_id = job.get('taskId') or change.get('appContextId')
    print("Started mapping task " + name + " (runId " + str(run_id) + ")")

    # activityLog only lists finished jobs: an empty list means the job is still running
    waited = 0
    while True:
        if waited >= JOB_TIMEOUT_SECONDS:
            fail("mapping task " + name + " still running after " + str(JOB_TIMEOUT_SECONDS) + " seconds")
        time.sleep(JOB_POLL_SECONDS)
        waited += JOB_POLL_SECONDS
        a = requests.get(url + "/api/v2/activity/activityLog", headers = headers,
                         params = {"runId": run_id, "taskId": task_id})
        if a.status_code != 200:
            fail(a.text)
        entries = a.json()
        if isinstance(entries, list) and entries and entries[0].get('state') in (JOB_SUCCESS, JOB_COMPLETED_WITH_ERRORS, JOB_FAILED):
            return entries[0]['state']
        print("Mapping task " + name + " still running (" + str(waited) + "s)")


def test_mapping_tasks(url, session_id, changes):
    """Run every mapping task in the commit; exit 99 if any fails."""
    tasks = mapping_tasks(changes)
    if not tasks:
        print("No mapping tasks in this commit to test")
        return
    for x in tasks:
        name = object_label(x)
        state = run_mapping_task(url, session_id, x)
        if state == JOB_SUCCESS:
            print("Mapping task: " + name + " completed successfully.")
        elif state == JOB_COMPLETED_WITH_ERRORS and ALLOW_WARNINGS:
            print("Mapping task: " + name + " completed with errors (allowed by ALLOW_WARNINGS).")
        elif state == JOB_COMPLETED_WITH_ERRORS:
            fail("Mapping task: " + name + " completed with errors.")
        else:
            fail("Mapping task: " + name + " failed.")


def pull_commit(url, session_id, commit_hash):
    """Pull the objects changed in a commit into the org and wait for the result."""
    headers = v3_headers(session_id)
    print("Pulling commit " + commit_hash + " into the org")
    p = requests.post(url + "/public/core/v3/pullByCommitHash", headers = headers, json = {"commitHash": commit_hash})
    if p.status_code != 200:
        fail(p.text)

    pull_action_id = p.json()['pullActionId']
    state = 'NOT_STARTED'
    status_json = {}
    waited = 0
    while state in ('NOT_STARTED', 'IN_PROGRESS'):
        if waited >= PULL_TIMEOUT_SECONDS:
            fail("pull still " + state + " after " + str(PULL_TIMEOUT_SECONDS) + " seconds")
        print("Getting pull status from Informatica")
        time.sleep(PULL_POLL_SECONDS)
        waited += PULL_POLL_SECONDS
        ps = requests.get(url + '/public/core/v3/sourceControlAction/' + pull_action_id,
                          headers = headers, params = {"expand": "objects"})
        if ps.status_code != 200:
            fail(ps.text)
        status_json = ps.json()
        state = status_json['status']['state']

    objects = status_json.get('objects') or []
    print("Pull finished with state " + state + "; " + str(len(objects)) + " object(s):")
    bad = []
    for o in objects:
        target = o.get('target') or {}
        o_state = (target.get('status') or o.get('status') or {}).get('state')
        print("  " + str(o_state) + " " + str(target.get('type')) + " " + object_label(target))
        if o_state in ('FAILED', 'CANCELLED'):
            bad.append(object_label(target))

    if state == 'SUCCESSFUL' and not bad:
        print("Pull of commit " + commit_hash + " completed successfully")
    elif state == 'WARNING' and ALLOW_WARNINGS and not bad:
        print("Pull of commit " + commit_hash + " completed with warnings (allowed by ALLOW_WARNINGS)")
    else:
        fail("Pull was not successful: " + json.dumps(status_json.get('status')) +
             ((" failed objects: " + ", ".join(bad)) if bad else ""))


def logout(url, session_id):
    try:
        requests.post(url + "/public/core/v3/logout", headers = v3_headers(session_id))
    except Exception as e:
        print("Logout failed (ignored): " + str(e))
