"""
wESP32 Temperature Monitor - MicroPython
Reads DS18B20 temperature sensor every 60 seconds and serves readings via HTTP.

Pins:
  - DS18B20 data: IO14 (requires 4.7k pull-up resistor to 3.3V)
  - Ethernet: MDC (IO16), MDIO (IO17)

New endpoints:
  GET  /              - Dashboard (existing nice HTML)
  GET  /help          - List of all routes
  GET  /api/temp      - JSON with temp + location
  GET  /health        - Health check
  POST /set_location  - Set location (?loc=Office or form field)
  POST /update?pw=xxx - Upload new main.py (password = last 8 chars of MAC, lowercase)
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

# DS18B20 one-wire bus on IO14
_ow_pin = Pin(14)
_ow_bus = onewire.OneWire(_ow_pin)
ds_sensor = ds18x20.DS18X20(_ow_bus)

# In-memory temperature state — None until first successful read
last_celsius = None
last_fahrenheit = None
last_read_time = None  # ticks_ms() of last successful read

# Uptime reference
start_time = time.ticks_ms()

# Location (persisted to location.txt)
location = "Unknown"

def load_location():
    global location
    try:
        with open('location.txt', 'r') as f:
            loc = f.read().strip()
            if loc:
                location = loc
    except:
        pass

def save_location(new_loc):
    global location
    location = new_loc
    try:
        with open('location.txt', 'w') as f:
            f.write(new_loc)
    except Exception as e:
        print(f'Failed to save location: {e}')

# Device password = last 8 chars of MAC address (lowercase) - set after Ethernet connects
device_password = None

# Scan for sensor ROMs at startup (re-scanned in loop if empty)
sensor_roms = []
try:
    sensor_roms = ds_sensor.scan()
    if sensor_roms:
        print(f'DS18B20 sensors found: {len(sensor_roms)}')
        for rom in sensor_roms:
            print('  ROM:', ubinascii.hexlify(rom).decode())
    else:
        print('DS18B20: no sensors found at startup (will retry)')
except Exception as e:
    print(f'DS18B20 scan error: {e}')

load_location()


# ---------------------------------------------------------------------------
# HTML page — served at /
# ---------------------------------------------------------------------------
WEB_PAGE = (
    "HTTP/1.1 200 OK\r\n"
    "Content-Type: text/html\r\n"
    "\r\n"
    "<!DOCTYPE html>"
    "<html>"
    "<head>"
    "<meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
    "<title>Temperature Monitor</title>"
    "<style>"
    "body{font-family:Arial,sans-serif;background:#1a1a2e;color:#eee;display:flex;"
    "flex-direction:column;align-items:center;justify-content:center;min-height:100vh;margin:0}"
    "h1{font-size:1.4em;margin-bottom:1.5em;letter-spacing:.05em;color:#a0c4ff}"
    ".cards{display:flex;gap:1.5em;flex-wrap:wrap;justify-content:center}"
    ".card{background:#16213e;border-radius:12px;padding:1.5em 2.5em;text-align:center;"
    "box-shadow:0 4px 20px rgba(0,0,0,.4);min-width:140px}"
    ".label{font-size:.8em;color:#8899aa;margin-bottom:.4em;letter-spacing:.08em;text-transform:uppercase}"
    ".value{font-size:2.8em;font-weight:700;color:#a0c4ff}"
    ".unit{font-size:.9em;color:#8899aa;margin-top:.2em}"
    ".status{margin-top:2em;font-size:.75em;color:#556677}"
    "</style>"
    "</head>"
    "<body>"
    "<h1>wESP32 Temperature Monitor</h1>"
    "<div class='cards'>"
    "<div class='card'>"
    "<div class='label'>Celsius</div>"
    "<div class='value' id='cel'>--.-</div>"
    "<div class='unit'>&deg;C</div>"
    "</div>"
    "<div class='card'>"
    "<div class='label'>Fahrenheit</div>"
    "<div class='value' id='fah'>--.-</div>"
    "<div class='unit'>&deg;F</div>"
    "</div>"
    "</div>"
    "<div class='status' id='st'>Loading&hellip;</div>"
    "<script>"
    "function refresh(){"
    "fetch('/api/temp')"
    ".then(r=>r.json())"
    ".then(d=>{"
    "document.getElementById('cel').textContent="
    "d.celsius!==null?d.celsius.toFixed(1):'--.-';"
    "document.getElementById('fah').textContent="
    "d.fahrenheit!==null?d.fahrenheit.toFixed(1):'--.-';"
    "document.getElementById('st').textContent="
    "'Last updated: '+new Date().toLocaleTimeString()+' | Location: '+(d.location||'Unknown');"
    "})"
    ".catch(()=>{"
    "document.getElementById('st').textContent='Error fetching data';"
    "});}"
    "refresh();"
    "setInterval(refresh,10000);"
    "</script>"
    "</body>"
    "</html>"
)


# ---------------------------------------------------------------------------
# Ethernet
# ---------------------------------------------------------------------------
async def connect_ethernet():
    """Bring up the wESP32 Ethernet interface and wait for a DHCP lease."""
    global device_password
    try:
        lan = network.LAN(
            mdc=machine.Pin(16),
            mdio=machine.Pin(17),
            power=None,
            phy_type=network.PHY_RTL8201,
            phy_addr=0
        )

        lan.active(True)
        print('Ethernet LAN activated')

        # Derive hostname from MAC tail
        try:
            mac = lan.config('mac')
            mac_tail = ubinascii.hexlify(mac[-4:]).decode()
            hostname = f'temp-sensor-{mac_tail}'

            # Set device password = last 8 characters of full MAC, lowercase
            mac_full = ubinascii.hexlify(mac).decode().lower()
            device_password = mac_full[-8:]
            print(f'Device password (last 8 MAC chars): {device_password}')
        except Exception as e:
            print(f'MAC read error: {e}')
            hostname = 'temp-sensor'

        try:
            network.hostname(hostname)
        except AttributeError:
            try:
                lan.config(dhcp_hostname=hostname)
            except ValueError:
                lan.config(hostname=hostname)

        print(f'Hostname: {hostname}')

        timeout_ms = time.ticks_ms() + 15000
        while not lan.isconnected() and time.ticks_diff(time.ticks_ms(), timeout_ms) < 0:
            print('Waiting for Ethernet...')
            await asyncio.sleep_ms(500)

        if not lan.isconnected():
            raise OSError('Ethernet did not connect within 15 s')

        print('Ethernet connected:', lan.ifconfig())
        return lan

    except Exception as e:
        print(f'Ethernet setup error: {e}')
        raise


# ---------------------------------------------------------------------------
# DS18B20 reader task
# ---------------------------------------------------------------------------
async def temp_reader():
    """Read DS18B20 every 60 seconds; store result in module-level globals."""
    global last_celsius, last_fahrenheit, last_read_time, sensor_roms

    while True:
        try:
            # Re-scan if we have no known ROMs
            if not sensor_roms:
                sensor_roms = ds_sensor.scan()
                if not sensor_roms:
                    print('DS18B20: no sensors detected, retrying in 10 s')
                    await asyncio.sleep(10)
                    continue
                print(f'DS18B20: found {len(sensor_roms)} sensor(s) after rescan')

            # Trigger temperature conversion on all sensors (~750 ms for 12-bit)
            ds_sensor.convert_temp()
            await asyncio.sleep_ms(800)

            # Read first sensor (most deployments have exactly one)
            raw = ds_sensor.read_temp(sensor_roms[0])

            # DS18X20 returns 85.0 on power-on fault; treat as error
            if raw == 85.0 or raw == -127.0:
                raise ValueError(f'DS18B20 returned sentinel value: {raw}')

            last_celsius = round(raw, 2)
            last_fahrenheit = round(raw * 9 / 5 + 32, 2)
            last_read_time = time.ticks_ms()
            print(f'Temp: {last_celsius} C  /  {last_fahrenheit} F  | Location: {location}')

        except Exception as e:
            print(f'DS18B20 read error: {e}')

        await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
async def http_server(lan):
    """Serve the web UI, /api/temp, /health, /help, /set_location, /update on port 80."""
    global location
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

            # Read until end-of-headers
            raw = b''
            while b'\r\n\r\n' not in raw:
                chunk = conn.recv(512)
                if not chunk:
                    break
                raw += chunk

            request_line = raw.split(b'\r\n', 1)[0].decode('utf-8', 'ignore')
            method, path = request_line.split(' ', 2)[:2]
            path = path.split('?', 1)[0]

            # Parse query string for password and location
            query = ''
            if '?' in request_line:
                query = request_line.split('?', 1)[1].split(' ', 1)[0]

            # Route
            if path == '/' or path == '/index.html':
                response = WEB_PAGE

            elif path == '/help':
                help_text = (
                    "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n"
                    "wESP32 Temperature Monitor - Available Routes\n\n"
                    "GET  /              Dashboard (HTML with auto-refresh)\n"
                    "GET  /help          This help text\n"
                    "GET  /api/temp      JSON: celsius, fahrenheit, age_s, location\n"
                    "GET  /health        JSON health + uptime + IP\n"
                    "POST /set_location  Set location (?loc=YourLocation or form field 'loc')\n"
                    "POST /update?pw=xxx Upload new main.py (pw = last 8 MAC chars, lowercase)\n"
                )
                response = help_text

            elif path == '/api/temp':
                body = ujson.dumps({
                    'celsius': last_celsius,
                    'fahrenheit': last_fahrenheit,
                    'age_s': (
                        round(time.ticks_diff(time.ticks_ms(), last_read_time) / 1000)
                        if last_read_time is not None else None
                    ),
                    'location': location
                })
                response = (
                    'HTTP/1.1 200 OK\r\n'
                    'Content-Type: application/json\r\n'
                    '\r\n' + body
                )

            elif path == '/health':
                uptime = round(time.ticks_diff(time.ticks_ms(), start_time) / 1000)
                body = ujson.dumps({
                    'status': 'ok',
                    'uptime_s': uptime,
                    'ip': lan.ifconfig()[0],
                    'sensor_ok': last_celsius is not None,
                    'location': location
                })
                response = (
                    'HTTP/1.1 200 OK\r\n'
                    'Content-Type: application/json\r\n'
                    '\r\n' + body
                )

            elif path == '/set_location':
                # Try to get location from query string first
                new_loc = None
                if 'loc=' in query:
                    new_loc = query.split('loc=', 1)[1].split('&', 1)[0]
                else:
                    # Try to read from body (form post)
                    if b'loc=' in raw:
                        body_start = raw.find(b'\r\n\r\n') + 4
                        body = raw[body_start:].decode('utf-8', 'ignore')
                        if 'loc=' in body:
                            new_loc = body.split('loc=', 1)[1].split('&', 1)[0]

                if new_loc:
                    new_loc = new_loc.replace('+', ' ').replace('%20', ' ')
                    save_location(new_loc)
                    print(f'Location updated to: {location}')
                    response = (
                        'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n'
                        '<html><body style="font-family:Arial">'
                        f'<h2>Location updated to: {location}</h2>'
                        '<a href="/">Back to dashboard</a>'
                        '</body></html>'
                    )
                else:
                    # Show form
                    response = (
                        'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n'
                        '<html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
                        '<style>body{font-family:Arial,sans-serif;margin:40px}input,button{padding:10px;font-size:1.1em}</style>'
                        '</head><body>'
                        '<h2>Set Device Location</h2>'
                        '<form method="POST" action="/set_location">'
                        '<input type="text" name="loc" placeholder="e.g. Server Room, Floor 2" style="width:300px"> '
                        '<button type="submit">Save Location</button>'
                        '</form>'
                        '<p><a href="/">Back to dashboard</a></p>'
                        '</body></html>'
                    )

            elif path == '/update':
                # Password check
                pw = None
                if 'pw=' in query:
                    pw = query.split('pw=', 1)[1].split('&', 1)[0].lower()

                if device_password is None or pw != device_password:
                    response = 'HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\n\r\nInvalid or missing password'
                else:
                    # Read body (everything after headers)
                    body_start = raw.find(b'\r\n\r\n') + 4
                    new_code = raw[body_start:]

                    if new_code:
                        try:
                            with open('main.py', 'wb') as f:
                                f.write(new_code)
                            print(f'Update received ({len(new_code)} bytes). Rebooting...')
                            response = 'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nUpdate successful. Rebooting...'
                            conn.sendall(response.encode('utf-8'))
                            conn.close()
                            await asyncio.sleep(1)
                            machine.reset()
                            return  # won't reach here
                        except Exception as e:
                            response = f'HTTP/1.1 500 Internal Server Error\r\nContent-Type: text/plain\r\n\r\nWrite failed: {e}'
                    else:
                        response = 'HTTP/1.1 400 Bad Request\r\nContent-Type: text/plain\r\n\r\nNo file content received'

            else:
                response = 'HTTP/1.1 404 Not Found\r\nContent-Type: text/plain\r\n\r\nNot Found'

            conn.sendall(response.encode('utf-8'))

        except OSError:
            await asyncio.sleep_ms(50)
            continue
        except Exception as e:
            print(f'HTTP handler error: {e}')
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

        await asyncio.sleep_ms(0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def main():
    """Initialise hardware, start background tasks, run forever."""
    try:
        lan = await connect_ethernet()
        asyncio.create_task(temp_reader())
        asyncio.create_task(http_server(lan))
        print('All tasks started')
        while True:
            await asyncio.sleep(1)
    except Exception as e:
        print(f'Fatal error in main: {e}')
        print('Restarting in 5 seconds...')
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
