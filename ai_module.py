from datetime import datetime, timedelta
import sqlite3
import qrcode
from PIL import Image
import cv2
import numpy as np

# === Risk keywords ===
RISK_KEYWORDS = {
    "leak": "High",
    "corrosion": "High",
    "crack": "High",
    "wear": "Medium",
    "loose": "Medium",
    "ok": "Low",
    "good": "Low",
    "fine": "Low",
    "perfect": "Low",
    "perfect fit": "Low",
    "perfect fittings": "Low"
}

def notes_risk_level(notes):
    if not notes:
        return "Low"
    text = notes.lower()
    if any(word in text for word in ["bad", "worse", "poor", "bad fit", "bad fittings"]):
        return "High"
    for word, risk in RISK_KEYWORDS.items():
        if word in text:
            return risk
    return "Low"

def get_failure_count(uid):
    """Get failure_count directly from fittings table"""
    conn = sqlite3.connect("fittings.db")
    c = conn.cursor()
    c.execute("SELECT failure_count FROM fittings WHERE uid=?", (uid,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else 0

def get_vendor_risk(vendor_id):
    conn = sqlite3.connect("fittings.db")
    c = conn.cursor()
    c.execute("SELECT SUM(failure_count) FROM fittings WHERE vendor_id=?", (vendor_id,))
    total_failures = c.fetchone()[0] or 0
    conn.close()
    if total_failures >= 10:
        return "High"
    elif total_failures >= 5:
        return "Medium"
    else:
        return "Low"

def get_risk_level(payload):
    try:
        today = datetime.today().date()
        warranty_end_str = payload.get("warranty_end") or payload.get("warranty")
        warranty_end = datetime.strptime(warranty_end_str, "%Y-%m-%d").date()
        days_remaining = (warranty_end - today).days

        # --- Warranty risk ---
        if days_remaining < 0:
            warranty_risk = "High"
        elif 0 <= days_remaining <= 29:
            warranty_risk = "High"
        elif 30 <= days_remaining <= 120:
            warranty_risk = "Medium"
        else:
            warranty_risk = "Low"

        # --- Notes risk ---
        notes_risk = notes_risk_level(payload.get("notes", ""))

        # --- Failure risk ---
        failure_count = payload.get("failure_count", 0)
        if failure_count >= 3:
            failure_risk = "High"
        elif failure_count == 2:
            failure_risk = "Medium"
        else:
            failure_risk = "Low"

        # --- Final risk ---
        if "High" in [warranty_risk, notes_risk, failure_risk]:
            return "High"
        elif "Medium" in [warranty_risk, notes_risk, failure_risk]:
            return "Medium"
        else:
            return "Low"

    except Exception as e:
        print("Error calculating risk:", e)
        return "Unknown"

def calculate_dates(manufactor_date, supply_date, warranty_end_str, risk):
    today = datetime.today().date()
    base_date = today

    # Parse manufactor_date or supply_date as base_date
    if manufactor_date:
        try:
            base_date = datetime.strptime(manufactor_date, "%Y-%m-%d").date()
        except:
            pass
    elif supply_date:
        try:
            base_date = datetime.strptime(supply_date, "%Y-%m-%d").date()
        except:
            pass

    # Parse warranty_end
    warranty_end = None
    if warranty_end_str:
        try:
            warranty_end = datetime.strptime(warranty_end_str, "%Y-%m-%d").date()
        except:
            pass

    # --- Initial inspection date ---
    inspection_date = base_date + timedelta(days={"High":30, "Medium":90, "Low":180}[risk])

    # --- Ensure inspection date is after supply_date if provided ---
    if supply_date:
        try:
            supply_dt = datetime.strptime(supply_date, "%Y-%m-%d").date()
            if inspection_date <= supply_dt:
                inspection_date = supply_dt + timedelta(days=1)
        except:
            pass

    # --- Repair date ---
    if risk == "High":
        repair_date = today + timedelta(days=60)
    elif risk == "Medium":
        repair_date = today + timedelta(days=120)
    else:
        repair_date = warranty_end if warranty_end else today + timedelta(days=365)

    # --- Clamp dates to warranty end if available ---
    if warranty_end:
        inspection_date = min(inspection_date, warranty_end)
        repair_date = min(repair_date, warranty_end)

    return inspection_date.isoformat(), repair_date.isoformat()


def update_all_risks():
    conn = sqlite3.connect("fittings.db")
    c = conn.cursor()
    c.execute("SELECT uid, warranty_end, notes, vendor_id, manufactor_date, supply_date FROM fittings")
    rows = c.fetchall()

    for uid, warranty_end, notes, vendor_id, manufactor_date, supply_date in rows:
        failure_count = get_failure_count(uid)
        payload = {
            "uid": uid,
            "warranty_end": warranty_end,
            "notes": notes,
            "failure_count": failure_count
        }
        risk = get_risk_level(payload)
        risk_flag = 1 if risk == "High" else 0

        inspection_date, repair_date = calculate_dates(manufactor_date, supply_date, warranty_end, risk)

        # --- Update row ---
        c.execute("""
            UPDATE fittings
            SET risk=?, risk_flag=?, failure_count=?, inspection_date=?, repair_date=?
            WHERE uid=?
        """, (risk, risk_flag, failure_count, inspection_date, repair_date, uid))

        # --- Update vendor risk ---
        vendor_risk = get_vendor_risk(vendor_id)
        c.execute("UPDATE fittings SET vendor_risk=? WHERE vendor_id=?", (vendor_risk, vendor_id))

    conn.commit()
    conn.close()

# === QR Anomaly Detector ===
class QRAnomalyDetector:
    def __init__(self, error_correction_level='H'):
        self.detector = cv2.QRCodeDetector()
        self.level_map = {
            'L': qrcode.constants.ERROR_CORRECT_L,
            'M': qrcode.constants.ERROR_CORRECT_M,
            'Q': qrcode.constants.ERROR_CORRECT_Q,
            'H': qrcode.constants.ERROR_CORRECT_H
        }
        self.error_correction = self.level_map.get(error_correction_level.upper(), qrcode.constants.ERROR_CORRECT_M)

    def generate_qr(self, data):
        qr = qrcode.QRCode(version=1, error_correction=self.error_correction, box_size=8, border=2)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color='black', back_color='white').convert('RGB')
        cv_img = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        status = self._check_anomaly(cv_img, data)
        return status, img

    def _check_anomaly(self, cv_img, original_data):
        decoded_text, points, _ = self.detector.detectAndDecode(cv_img)
        if not decoded_text:
            return "anomaly detected"
        return "valid" if decoded_text == original_data else "anomaly detected"
