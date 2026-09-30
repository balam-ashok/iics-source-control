# Pulls a single commit from the source control repo into the target (Dfactory) org.
# Use it for assets that are on Dfactory-Branch but have not been pulled into the org yet.
#
# Needs (set by the workflow / scripts/infa_login.py):
#   UAT_IICS_POD_URL, uat_sessionId, UAT_COMMIT_HASH
# Optional: see scripts/infa_common.py (ALLOW_WARNINGS, PULL_TIMEOUT_SECONDS)

import os
from infa_common import pull_commit, logout

URL = os.environ['UAT_IICS_POD_URL']
UAT_SESSION_ID = os.environ['uat_sessionId']
UAT_COMMIT_HASH = os.environ['UAT_COMMIT_HASH']

try:
    pull_commit(URL, UAT_SESSION_ID, UAT_COMMIT_HASH)
finally:
    logout(URL, UAT_SESSION_ID)
