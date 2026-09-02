import base64
import json
import os
import requests

def getWfDependencies(wf_name):
    eiap_url = os.getenv("EIAP_URL", "http://adp-eiap-app1.adp.local")
    url = f"{eiap_url}:8191/svc/mdc/dependency/2/{wf_name}"

    payload = {}
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Basic {base64.b64encode(f'{os.getenv('BUM_USERNAME')}:{os.getenv('BUM_PASSWORD')}'.encode('utf-8')).decode('utf-8')}'
    }

    response = requests.request("GET", url, headers=headers, data=payload)

    return response.text

def getWfUpdYaml(wf_name):
    eiap_url = os.getenv("EIAP_URL", "http://adp-eiap-app1.adp.local")
    url = f"{eiap_url}:8191/svc/mdc/export"

    payload = json.dumps({
        "operation": "update",
        "object": "workflow",
        "name": f"{wf_name}"
    })
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Basic {base64.b64encode(f'{os.getenv('BUM_USERNAME')}:{os.getenv('BUM_PASSWORD')}'.encode('utf-8')).decode('utf-8')}'
    }

    response = requests.request("POST", url, headers=headers, data=payload)

    return response.text