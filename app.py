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
    from qrcode_artistic import qr_art
    HAS_AI_QR = True
except Exception:
    qr_art = None
    HAS_AI_QR = False

# OpenCV / numpy / PIL
import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import asyncio
import websockets

# External modules (assumed available)
from udm import push_to_udm
from tms import push_to_tms
from ai_module import get_risk_level, update_all_risks, QRAnomalyDetector

app = Flask(__name__)
DB = 'fittings.db'

# QR code directory
qr_dir = os.path.join("static", "qrcodes")
os.makedirs(qr_dir, exist_ok=True)

# ESP32 endpoint (update if you want)
ESP32_IP = "192.168.29.109"
ESP32_WS = f"ws://{ESP32_IP}:81"

# QR Anomaly Detector instance
qr_detector = QRAnomalyDetector()

# === Configuration: enable/disable AI-stylized QR ===
USE_AI_QR = True

# Path for a logo/background to embed into QR (user insisted logo is mandatory)
AI_QR_EMBED_IMAGE = "D:\\CityGrid\\my-project\\qr demo\\static\\image\\rail.png"

# === Database Connection Helper ===
def get_db_connection():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

# === Ensure table has required columns (adds missing columns automatically) ===
def ensure_table_columns():
    conn = get_db_connection()
    c = conn.cursor()
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
    c.execute("PRAGMA table_info(fittings)")
    existing_cols = {row[1] for row in c.fetchall()}

    wanted = {
        "inspection_date": "TEXT",
        "repair_date": "TEXT",
        "failure_count": "INTEGER DEFAULT 0",
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

# === helper to generate base64 inline QR for templates ===
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

# === Create anime-themed QR code with logo ===
def create_anime_qr_with_logo(qr_content, logo_path=None):
    """Create an anime-themed QR code with optional logo"""
    try:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=10,
            border=4,
        )
        qr.add_data(qr_content)
        qr.make(fit=True)

        img = qr.make_image(fill_color="black", back_color="white").convert('RGB')

        # Apply anime-style effects if requested
        img = apply_anime_effects(img)

        # Add logo (logo is mandatory per your instruction)
        if logo_path:
            if os.path.exists(logo_path):
                img = add_logo_to_qr(img, logo_path)
            else:
                print(f"[Logo] Logo file not found at {logo_path} (logo is required). Proceeding without visual logo overlay.")
        return img
    except Exception as e:
        print(f"Anime QR creation failed: {e}")
        return qrcode.make(qr_content).convert('RGB')

def apply_anime_effects(img):
    """Apply anime-style visual effects to the QR code"""
    try:
        img_array = np.array(img)
        h, w = img_array.shape[:2]
        for i in range(h):
            for j in range(w):
                if img_array[i, j, 0] < 128:  # Dark pixels
                    if (i + j) % 4 == 0:
                        img_array[i, j] = [50, 100, 200]
                    elif (i + j) % 4 == 1:
                        img_array[i, j] = [200, 100, 150]
                else:
                    img_array[i, j] = [245, 230, 240]
        img = Image.fromarray(img_array.astype('uint8'))
        draw = ImageDraw.Draw(img)
        width, height = img.size
        border_color = (255, 150, 200)
        draw.rectangle([0, 0, width-1, height-1], outline=border_color, width=3)
        corner_size = 15
        for x, y in [(0, 0), (width-corner_size, 0), (0, height-corner_size), (width-corner_size, height-corner_size)]:
            draw.ellipse([x, y, x+corner_size, y+corner_size], fill=border_color)
        return img
    except Exception as e:
        print(f"Anime effects failed: {e}")
        return img

def add_logo_to_qr(qr_img, logo_path):
    """Add logo to the center of QR code"""
    try:
        logo = Image.open(logo_path)
        base_width = min(qr_img.size[0] // 5, qr_img.size[1] // 5)
        wpercent = (base_width / float(logo.size[0]))
        hsize = int((float(logo.size[1]) * float(wpercent)))
        logo = logo.resize((base_width, hsize), Image.LANCZOS)

        pos = ((qr_img.size[0] - logo.size[0]) // 2,
               (qr_img.size[1] - logo.size[1]) // 2)

        if logo.mode != 'RGBA':
            logo = logo.convert('RGBA')

        white_bg = Image.new('RGBA', logo.size, (255, 255, 255, 255))
        white_bg.paste(logo, (0, 0), logo if logo.mode == 'RGBA' else None)

        # Ensure QR image is RGBA to preserve transparency if merging
        if qr_img.mode != 'RGBA':
            qr_img = qr_img.convert('RGBA')
        qr_img.paste(white_bg, pos, white_bg)
        return qr_img.convert('RGB')
    except Exception as e:
        print(f"Logo addition failed: {e}")
        return qr_img

# === Centralized QR image saver (creates display + engrave) ===
def save_qr_image(uid, qr_content):
    """
    Saves two QR images:
    - <uid>_display.png (pink background + logo) for UI
    - <uid>_engrave.png (white background + logo, 1-bit B/W) for laser engraving
    Returns (display_path, engrave_path)
    """
    qr_path_display = os.path.join(qr_dir, f"{uid}_display.png")
    qr_path_engrave = os.path.join(qr_dir, f"{uid}_engrave.png")

    # Generate base QR
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(qr_content)
    qr.make(fit=True)

    # 1) Display QR (pink background)
    try:
        if USE_AI_QR:
            # attempt an "artistic/anime" style first
            img_disp = create_anime_qr_with_logo(qr_content, AI_QR_EMBED_IMAGE)
        else:
            img_disp = qr.make_image(fill_color="black", back_color="pink").convert("RGB")
            if AI_QR_EMBED_IMAGE and os.path.exists(AI_QR_EMBED_IMAGE):
                img_disp = add_logo_to_qr(img_disp, AI_QR_EMBED_IMAGE)
    except Exception as e:
        print(f"[save_qr_image] display generation failed: {e}")
        img_disp = qr.make_image(fill_color="black", back_color="pink").convert("RGB")
        if AI_QR_EMBED_IMAGE and os.path.exists(AI_QR_EMBED_IMAGE):
            img_disp = add_logo_to_qr(img_disp, AI_QR_EMBED_IMAGE)

    img_disp.save(qr_path_display)
    print(f"[QR] Display QR saved at {qr_path_display}")

    # 2) Engrave QR (strict black/white, with logo area white to keep scannable)
    try:
        img_eng = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        # Add logo (mandatory) on engrave image as well
        if AI_QR_EMBED_IMAGE and os.path.exists(AI_QR_EMBED_IMAGE):
            img_eng = add_logo_to_qr(img_eng, AI_QR_EMBED_IMAGE)
        # Force strict B/W (1-bit) - helps reduce g-code size and ensures engraving clarity
        img_eng = img_eng.convert("L").point(lambda p: 0 if p < 128 else 255, "1")
    except Exception as e:
        print(f"[save_qr_image] engrave generation failed: {e}")
        img_eng = qr.make_image(fill_color="black", back_color="white").convert("L").point(lambda p: 0 if p < 128 else 255, "1")

    img_eng.save(qr_path_engrave)
    print(f"[QR] Engrave QR saved at {qr_path_engrave}")

    return qr_path_display, qr_path_engrave

# === QR -> G-code functions ===
def qr_to_gcode_final(image_path, laser_power=255, travel_speed=5000, engrave_speed=1500, target_size_mm=25.0):
    """
    Vector-like approach: contour-following. Good for fewer G-lines but may produce complex paths.
    """
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

def qr_to_gcode_raster(img_path, laser_power=255, travel_speed=5000,
                       engrave_speed=1500, target_size_mm=20.0):
    """
    Raster engraving: line-by-line (zig-zag) scan producing many lines but simpler control.
    Produces denser G-code appropriate for raster engravers.
    """
    img = cv2.imread(img_path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ValueError(f"Cannot load image: {img_path}")

    # Binarize (black=0, white=255)
    _, bw = cv2.threshold(img, 127, 255, cv2.THRESH_BINARY)

    h, w = bw.shape
    px_per_mm = w / target_size_mm
    if px_per_mm == 0:
        raise ValueError("Invalid scale: px_per_mm == 0")
    mm_per_px = 1.0 / px_per_mm

    gcode = []
    gcode.append("G21 ; mm mode")
    gcode.append("G90 ; absolute positioning")
    gcode.append("M5  ; laser off")
    gcode.append(f"G0 F{travel_speed}")

    # iterate rows, zigzag pattern
    for row in range(h):
        y_mm = round(row * mm_per_px, 3)
        # choose forward/backwards scanning
        if row % 2 == 0:
            col_iter = range(w)
        else:
            col_iter = range(w-1, -1, -1)

        laser_on = False
        for col in col_iter:
            pixel = bw[row, col]
            x_mm = round(col * mm_per_px, 3)
            if pixel == 0:  # black pixel to engrave
                if not laser_on:
                    gcode.append(f"G0 X{x_mm} Y{y_mm} F{travel_speed}")
                    gcode.append(f"M3 S{laser_power}")
                    laser_on = True
                gcode.append(f"G1 X{x_mm} Y{y_mm} F{engrave_speed}")
            else:
                if laser_on:
                    gcode.append("M5")
                    laser_on = False

        if laser_on:
            gcode.append("M5")
            laser_on = False

    gcode.append("M5 ; ensure laser off")
    gcode.append("G0 X0 Y0 ; go home")
    return "\n".join(gcode)

def qr_to_gcode_fallback(image_path, laser_power=255, scale=1.0):
    # Simple horizontal-run fallback scanning
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

# === Send G-code to ESP32 over WebSocket ===
async def send_gcode_websocket(gcode_text, command_delay=0.02):
    try:
        async with websockets.connect(ESP32_WS) as websocket:
            # Optionally read an initial greeting from ESP32
            try:
                first_msg = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                print(f"ESP32 says: {first_msg}")
            except Exception:
                pass

            lines = [line.strip() for line in gcode_text.splitlines() if line.strip() and not line.lstrip().startswith(';')]
            total = len(lines)
            success_count = 0

            for i, line in enumerate(lines):
                await websocket.send(line)
                try:
                    ack = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                    if "ok" in ack.lower() or "ready" in ack.lower():
                        success_count += 1
                    else:
                        print(f"Unexpected ACK: {ack}")
                except asyncio.TimeoutError:
                    # no ack — we still proceed but log
                    print(f"No ACK for: {line[:80]}")
                if i % 100 == 0:
                    print(f"Progress: {i}/{total} lines sent")
                await asyncio.sleep(command_delay)

            rate = (success_count / total) * 100 if total else 100.0
            return rate > 90, f"Sent {success_count}/{total} ({rate:.1f}%)"
    except Exception as e:
        return False, f"WebSocket error: {e}"

def send_gcode_to_esp32_enhanced(gcode_text):
    """Wrapper so Flask can call the async WebSocket sender and returns (success_bool, message)."""
    try:
        return asyncio.run(send_gcode_websocket(gcode_text))
    except Exception as e:
        print(f"[send_gcode_to_esp32_enhanced] Exception: {e}")
        return False, f"Async send failed: {e}"

# === Flask routes (main app) ===
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

        inspection_date = None
        repair_date = None
        risk_level = "Low"
        vendor_risk = "Low"

        try:
            payload = {
                "uid": uid, "item_type": item_type, "vendor": vendor, "lot": lot,
                "supply_date": supply_date, "warranty_end": warranty_end, "notes": notes
            }
            risk_level = get_risk_level(payload)
            vendor_risk = calculate_vendor_risk(vendor)
        except Exception as e:
            print(f"[Risk Calculation] Exception: {e}")

        try:
            inspection_date, repair_date = calculate_dates(manufactor_date, supply_date, warranty_end, risk_level)
        except Exception as e:
            print(f"[Date Calculation] Exception: {e}")
            inspection_date = supply_date or datetime.today().strftime("%Y-%m-%d")
            repair_date = warranty_end or datetime.today().strftime("%Y-%m-%d")

        qr_content = generate_qr_content(
            uid, item_type, vendor, lot, supply_date, warranty_end,
            manufactor_date, manufactor_number, notes, risk_level, vendor_risk
        )

        # returns (display_path, engrave_path)
        qr_display_path, qr_engrave_path = save_qr_image(uid, qr_content)

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

        try:
            update_all_risks()
        except Exception as e:
            print(f"[Global Risk Update] Exception: {e}")

        # push to remote systems (best-effort)
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

@app.route('/all')
def view_all():
    sort_by = request.args.get('sort_by', 'uid')
    valid_columns = ['uid', 'lot', 'supply_date', 'warranty_end', 'manufactor_date', 'manufactor_number', 'vendor', 'risk', 'item_type', 'vendor_risk']
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

@app.route('/send_gcode/<uid>', methods=['POST'])
def send_gcode(uid):
    """
    send_gcode expects form data optionally containing:
      - method: 'raster' | 'vector' | 'fallback'  (default 'raster')
      - stream_delay: optional float seconds between lines (default 0.02)
    """
    method = request.form.get('method', 'raster').lower()
    try:
        command_delay = float(request.form.get('stream_delay', 0.02))
    except Exception:
        command_delay = 0.02

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

    # Generate both display + engrave QR; use the engrave one for g-code generation
    _, qr_path_engrave = save_qr_image(uid, qr_content)

    # Choose generator
    try:
        if method == 'vector':
            gcode_text = qr_to_gcode_final(qr_path_engrave, laser_power=255, travel_speed=5000, engrave_speed=1500, target_size_mm=20.0)
            print(f"[Vector] Generated {len(gcode_text.splitlines())} lines of G-code")
        elif method == 'fallback':
            gcode_text = qr_to_gcode_fallback(qr_path_engrave, laser_power=255, scale=0.5)
            print(f"[Fallback] Generated {len(gcode_text.splitlines())} lines of G-code")
        else:  # default raster
            gcode_text = qr_to_gcode_raster(qr_path_engrave, laser_power=255, travel_speed=5000, engrave_speed=1500, target_size_mm=20.0)
            print(f"[Raster] Generated {len(gcode_text.splitlines())} lines of G-code")
    except Exception as e:
        print(f"[G-code generation] Failed: {e}")
        return f"G-code generation failed: {e}", 500

    # Save G-code file
    gcode_path = os.path.join(qr_dir, f"{uid}_engrave.gcode")
    with open(gcode_path, "w") as f:
        f.write(gcode_text)
    print(f"[GCODE] Saved at {gcode_path}")

    # Stream/send to ESP32 via websocket
    success, resp_text = send_gcode_to_esp32_enhanced(gcode_text)
    msg = f"G-code sent successfully! {resp_text}" if success else f"Failed: {resp_text}"
    return redirect(url_for('view_record', uid=uid, msg=msg))

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
    save_qr_image(uid, qr_content)
    msg = f"QR regenerated for UID {uid}."
    return redirect(url_for('view_record', uid=uid, msg=msg))

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

    # Build QR content; embed minimal info for scanning template
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

@app.route('/test_qr/<uid>')
def test_qr(uid):
    """Return the display QR image for visual testing."""
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

    display_path, _ = save_qr_image(uid, qr_content)
    return send_file(display_path, mimetype='image/png')

# === Background threads ===
def periodic_risk_update():
    while True:
        try:
            update_all_risks()
        except Exception as e:
            print("[Risk Update] Exception:", e)
        time.sleep(3600)

def validate_all_qr_codes():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM fittings")
    rows = c.fetchall()
    for row in rows:
        row_dict = dict(row)
        uid = row_dict.get('uid')
        display, engrave = os.path.join(qr_dir, f"{uid}_display.png"), os.path.join(qr_dir, f"{uid}_engrave.png")
        qr_content = generate_qr_content(
            row_dict.get('uid'), row_dict.get('item_type'), row_dict.get('vendor'), row_dict.get('lot'),
            row_dict.get('supply_date'), row_dict.get('warranty_end'), row_dict.get('manufactor_date',''),
            row_dict.get('manufactor_number',''), row_dict.get('notes',''),
            row_dict.get('risk','Low'), row_dict.get('vendor_risk','Low')
        )
        try:
            if not os.path.exists(display) or not os.path.exists(engrave):
                save_qr_image(uid, qr_content)
                print(f"[QR Validation] Generated QR for UID {uid}")
        except Exception as e:
            print(f"[QR Validation] Error for UID {uid}: {e}")
    conn.close()
    print("[QR Validation] All QR codes checked.")

def retry_pending_sync():
    while True:
        conn = get_db_connection()
        c = conn.cursor()
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

# === Run app ===
if __name__ == '__main__':
    threading.Thread(target=periodic_risk_update, daemon=True).start()
    threading.Thread(target=validate_all_qr_codes, daemon=True).start()
    threading.Thread(target=retry_pending_sync, daemon=True).start()

    def open_browser():
        webbrowser.open_new("http://127.0.0.1:5000")
    threading.Timer(1.0, open_browser).start()

    app.run(debug=True, host="0.0.0.0")
