import requests
import os
import sys

# This sample source code is offered only as an example of what can or might be built using the IICS Github APIs, 
# and is provided for educational purposes only. This source code is provided "as-is" 
# and without representations or warrantees of any kind, is not supported by Informatica.
# Users of this sample code in whole or in part or any extraction or derivative of it 
# assume all the risks attendant thereto, and Informatica disclaims any/all liabilities 
# arising from any such use to the fullest extent permitted by law.

LOGIN_URL = os.environ['IICS_LOGIN_URL'].rstrip('/') + "/saas/public/core/v3/login"

USERNAME = os.environ['IICS_USERNAME']
PASSWORD = os.environ['IICS_PASSWORD']

UAT_USERNAME = os.environ['UAT_IICS_USERNAME']
UAT_PASSWORD = os.environ['UAT_IICS_PASSWORD']


def login(username, password, label):
    r = requests.post(url = LOGIN_URL, json = {"username": username, "password": password})
    if r.status_code != 200:
        print("Caught exception during " + label + " login: " + r.text)
        sys.exit(99)
    return r.json()


data = login(USERNAME, PASSWORD, "development")
uat_data = login(UAT_USERNAME, UAT_PASSWORD, "UAT")

# The org's pod URL (e.g. https://usw1.dmp-us.informaticacloud.com/saas) is returned by the login call
pod_url = data['products'][0]['baseApiUrl']
uat_pod_url = uat_data['products'][0]['baseApiUrl']

# Set session tokens and pod URLs to the environment
env_file = os.getenv('GITHUB_ENV')

with open(env_file, "a") as myfile:
    myfile.write("sessionId=" + data['userInfo']['sessionId'] + "\n")
    myfile.write("uat_sessionId=" + uat_data['userInfo']['sessionId'] + "\n")
    myfile.write("IICS_POD_URL=" + pod_url + "\n")
    myfile.write("UAT_IICS_POD_URL=" + uat_pod_url + "\n")
