import requests

COMPANION_URL = "http://127.0.0.1:5001/receive_data"  # Companion site URL

def push_to_udm(data):
    try:
        response = requests.post(COMPANION_URL, json=data, timeout=5)
        print("[UDM] Response:", response.status_code, response.text)
        return response.status_code == 200
    except Exception as e:
        print("[UDM] Exception:", e)
        return False
