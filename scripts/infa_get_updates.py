# This sample source code is offered only as an example of what can or might be built using the IICS Github APIs, 
# and is provided for educational purposes only. This source code is provided "as-is" 
# and without representations or warrantees of any kind, is not supported by Informatica.
# Users of this sample code in whole or in part or any extraction or derivative of it 
# assume all the risks attendant thereto, and Informatica disclaims any/all liabilities 
# arising from any such use to the fullest extent permitted by law.

# Tests the mapping tasks changed in COMMIT_HASH in the source (Demo) org.

import os
from infa_common import get_commit_changes, test_mapping_tasks, logout

URL = os.environ['IICS_POD_URL']
SESSION_ID = os.environ['sessionId']
COMMIT_HASH = os.environ['COMMIT_HASH']

try:
    changes = get_commit_changes(URL, SESSION_ID, COMMIT_HASH)
    test_mapping_tasks(URL, SESSION_ID, changes)
finally:
    logout(URL, SESSION_ID)
