# This sample source code is offered only as an example of what can or might be built using the IICS Github APIs, 
# and is provided for educational purposes only. This source code is provided "as-is" 
# and without representations or warrantees of any kind, is not supported by Informatica.
# Users of this sample code in whole or in part or any extraction or derivative of it 
# assume all the risks attendant thereto, and Informatica disclaims any/all liabilities 
# arising from any such use to the fullest extent permitted by law.

# Pulls UAT_COMMIT_HASH into the target (Dfactory) org, then tests the mapping tasks it changed.

import os
from infa_common import pull_commit, get_commit_changes, test_mapping_tasks, logout

URL = os.environ['UAT_IICS_POD_URL']
UAT_SESSION_ID = os.environ['uat_sessionId']
UAT_COMMIT_HASH = os.environ['UAT_COMMIT_HASH']

try:
    pull_commit(URL, UAT_SESSION_ID, UAT_COMMIT_HASH)
    changes = get_commit_changes(URL, UAT_SESSION_ID, UAT_COMMIT_HASH)
    test_mapping_tasks(URL, UAT_SESSION_ID, changes)
finally:
    logout(URL, UAT_SESSION_ID)
