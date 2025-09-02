import requests

COMPANION_URL = "http://127.0.0.1:5001/receive_data"  # Use same for demo

def push_to_tms(data):
    try:
        response = requests.post(COMPANION_URL, json=data, timeout=5)
        print("[TMS] Response:", response.status_code, response.text)
        return response.status_code == 200
    except Exception as e:
        print("[TMS] Exception:", e)
        return False
