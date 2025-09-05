from flask import Flask, render_template, request, redirect, url_for, jsonify, send_file
import sqlite3
import os
import threading
import webbrowser
import time
import socket
import json
from datetime import datetime, timedelta
import io
import base64
import qrcode
import requests
import qrcode_artistic
# Try to import AI-stylized QR generator; fall back if unavailable
try:
    from qrcode_artistic  import qr_art
    HAS_AI_QR = True
except Exception:
    qr_art = None
    HAS_AI_QR = False

# OpenCV / numpy / PIL
import cv2
import numpy as np
from PIL import Image

# External modules (assumed available)
from udm import push_to_udm
from tms import push_to_tms
from ai_module import get_risk_level, update_all_risks, QRAnomalyDetector

app = Flask(__name__)
DB = 'fittings.db'

# QR code directory
qr_dir = os.path.join("static", "qrcodes")
os.makedirs(qr_dir, exist_ok=True)

# ESP32 endpoint
ESP32_IP = "192.168.29.109"
ESP32_PORT = 8080

# QR Anomaly Detector instance
qr_detector = QRAnomalyDetector()

# === Configuration: enable/disable AI-stylized QR ===
# Leave True to attempt AI QR (will auto-fallback to classic if anything fails)
USE_AI_QR = True

# (Optional) path for a logo/background to embed into AI QR (leave None if not needed)
AI_QR_EMBED_IMAGE = "D:\\CityGrid\\my-project\\qr demo\\static\\image\\rail.png"  # Your specified image path

# === Database Connection Helper ===
def get_db_connection():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

# === Ensure table has required columns (adds missing columns automatically) ===
def ensure_table_columns():
    conn = get_db_connection()
    c = conn.cursor()

    # Create base table if missing (minimal schema)
    c.execute("""
        CREATE TABLE IF NOT EXISTS fittings (
            uid TEXT PRIMARY KEY,
            item_type TEXT,
            vendor TEXT,
            vendor_id TEXT,
            lot TEXT,
            supply_date TEXT,
            warranty TEXT,
            warranty_end TEXT,
            manufactor_date TEXT,
            manufactor_number TEXT,
            notes TEXT,
            udm_synced INTEGER DEFAULT 0,
            tms_synced INTEGER DEFAULT 0,
            risk_flag INTEGER DEFAULT 0,
            risk TEXT DEFAULT 'Low',
            vendor_risk TEXT DEFAULT 'Low'
        )
    """)

    # get existing columns
    c.execute("PRAGMA table_info(fittings)")
    existing_cols = {row[1] for row in c.fetchall()}

    # desired additional columns (name: type)
    wanted = {
        "inspection_date": "TEXT",
        "repair_date": "TEXT",
        "failure_count": "INTEGER DEFAULT 0",
        # manufactor_date/manufactor_number/vendor_risk/vendor_id/etc already in base schema,
        # but keep checks in case base schema changed.
        "manufactor_date": "TEXT",
        "manufactor_number": "TEXT",
        "vendor_risk": "TEXT",
        "vendor_id": "TEXT"
    }

    for col, coltype in wanted.items():
        if col not in existing_cols:
            try:
                c.execute(f"ALTER TABLE fittings ADD COLUMN {col} {coltype}")
                print(f"[DB] Added missing column: {col}")
            except Exception as e:
                print(f"[DB] Failed adding column {col}: {e}")

    conn.commit()
    conn.close()

# Run schema check at startup
ensure_table_columns()

# === Date calculation helpers ===
def calculate_dates(manufactor_date, supply_date, warranty_end_str, risk):
    today = datetime.today().date()
    base_date = None
    for d in (manufactor_date, supply_date):
        if d:
            try:
                base_date = datetime.strptime(d, "%Y-%m-%d").date()
                break
            except Exception:
                base_date = None
    if base_date is None:
        base_date = today

    warranty_end = None
    if warranty_end_str:
        try:
            warranty_end = datetime.strptime(warranty_end_str, "%Y-%m-%d").date()
        except Exception:
            warranty_end = None

    if risk == "High":
        inspection_date = base_date + timedelta(days=30)
    elif risk == "Medium":
        inspection_date = base_date + timedelta(days=90)
    else:
        inspection_date = base_date + timedelta(days=180)

    if risk == "High":
        repair_date = today + timedelta(days=60)
    elif risk == "Medium":
        repair_date = today + timedelta(days=120)
    else:
        repair_date = warranty_end if warranty_end else (today + timedelta(days=365))

    if warranty_end:
        if inspection_date > warranty_end:
            inspection_date = warranty_end
        if repair_date > warranty_end:
            repair_date = warranty_end

    return inspection_date.isoformat(), repair_date.isoformat()

def compute_next_inspection(inspection_date_str, repair_date_str, risk):
    today = datetime.today().date()
    if inspection_date_str:
        try:
            dt = datetime.strptime(inspection_date_str, "%Y-%m-%d").date()
            return dt.isoformat()
        except Exception:
            pass

    if repair_date_str:
        try:
            rd = datetime.strptime(repair_date_str, "%Y-%m-%d").date()
            if risk == "High":
                delta = timedelta(days=90)
            elif risk == "Medium":
                delta = timedelta(days=180)
            else:
                delta = timedelta(days=365)
            next_dt = rd + delta
            return next_dt.isoformat()
        except Exception:
            pass

    return "Not scheduled"

# === Vendor Risk Calculation Helper ===
def calculate_vendor_risk(vendor):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM fittings WHERE vendor=? AND risk_flag=1", (vendor,))
    failures = c.fetchone()[0]
    conn.close()
    if failures >= 5:
        return "High"
    elif failures >= 2:
        return "Medium"
    else:
        return "Low"

# === QR Content Generation ===
def generate_qr_content(uid, item_type, vendor, lot, supply_date, warranty_end, manufactor_date, manufactor_number, notes, risk, vendor_risk):
    qr_payload = {
        "uid": uid,
        "item_type": item_type,
        "vendor": vendor,
        "lot": lot,
        "supply_date": supply_date,
        "warranty_end": warranty_end,
        "manufactor_date": manufactor_date,
        "manufactor_number": manufactor_number,
        "notes": notes,
        "risk": risk,
        "vendor_risk": vendor_risk,
    }
    return json.dumps(qr_payload)

# === helper to generate base64 inline QR for templates (unchanged) ===
def generate_qr_image_base64(qr_content):
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(qr_content)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode('ascii')
    return b64

# === Centralized QR image saver (AI-stylized with safe fallback) ===
def save_qr_image(uid, qr_content):
    """
    Saves a QR image at static/qrcodes/<uid>.png.
    Priority:
    1. Try Stable Diffusion (Replicate API) for anime-style QR if USE_AI_QR enabled.
    2. If not available/fails, try qrcode_artistic.
    3. If all fail, fallback to classic QR with logo.
    """
    qr_path = os.path.join(qr_dir, f"{uid}.png")
    
    # === Step 1: Try anime-style AI QR (Replicate Stable Diffusion + ControlNet) ===
    if USE_AI_QR and "REPLICATE_API_TOKEN" in os.environ:
        try:
            # Create a basic QR code first to use as input
            basic_qr = qrcode.make(qr_content).convert("RGB")
            basic_qr_path = os.path.join(qr_dir, f"{uid}_basic.png")
            basic_qr.save(basic_qr_path)
            
            # Convert to base64 for API
            with open(basic_qr_path, "rb") as f:
                qr_b64 = base64.b64encode(f.read()).decode("utf-8")
            
            # Prepare prompt with logo mention if available
            prompt = "anime style, pastel colors, kawaii theme, scannable QR code"
            if AI_QR_EMBED_IMAGE and os.path.exists(AI_QR_EMBED_IMAGE):
                prompt += ", with a subtle logo in the center"
            
            url = "https://api.replicate.com/v1/predictions"
            headers = {
                "Authorization": f"Token {os.environ['REPLICATE_API_TOKEN']}",
                "Content-Type": "application/json"
            }
            payload = {
                "version": "latest",
                "input": {
                    "image": qr_b64,
                    "prompt": prompt,
                    "strength": 0.85,
                    "guidance_scale": 7.5
                }
            }
            response = requests.post(url, headers=headers, json=payload)
            if response.status_code == 201:
                result = response.json()
                poll_url = result["urls"]["get"]

                # Poll until complete
                for _ in range(30):  # Max 30 attempts (60 seconds)
                    r = requests.get(poll_url, headers=headers)
                    result = r.json()
                    if result["status"] == "succeeded":
                        anime_url = result["output"][0]
                        img_data = requests.get(anime_url).content
                        with open(qr_path, "wb") as f:
                            f.write(img_data)
                        print(f"[AI QR] Anime-style QR saved at {qr_path}")
                        
                        # Clean up temporary file
                        if os.path.exists(basic_qr_path):
                            os.remove(basic_qr_path)
                            
                        return qr_path
                    elif result["status"] == "failed":
                        print("[AI QR] Replicate failed, falling back.")
                        break
                    time.sleep(2)
        except Exception as e:
            print(f"[AI QR] Exception: {e}, falling back.")

    # === Step 2: Try qrcode_artistic if available ===
    if USE_AI_QR and HAS_AI_QR:
        try:
            ai_img = qr_art(
                data=qr_content,
                mode="art",
                color="black",
                background="white",
                embed_img=AI_QR_EMBED_IMAGE if AI_QR_EMBED_IMAGE and os.path.exists(AI_QR_EMBED_IMAGE) else None
            )
            if isinstance(ai_img, Image.Image):
                ai_img.save(qr_path)
                print(f"[AI QR] Artistic QR saved at {qr_path}")
                return qr_path
        except Exception as e:
            print(f"[AI QR] qrcode_artistic failed: {e}, falling back.")

    # === Step 3: Classic QR with logo fallback ===
    try:
        # Generate basic QR code
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=10,
            border=4,
        )
        qr.add_data(qr_content)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white").convert('RGB')
        
        # Add logo if available
        if AI_QR_EMBED_IMAGE and os.path.exists(AI_QR_EMBED_IMAGE):
            try:
                logo = Image.open(AI_QR_EMBED_IMAGE)
                
                # Calculate logo size (20% of QR code size)
                base_width = min(img.size[0] // 5, img.size[1] // 5)
                wpercent = (base_width / float(logo.size[0]))
                hsize = int((float(logo.size[1]) * float(wpercent)))
                logo = logo.resize((base_width, hsize), Image.LANCZOS)
                
                # Calculate position to center the logo
                pos = ((img.size[0] - logo.size[0]) // 2, 
                       (img.size[1] - logo.size[1]) // 2)
                
                # Create a transparent background for the logo
                logo_with_bg = Image.new("RGBA", img.size, (0, 0, 0, 0))
                logo_with_bg.paste(logo, pos)
                
                # Convert QR to RGBA to support transparency
                img = img.convert("RGBA")
                
                # Composite the logo onto the QR code
                img = Image.alpha_composite(img, logo_with_bg)
                
            except Exception as e:
                print(f"[QR Logo] Failed to add logo: {e}")
        
        img.save(qr_path)
        print(f"[QR] Classic QR saved at {qr_path}")
    except Exception as e:
        print(f"[QR] Exception: {e}")
    
    return qr_path

# === QR -> G-code functions ===
def qr_to_gcode_final(image_path, laser_power=255, travel_speed=5000, engrave_speed=1500, target_size_mm=25.0):
    img = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return "G21\nG90\nM5\nG0 X0 Y0\n;(Error: Failed to load image)"
    height, width = img.shape
    scale_factor = target_size_mm / max(width, height)
    _, thresh = cv2.threshold(img, 127, 255, cv2.THRESH_BINARY_INV)
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    min_contour_area = 5
    significant_contours = [cnt for cnt in contours if cv2.contourArea(cnt) > min_contour_area]
    gcode_lines = ["G21", "G90", f"G0 F{travel_speed}", f"G1 F{engrave_speed}", "M3 S0", "G0 X0 Y0"]
    for contour in significant_contours:
        epsilon = 0.002 * cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, epsilon, True)
        if len(approx) < 2:
            continue
        first_point = approx[0][0]
        x_start = round(first_point[0] * scale_factor, 3)
        y_start = round(first_point[1] * scale_factor, 3)
        gcode_lines.append(f"G0 X{x_start} Y{y_start}")
        gcode_lines.append(f"M3 S{laser_power}")
        for point in approx[1:]:
            x = round(point[0][0] * scale_factor, 3)
            y = round(point[0][1] * scale_factor, 3)
            gcode_lines.append(f"G1 X{x} Y{y}")
        gcode_lines.append(f"G1 X{x_start} Y{y_start}")
        gcode_lines.append("M3 S0")
    gcode_lines.append("G0 X0 Y0")
    gcode_lines.append("M5")
    return "\n".join(gcode_lines)

def qr_to_gcode_fallback(image_path, laser_power=255, scale=1.0):
    img = Image.open(image_path).convert("L")
    width, height = img.size
    pixels = img.load()
    gcode_lines = ["G21 ; Set units to mm", "G90 ; Absolute positioning", "M3 S0 ; Laser off at start"]
    for y in range(height):
        x = 0
        while x < width:
            while x < width and pixels[x, y] >= 128:
                x += 1
            if x >= width:
                break
            start_x = x
            while x < width and pixels[x, y] < 128:
                x += 1
            end_x = x - 1
            gx_start = round(start_x * scale, 3)
            gy = round(y * scale, 3)
            gx_end = round(end_x * scale, 3)
            gcode_lines.append(f"G0 X{gx_start} Y{gy}")
            gcode_lines.append(f"M3 S{laser_power}")
            gcode_lines.append(f"G1 X{gx_end} Y{gy}")
            gcode_lines.append("M3 S0")
    gcode_lines.append("M5 ; Laser off at end")
    gcode_lines.append("G0 X0 Y0 ; Return to origin")
    return "\n".join(gcode_lines)

# === Send G-code to ESP32 ===
def send_gcode_to_esp32_enhanced(gcode_text, timeout=3, command_delay=0.02):
    lines = [line.strip() for line in gcode_text.splitlines() if line.strip() and not line.strip().startswith(';')]
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((ESP32_IP, ESP32_PORT))
        successful_commands = 0
        total_commands = len(lines)
        for i, command in enumerate(lines):
            try:
                s.sendall((command + '\n').encode('utf-8'))
                s.settimeout(1.0)
                try:
                    response = s.recv(32).decode('utf-8', errors='ignore').strip()
                    if 'ok' in response.lower() or 'done' in response.lower():
                        successful_commands += 1
                    else:
                        print(f"Unexpected response: {response}")
                except socket.timeout:
                    successful_commands += 1
                    print(f"No response for command: {command}")
                if i % 10 == 0:
                    print(f"Progress: {i}/{total_commands} commands")
                time.sleep(command_delay)
            except Exception as cmd_error:
                print(f"Error sending command '{command}': {cmd_error}")
                continue
        s.close()
        success_rate = (successful_commands / total_commands) * 100 if total_commands > 0 else 100.0
        message = f"Completed: {successful_commands}/{total_commands} commands ({success_rate:.1f}%)"
        return success_rate > 90, message
    except Exception as e:
        print(f"Connection error: {e}")
        return False, f"Connection failed: {e}"

# === Home / Add Fitting ===
@app.route('/', methods=['GET', 'POST'])
def index():
    error = None
    if request.method == 'POST':
        uid = request.form['uid']
        item_type = request.form['item_type']
        vendor = request.form['vendor']
        lot = request.form['lot']
        supply_date = request.form['supply_date']
        warranty_end = request.form['warranty_end']
        manufactor_date = request.form.get('manufactor_date', '')
        manufactor_number = request.form.get('manufactor_number', '')
        notes = request.form.get('notes', '')

        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT * FROM fittings WHERE uid=?", (uid,))
        if c.fetchone():
            conn.close()
            error = "UID already exists!"
            return render_template('index.html', error=error, request=request)

        # --- Initialize variables ---
        inspection_date = None
        repair_date = None
        risk_level = "Low"
        vendor_risk = "Low"

        # --- Calculate risk and vendor risk ---
        try:
            payload = {
                "uid": uid, "item_type": item_type, "vendor": vendor, "lot": lot,
                "supply_date": supply_date, "warranty_end": warranty_end, "notes": notes
            }
            risk_level = get_risk_level(payload)
            vendor_risk = calculate_vendor_risk(vendor)
        except Exception as e:
            print(f"[Risk Calculation] Exception: {e}")

        # --- Calculate inspection & repair dates safely ---
        try:
            inspection_date, repair_date = calculate_dates(manufactor_date, supply_date, warranty_end, risk_level)
        except Exception as e:
            print(f"[Date Calculation] Exception: {e}")
            # fallback values
            inspection_date = supply_date or datetime.today().strftime("%Y-%m-%d")
            repair_date = warranty_end or datetime.today().strftime("%Y-%m-%d")

        # --- Generate QR code content ---
        qr_content = generate_qr_content(
            uid, item_type, vendor, lot, supply_date, warranty_end,
            manufactor_date, manufactor_number, notes, risk_level, vendor_risk
        )

        # --- Generate and save QR image (AI or classic) ---
        qr_path = save_qr_image(uid, qr_content)

        # --- Insert into DB ---
        try:
            c.execute("""INSERT INTO fittings 
                (uid, item_type, vendor, lot, supply_date, warranty, warranty_end, 
                 manufactor_date, manufactor_number, notes, udm_synced, tms_synced, 
                 risk_flag, risk, vendor_risk, inspection_date, repair_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?)""",
                (uid, item_type, vendor, lot, supply_date, supply_date, warranty_end,
                 manufactor_date, manufactor_number, notes,
                 1 if risk_level == "High" else 0, risk_level, vendor_risk,
                 inspection_date, repair_date)
            )
            conn.commit()
        except Exception as e:
            print(f"[DB Insert] Exception: {e}")
            conn.close()
            error = "Database insert failed."
            return render_template('index.html', error=error, request=request)
        conn.close()

        # --- Update global risks ---
        try:
            update_all_risks()
        except Exception as e:
            print(f"[Global Risk Update] Exception: {e}")

        # --- Push to UDM ---
        try:
            payload["repair_date"] = repair_date
            payload["inspection_date"] = inspection_date
            payload["risk"] = risk_level
            payload["vendor_risk"] = vendor_risk

            if push_to_udm(payload):
                conn = get_db_connection()
                c = conn.cursor()
                c.execute("UPDATE fittings SET udm_synced=1 WHERE uid=?", (uid,))
                conn.commit()
                conn.close()
        except Exception as e:
            print(f"[UDM Push] Exception: {e}")

        # --- Push to TMS ---
        try:
            if push_to_tms(payload):
                conn = get_db_connection()
                c = conn.cursor()
                c.execute("UPDATE fittings SET tms_synced=1 WHERE uid=?", (uid,))
                conn.commit()
                conn.close()
        except Exception as e:
            print(f"[TMS Push] Exception: {e}")

        return redirect(url_for('view_record', uid=uid))

    return render_template('index.html', error=error, request=request)

# === View All Fittings ===
@app.route('/all')
def view_all():
    sort_by = request.args.get('sort_by', 'uid')
    valid_columns = ['uid', 'lot', 'supply_date', 'warranty_end','manufactor_date', 'manufactor_number',  'vendor', 'risk', 'item_type', 'vendor_risk']
    if sort_by not in valid_columns:
        sort_by = 'uid'
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(f"SELECT * FROM fittings ORDER BY {sort_by} ASC")
    rows = c.fetchall()
    conn.close()

    data = []
    for r in rows:
        rd = dict(r)
        rd['next_inspection'] = compute_next_inspection(rd.get('inspection_date'), rd.get('repair_date'), rd.get('risk'))
        data.append(rd)

    return render_template('all.html', rows=data, sort_by=sort_by)

# === View Single Record ===
@app.route('/view/<uid>')
def view_record(uid):
    message = request.args.get('msg')
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM fittings WHERE uid=?", (uid,))
    row = c.fetchone()
    conn.close()
    if not row:
        return "Not found", 404
    return render_template('view.html', row=row, message=message)

# === Send G-code Manually ===
@app.route('/send_gcode/<uid>', methods=['POST'])
def send_gcode(uid):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM fittings WHERE uid=?", (uid,))
    row = c.fetchone()
    conn.close()
    if not row:
        return f"Fitting with UID {uid} not found.", 404
    qr_path = os.path.join(qr_dir, f"{uid}.png")
    
    # Generate QR code if it doesn't exist or needs update
    if not os.path.exists(qr_path):
        row_dict = dict(row)
        qr_content = generate_qr_content(
            row_dict.get('uid'), row_dict.get('item_type'), row_dict.get('vendor'), row_dict.get('lot'), 
            row_dict.get('supply_date'), row_dict.get('warranty_end'), row_dict.get('manufactor_date',''),
            row_dict.get('manufactor_number',''), row_dict.get('notes',''), 
            row_dict.get('risk','Low'), row_dict.get('vendor_risk','Low')
        )
        save_qr_image(uid, qr_content)  # AI or classic

    try:
        gcode_text = qr_to_gcode_final(qr_path, laser_power=255, travel_speed=5000, engrave_speed=1500, target_size_mm=20.0)
        print(f"Generated {len(gcode_text.splitlines())} lines of G-code")
    except Exception as e:
        print(f"G-code generation failed: {e}, using fallback")
        gcode_text = qr_to_gcode_fallback(qr_path, laser_power=255, scale=0.5)
    success, resp_text = send_gcode_to_esp32_enhanced(gcode_text)
    print(f"G-code send result: {success}, {resp_text}")
    msg = f"G-code sent successfully! {resp_text}" if success else f"Failed: {resp_text}"
    return redirect(url_for('view_record', uid=uid, msg=msg))

# === Regenerate QR ===
@app.route('/regenerate_qr/<uid>', methods=['POST'])
def regenerate_qr(uid):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM fittings WHERE uid=?", (uid,))
    row = c.fetchone()
    conn.close()
    if not row:
        return f"Fitting with UID {uid} not found.", 404
    row_dict = dict(row)
    qr_content = generate_qr_content(
        row_dict.get('uid'), row_dict.get('item_type'), row_dict.get('vendor'), row_dict.get('lot'), 
        row_dict.get('supply_date'), row_dict.get('warranty_end'), row_dict.get('manufactor_date',''),
        row_dict.get('manufactor_number',''), row_dict.get('notes',''), 
        row_dict.get('risk','Low'), row_dict.get('vendor_risk','Low')
    )
    save_qr_image(uid, qr_content)  # AI or classic
    msg = f"QR regenerated for UID {uid}."
    return redirect(url_for('view_record', uid=uid, msg=msg))

# === QR Code Scanning Endpoint (renders your template) ===
@app.route('/scan/<uid>', methods=['GET'])
def scan(uid):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM fittings WHERE uid=?", (uid,))
    row = c.fetchone()
    conn.close()

    if not row:
        return "UID not found", 404

    row_dict = dict(row)
    risk = row_dict.get('risk', 'Unknown')
    vendor_risk = row_dict.get('vendor_risk', 'Unknown')
    inspection_date = row_dict.get('inspection_date') or compute_next_inspection(row_dict.get('inspection_date'), row_dict.get('repair_date'), row_dict.get('risk'))
    qr_content = generate_qr_content(
        row_dict.get('uid'),
        row_dict.get('item_type', ''),
        row_dict.get('vendor', ''),
        row_dict.get('lot', ''),
        row_dict.get('supply_date', ''),
        row_dict.get('warranty_end', ''),
        row_dict.get('manufactor_date', ''),
        row_dict.get('manufactor_number', ''),
        row_dict.get('notes', ''),
        risk,
        vendor_risk
    )
    qr_b64 = generate_qr_image_base64(qr_content)
    return render_template(
        'scan_result.html',
        uid=row_dict.get('uid'),
        risk=risk,
        vendor_risk=vendor_risk,
        inspection_date=inspection_date,
        qr_code=qr_b64
    )

# === Test QR Generation ===
@app.route('/test_qr/<uid>')
def test_qr(uid):
    """Test route to see the generated QR code"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM fittings WHERE uid=?", (uid,))
    row = c.fetchone()
    conn.close()
    
    if not row:
        return "UID not found", 404
        
    row_dict = dict(row)
    qr_content = generate_qr_content(
        row_dict.get('uid'), row_dict.get('item_type'), row_dict.get('vendor'), row_dict.get('lot'), 
        row_dict.get('supply_date'), row_dict.get('warranty_end'), row_dict.get('manufactor_date',''),
        row_dict.get('manufactor_number',''), row_dict.get('notes',''), 
        row_dict.get('risk','Low'), row_dict.get('vendor_risk','Low')
    )
    
    # Generate the QR code
    qr_path = save_qr_image(uid, qr_content)
    
    # Return the image
    return send_file(qr_path, mimetype='image/png')

# === Periodic Risk Update ===
def periodic_risk_update():
    while True:
        try:
            update_all_risks()
        except Exception as e:
            print("[Risk Update] Exception:", e)
        time.sleep(3600)

# === Validate All QR Codes on Startup ===
def validate_all_qr_codes():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM fittings")
    rows = c.fetchall()
    for row in rows:
        row_dict = dict(row)
        uid = row_dict.get('uid')
        qr_path = os.path.join(qr_dir, f"{uid}.png")
        qr_content = generate_qr_content(
            row_dict.get('uid'), row_dict.get('item_type'), row_dict.get('vendor'), row_dict.get('lot'), 
            row_dict.get('supply_date'), row_dict.get('warranty_end'), row_dict.get('manufactor_date',''),
            row_dict.get('manufactor_number',''), row_dict.get('notes',''), 
            row_dict.get('risk','Low'), row_dict.get('vendor_risk','Low')
        )
        try:
            if not os.path.exists(qr_path):
                save_qr_image(uid, qr_content)  # AI or classic
                print(f"[QR Validation] Generated QR for UID {uid}")
        except Exception as e:
            print(f"[QR Validation] Error for UID {uid}: {e}")
    conn.close()
    print("[QR Validation] All QR codes checked.")

def retry_pending_sync():
    while True:
        conn = get_db_connection()
        c = conn.cursor()
        # Retry UDM
        c.execute("SELECT * FROM fittings WHERE udm_synced=0")
        pending_udm = c.fetchall()
        for row in pending_udm:
            r = dict(row)
            payload = {
                "uid": r.get('uid'),
                "item_type": r.get('item_type'),
                "vendor": r.get('vendor'),
                "lot": r.get('lot'),
                "supply_date": r.get('supply_date'),
                "warranty_end": r.get('warranty_end'),
                "manufactor_date": r.get('manufactor_date'),
                "manufactor_number": r.get('manufactor_number'),
                "repair_date": r.get('repair_date'),
                "inspection_date": r.get('inspection_date'),
                "risk": r.get('risk'),
                "vendor_risk": r.get('vendor_risk'),
                "notes": r.get('notes')
            }
            try:
                if push_to_udm(payload):
                    c.execute("UPDATE fittings SET udm_synced=1 WHERE uid=?", (r.get('uid'),))
                    conn.commit()
                    print(f"[UDM Retry] UID {r.get('uid')} synced successfully.")
            except Exception as e:
                print(f"[UDM Retry] error pushing {r.get('uid')}: {e}")

        # Retry TMS
        c.execute("SELECT * FROM fittings WHERE tms_synced=0")
        pending_tms = c.fetchall()
        for row in pending_tms:
            r = dict(row)
            payload = {
                "uid": r.get('uid'),
                "item_type": r.get('item_type'),
                "vendor": r.get('vendor'),
                "lot": r.get('lot'),
                "supply_date": r.get('supply_date'),
                "warranty_end": r.get('warranty_end'),
                "manufactor_date": r.get('manufactor_date'),
                "manufactor_number": r.get('manufactor_number'),
                "repair_date": r.get('repair_date'),
                "inspection_date": r.get('inspection_date'),
                "risk": r.get('risk'),
                "vendor_risk": r.get('vendor_risk'),
                "notes": r.get('notes')
            }
            try:
                if push_to_tms(payload):
                    c.execute("UPDATE fittings SET tms_synced=1 WHERE uid=?", (r.get('uid'),))
                    conn.commit()
                    print(f"[TMS Retry] UID {r.get('uid')} synced successfully.")
            except Exception as e:
                print(f"[TMS Retry] error pushing {r.get('uid')}: {e}")

        conn.close()
        time.sleep(10)

# === Main ===
if __name__ == '__main__':
    threading.Thread(target=periodic_risk_update, daemon=True).start()
    threading.Thread(target=validate_all_qr_codes, daemon=True).start()
    threading.Thread(target=retry_pending_sync, daemon=True).start()

    def open_browser():
        webbrowser.open_new("http://127.0.0.1:5000")
    threading.Timer(1.0, open_browser).start()

    app.run(debug=True, host="0.0.0.0")
