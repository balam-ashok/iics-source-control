# Pulls a single commit from the source control repo into the target (Dfactory) org.
# Use it for assets that are on Dfactory-Branch but have not been pulled into the org yet.
#
# Needs (set by the workflow / scripts/infa_login.py):
#   UAT_IICS_POD_URL, uat_sessionId, UAT_COMMIT_HASH
# Optional:
#   PULL_TIMEOUT_SECONDS (default 1800)

import requests
import os
import json
import time
import sys

URL = os.environ['UAT_IICS_POD_URL']
UAT_SESSION_ID = os.environ['uat_sessionId']
UAT_COMMIT_HASH = os.environ['UAT_COMMIT_HASH']
MAX_WAIT_SECONDS = int(os.environ.get('PULL_TIMEOUT_SECONDS', '1800'))

HEADERS = {"Content-Type": "application/json; charset=utf-8", "INFA-SESSION-ID": UAT_SESSION_ID }

BODY = { "commitHash": UAT_COMMIT_HASH }

print("Pulling commit " + UAT_COMMIT_HASH + " into the target org")

# Sync Github and target org
p = requests.post(URL + "/public/core/v3/pullByCommitHash", headers = HEADERS, json = BODY)

if p.status_code != 200:
    print("Exception caught: " + p.text)
    sys.exit(99)

pull_json = p.json()
PULL_ACTION_ID = pull_json['pullActionId']
PULL_STATUS = 'IN_PROGRESS'
pull_status_json = {}
waited = 0

while PULL_STATUS in ('IN_PROGRESS', 'NOT_STARTED'):
    if waited >= MAX_WAIT_SECONDS:
        print("Exception caught: pull still " + PULL_STATUS + " after " + str(MAX_WAIT_SECONDS) + " seconds")
        sys.exit(99)
    print("Getting pull status from Informatica")
    time.sleep(10)
    waited += 10
    ps = requests.get(URL + '/public/core/v3/sourceControlAction/' + PULL_ACTION_ID, headers = HEADERS)
    if ps.status_code != 200:
        print("Exception caught: " + ps.text)
        sys.exit(99)
    pull_status_json = ps.json()
    PULL_STATUS = pull_status_json['status']['state']

requests.post(URL + "/public/core/v3/logout", headers = HEADERS)

if PULL_STATUS != 'SUCCESSFUL':
    print('Exception caught: Pull was not successful: ' + json.dumps(pull_status_json.get('status')))
    sys.exit(99)

print("Pull of commit " + UAT_COMMIT_HASH + " completed successfully")
