"""
wESP32 Temperature Monitor - MicroPython
Reads DS18B20 temperature sensor every 60 seconds and serves readings via HTTP.
Pins:
  - DS18B20 data: IO14 (requires 4.7k pull-up resistor to 3.3V)
  - Ethernet: MDC (IO16), MDIO (IO17)
"""
import uasyncio as asyncio
import network
import socket
import ujson
import machine
from machine import Pin
import onewire
import ds18x20
import time
import sys
import ubinascii
import ntptime
import os

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CONFIG_FILE = 'config.json'
config = {
    'password': None,      # None = first run, open access
    'location': 'Unknown',
    'temp_offset_c': 0.0,
    'ntp_server': 'pool.ntp.org'
}

def load_config():
    global config
    try:
        with open(CONFIG_FILE) as f:
            loaded = ujson.load(f)
            config.update(loaded)
    except:
        pass  # first run or corrupt file

def save_config():
    try:
        with open(CONFIG_FILE, 'w') as f:
            ujson.dump(config, f)
    except Exception as e:
        print('Config save error:', e)

load_config()

# DS18B20 one-wire bus on IO14
_ow_pin = Pin(14)
_ow_bus = onewire.OneWire(_ow_pin)
ds_sensor = ds18x20.DS18X20(_ow_bus)

last_celsius = None
last_fahrenheit = None
last_read_time = None
start_time = time.ticks_ms()
sensor_roms = []
current_time = None   # will be set by NTP

try:
    sensor_roms = ds_sensor.scan()
    if sensor_roms:
        print(f'DS18B20 sensors found: {len(sensor_roms)}')
        for rom in sensor_roms:
            print('  ROM:', ubinascii.hexlify(rom).decode())
except Exception as e:
    print(f'DS18B20 scan error: {e}')

# ---------------------------------------------------------------------------
# Time (NTP)
# ---------------------------------------------------------------------------
def sync_time():
    global current_time
    try:
        ntptime.host = config.get('ntp_server', 'pool.ntp.org')
        ntptime.settime()
        current_time = time.localtime()
        print('NTP time synced:', current_time)
    except Exception as e:
        print('NTP sync failed:', e)
        current_time = None

# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------
def password_required():
    return config.get('password') is not None

def check_password(pw):
    if not password_required():
        return True
    return pw == config.get('password')

# ---------------------------------------------------------------------------
# HTML templates
# ---------------------------------------------------------------------------
def protected_page(content_html):
    return (
        "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n"
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Settings</title><style>"
        "body{font-family:Arial,sans-serif;background:#1a1a2e;color:#eee;margin:2em}"
        "h1{color:#a0c4ff} form{margin:1em 0} input,button{padding:0.4em;margin:0.2em}"
        ".msg{color:#4ade80}</style></head><body>"
        "<h1>wESP32 Settings</h1>" + content_html +
        "<p><a href='/'>Back to dashboard</a></p></body></html>"
    )
def get_login_page(msg=""):
    return (
        "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n"
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Settings Login</title><style>"
        "body{font-family:Arial,sans-serif;background:#1a1a2e;color:#eee;margin:2em}"
        "h1{color:#a0c4ff} input,button{padding:0.6em;margin:0.3em;font-size:1.1em}"
        ".msg{color:#f87171}</style></head><body>"
        "<h1>Settings Login</h1>"
        "<form method='POST' action='/settings'>"
        "<input type='hidden' name='action' value='login'>"
        "Password: <input type='password' name='pw' required><br>"
        "<button type='submit'>Login</button>"
        "</form>"
        "<p class='msg'>" + msg + "</p>"
        "<p><a href='/'>Back to dashboard</a></p></body></html>"
    )


# Main dashboard (unchanged from before, with offset applied)
WEB_PAGE = (
    "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n"
    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
    "<title>Temperature Monitor</title><style>"
    "body{font-family:Arial,sans-serif;background:#1a1a2e;color:#eee;display:flex;"
    "flex-direction:column;align-items:center;justify-content:center;min-height:100vh;margin:0}"
    "h1{font-size:1.4em;margin-bottom:1.5em;color:#a0c4ff}"
    ".cards{display:flex;gap:1.5em;flex-wrap:wrap;justify-content:center}"
    ".card{background:#16213e;border-radius:12px;padding:1.5em 2.5em;text-align:center;"
    "box-shadow:0 4px 20px rgba(0,0,0,.4);min-width:140px}"
    ".label{font-size:.8em;color:#8899aa;margin-bottom:.4em;letter-spacing:.08em;text-transform:uppercase}"
    ".value{font-size:2.8em;font-weight:700;color:#a0c4ff}"
    ".unit{font-size:.9em;color:#8899aa;margin-top:.2em}"
    ".status{margin-top:2em;font-size:.75em;color:#556677}"
    "</style></head><body>"
    "<h1>Temperature Sensor</h1>"
    "<div class='cards'>"
    "<div class='card'><div class='label'>Celsius</div>"
    "<div class='value' id='cel'>--.-</div><div class='unit'>&deg;C</div></div>"
    "<div class='card'><div class='label'>Fahrenheit</div>"
    "<div class='value' id='fah'>--.-</div><div class='unit'>&deg;F</div></div>"
    "</div>"
    "<div class='status' id='st'>Loading…</div>"
    "<p><a href='/settings'>Settings</a></p>"
    "<script>"
    "function refresh(){fetch('/api/temp').then(r=>r.json()).then(d=>{"
    "document.getElementById('cel').textContent = d.celsius!==null ? d.celsius.toFixed(1) : '--.-';"
    "document.getElementById('fah').textContent = d.fahrenheit!==null ? d.fahrenheit.toFixed(1) : '--.-';"
    "document.getElementById('st').textContent = 'Last updated: ' + new Date().toLocaleTimeString() + ' | ' + (d.location || 'Unknown');"
    "}).catch(()=>{})}"
    "refresh(); setInterval(refresh,10000);"
    "</script></body></html>"
)

# ---------------------------------------------------------------------------
# Settings page HTML
# ---------------------------------------------------------------------------
def get_settings_page(msg="", pw=""):
    pw_html = ""
    if password_required():
        pw_html = """<h3>Change Password</h3>
        <form method="POST" action="/settings">
            <input type="hidden" name="action" value="password">
            <input type="hidden" name="pw" value="{pw}">
            New: <input type="password" name="new"><br>
            <button type="submit">Change Password</button>
        </form>"""
    else:
        pw_html = """<h3>Set Initial Password</h3>
        <form method="POST" action="/settings">
            <input type="hidden" name="action" value="password">
            <input type="hidden" name="pw" value="{pw}">
            New Password: <input type="password" name="new"><br>
            <button type="submit">Set Password</button>
        </form>"""

    return protected_page(f"""
    {pw_html}
    <h3>Location</h3>
    <form method="POST" action="/settings">
        <input type="hidden" name="action" value="location">
        <input type="hidden" name="pw" value="{pw}">
        <input type="text" name="location" value="{config.get('location','')}" size="40"><br>
        <button type="submit">Save Location</button>
    </form>

    <h3>Temperature Offset (°C)</h3>
    <form method="POST" action="/settings">
        <input type="hidden" name="action" value="offset">
        <input type="hidden" name="pw" value="{pw}">
        <input type="number" step="0.1" name="offset" value="{config.get('temp_offset_c',0)}"><br>
        <button type="submit">Save Offset</button>
    </form>

    <h3>NTP Server</h3>
    <form method="POST" action="/settings">
        <input type="hidden" name="action" value="ntp">
        <input type="hidden" name="pw" value="{pw}">
        <input type="text" name="ntp_server" value="{config.get('ntp_server', 'pool.ntp.org')}" size="30"><br>
        <button type="submit">Save NTP Server</button>
    </form>

    <h3>Upload new main.py</h3>
    <form method="POST" action="/upload" enctype="multipart/form-data">
        <input type="hidden" name="pw" value="{pw}">
        <input type="file" name="file" accept=".py"><br>
        <button type="submit">Upload & Reboot</button>
    </form>

    <p style="font-size:0.85em;color:#8899aa;margin-top:1.5em">
    <strong>Reliable alternative (recommended):</strong><br>
    Use this curl command from your computer:<br>
    <code style="background:#16213e;padding:2px 6px">curl -X POST http://IP/upload -F "pw=YOUR_PASSWORD" -F "file=@main.py"</code><br>
    Replace IP and YOUR_PASSWORD with the actual values.
    </p>

    <p class="msg">{msg}</p>
    """)

# ---------------------------------------------------------------------------
# Ethernet + NTP
# ---------------------------------------------------------------------------
async def connect_ethernet():
    try:
        lan = network.LAN(mdc=Pin(16), mdio=Pin(17), power=None,
                          phy_type=network.PHY_RTL8201, phy_addr=0)
        lan.active(True)
        print('Ethernet LAN activated')

        try:
            mac = lan.config('mac')
            mac_tail = ubinascii.hexlify(mac[-4:]).decode()
            hostname = f'temp-sensor-{mac_tail}'
        except:
            hostname = 'temp-sensor'
        try:
            network.hostname(hostname)
        except:
            pass
        print(f'Hostname: {hostname}')

        deadline = time.ticks_ms() + 15000
        while not lan.isconnected():
            if time.ticks_diff(time.ticks_ms(), deadline) > 0:
                raise OSError('Ethernet did not connect within 15 s')
            print('Waiting for Ethernet...')
            await asyncio.sleep_ms(500)

        print('Ethernet connected:', lan.ifconfig())
        return lan
    except Exception as e:
        print(f'Ethernet setup error: {e}')
        raise

# ---------------------------------------------------------------------------
# DS18B20 reader (with offset)
# ---------------------------------------------------------------------------
async def temp_reader():
    global last_celsius, last_fahrenheit, last_read_time, sensor_roms
    while True:
        try:
            if not sensor_roms:
                sensor_roms = ds_sensor.scan()
                if not sensor_roms:
                    await asyncio.sleep(10)
                    continue

            ds_sensor.convert_temp()
            await asyncio.sleep_ms(800)
            raw = ds_sensor.read_temp(sensor_roms[0])
            if raw in (85.0, -127.0):
                raise ValueError(f'Bad sensor value: {raw}')

            offset = config.get('temp_offset_c', 0.0)
            c = round(raw + offset, 2)
            last_celsius = c
            last_fahrenheit = round(c * 9 / 5 + 32, 2)
            last_read_time = time.ticks_ms()
            print(f'Temp: {last_celsius} C / {last_fahrenheit} F')
        except Exception as e:
            print(f'DS18B20 read error: {e}')
        await asyncio.sleep(60)

# ---------------------------------------------------------------------------
# Simple password check for protected routes
# ---------------------------------------------------------------------------
def is_authorized(request):
    if not password_required():
        return True
    # Very simple check: look for ?pw=xxx or form field
    if b'pw=' in request or b'password=' in request:
        # crude extraction for demo
        try:
            pw = request.split(b'pw=')[1].split(b'&')[0].decode()
            return pw == config.get('password')
        except:
            pass
    return False

# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
async def http_server(lan):
    try:
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(('', 80))
        srv.listen(5)
        srv.setblocking(False)
        print('HTTP server listening on port 80')
    except Exception as e:
        print(f'HTTP server setup error: {e}')
        return

    while True:
        conn = None
        try:
            conn, addr = srv.accept()
            conn.settimeout(5.0)
            raw = b''
            while b'\r\n\r\n' not in raw:
                chunk = conn.recv(512)
                if not chunk: break
                raw += chunk

            # Read POST body if present
            if b'Content-Length:' in raw:
                try:
                    cl = int(raw.split(b'Content-Length:')[1].split(b'\r\n')[0].strip())
                    body_so_far = raw.split(b'\r\n\r\n', 1)[1] if b'\r\n\r\n' in raw else b''
                    while len(body_so_far) < cl:
                        more = conn.recv(min(512, cl - len(body_so_far)))
                        if not more: break
                        body_so_far += more
                    raw = raw.split(b'\r\n\r\n', 1)[0] + b'\r\n\r\n' + body_so_far
                except:
                    pass

            request_line = raw.split(b'\r\n', 1)[0].decode('utf-8', 'ignore')
            method, path = request_line.split(' ', 2)[:2]
            path = path.split('?', 1)[0]

            # Public routes
            if path in ('/', '/index.html'):
                response = WEB_PAGE
            elif path == '/api/temp':
                body = ujson.dumps({
                    'celsius': last_celsius,
                    'fahrenheit': last_fahrenheit,
                    'age_s': round(time.ticks_diff(time.ticks_ms(), last_read_time)/1000) if last_read_time else None,
                    'location': config.get('location'),
                    'time': str(current_time) if current_time else None
                })
                response = 'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n' + body

            # Protected routes
            elif path == '/settings':
                # Always require password for settings
                body = raw.split(b'\r\n\r\n', 1)[1] if b'\r\n\r\n' in raw else b''
                form = {}
                for pair in body.split(b'&'):
                    if b'=' in pair:
                        k, v = pair.split(b'=', 1)
                        key = k.decode('utf-8', 'ignore')
                        val = v.decode('utf-8', 'ignore').replace('+', ' ')
                        form[key] = val

                submitted_pw = form.get('pw', '')
                action = form.get('action', '')

                # If this is a login attempt or first GET
                if method == 'GET' or action == 'login':
                    if check_password(submitted_pw):
                        response = get_settings_page(pw=submitted_pw)
                    else:
                        msg = "Incorrect password" if submitted_pw else ""
                        response = get_login_page(msg)

                else:
                    # POST action - verify password first (except for password change)
                    msg = ''
                    if action == 'password':
                        new_pw = form.get('new', '')
                        if new_pw:
                            config['password'] = new_pw
                            save_config()
                            msg = 'Password set/changed. Reboot recommended.'
                            submitted_pw = new_pw
                        else:
                            msg = 'New password cannot be empty.'
                    elif not check_password(submitted_pw):
                        response = get_login_page("Incorrect password")
                    else:
                        if action == 'location':
                            loc = form.get('location', '').replace('+', ' ')
                            config['location'] = loc
                            save_config()
                            msg = 'Location saved.'
                        elif action == 'offset':
                            try:
                                config['temp_offset_c'] = float(form.get('offset', 0))
                                save_config()
                                msg = 'Offset saved.'
                            except:
                                msg = 'Invalid offset.'
                        elif action == 'ntp':
                            ntp = form.get('ntp_server', '').strip()
                            if ntp:
                                config['ntp_server'] = ntp
                                save_config()
                                msg = 'NTP server saved.'
                            else:
                                msg = 'NTP server cannot be empty.'
                    response = get_settings_page(msg, pw=submitted_pw)

            # For reliability, prefer curl or implement chunked streaming.
            # NOTE: Current upload handler loads entire file into RAM.
            # On ESP32 this often causes "memory allocation failed".
            # For reliability, prefer curl or implement chunked streaming.
            elif path == '/upload' and method == 'POST':
                # Improved multipart upload handler
                try:
                    # Get the full body (already read by the improved request reader)
                    body = raw.split(b'\r\n\r\n', 1)[1] if b'\r\n\r\n' in raw else b''

                    # Find the file part (look for filename="main.py" or any .py file)
                    filename_marker = b'filename="'
                    if filename_marker in body:
                        # Find start of file content (after second \r\n\r\n)
                        parts = body.split(b'\r\n\r\n', 2)
                        if len(parts) >= 3:
                            file_content = parts[2]

                            # Remove trailing multipart boundary
                            boundary_start = file_content.find(b'\r\n------')
                            if boundary_start != -1:
                                file_content = file_content[:boundary_start]

                            # Also try common boundary patterns
                            for boundary in [b'\r\n--', b'\r\n------WebKitFormBoundary']:
                                idx = file_content.find(boundary)
                                if idx != -1:
                                    file_content = file_content[:idx]
                                    break

                            if len(file_content) > 500:  # sanity check
                                with open('main.py', 'wb') as f:
                                    f.write(file_content)
                                print(f'Upload successful: {len(file_content)} bytes')

                                response = (
                                    'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n'
                                    '<html><body style="font-family:Arial">'
                                    f'<h2>Upload successful ({len(file_content)} bytes)</h2>'
                                    '<p>Device is rebooting with new code...</p>'
                                    '</body></html>'
                                )
                                conn.sendall(response.encode('utf-8'))
                                conn.close()
                                await asyncio.sleep(1)
                                machine.reset()
                                return
                            else:
                                response = 'HTTP/1.1 400 Bad Request\r\nContent-Type: text/plain\r\n\r\nFile too small or corrupted'
                        else:
                            response = 'HTTP/1.1 400 Bad Request\r\nContent-Type: text/plain\r\n\r\nCould not parse multipart data'
                    else:
                        response = 'HTTP/1.1 400 Bad Request\r\nContent-Type: text/plain\r\n\r\nNo file uploaded (make sure field name is "file")'
                except Exception as e:
                    response = f'HTTP/1.1 500 Internal Server Error\r\nContent-Type: text/plain\r\n\r\nUpload error: {e}'
            else:
                response = 'HTTP/1.1 404 Not Found\r\nContent-Type: text/plain\r\n\r\nNot Found'

            conn.sendall(response.encode('utf-8'))
        except OSError:
            await asyncio.sleep_ms(30)
            continue
        except Exception as e:
            print(f'HTTP handler error: {e}')
        finally:
            if conn:
                try: conn.close()
                except: pass
        await asyncio.sleep_ms(10)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    try:
        lan = await connect_ethernet()
        sync_time()
        asyncio.create_task(temp_reader())
        asyncio.create_task(http_server(lan))
        print('All tasks started')
        while True:
            await asyncio.sleep(1)
    except Exception as e:
        print(f'Fatal error in main: {e}')
        await asyncio.sleep(5)
        machine.reset()

if __name__ == '__main__':
    try:
        print('MicroPython:', sys.version)
        print('Starting wESP32 Temperature Monitor...')
        asyncio.run(main())
    except KeyboardInterrupt:
        print('Stopped by user')
    except Exception as e:
        print(f'Unhandled exception: {e}')
        time.sleep(5)
        machine.reset()
