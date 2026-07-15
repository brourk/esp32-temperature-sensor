# This file is executed on every boot (including wake-boot from deepsleep)
import machine
import network
import ubinascii
import ujson

def _get_mdns_hostname(lan):
    """Return custom hostname from config.json if present, else MAC-derived default."""
    try:
        with open('config.json') as f:
            cfg = ujson.load(f)
            custom = cfg.get('mdns_hostname')
            if custom and isinstance(custom, str) and custom.strip():
                return custom.strip()
    except:
        pass
    # Default: temp-sensor + last 4 bytes of MAC
    try:
        mac = lan.config('mac')
        mac_tail = ubinascii.hexlify(mac[-4:]).decode()
        return f'temp-sensor-{mac_tail}'
    except:
        return 'temp-sensor'

# Connect to LAN
lan = network.LAN(mdc = machine.Pin(16), mdio = machine.Pin(17),
                  power = None, phy_type = network.PHY_RTL8201, phy_addr=0)

# Set mDNS hostname BEFORE activating the interface (per MicroPython docs)
if machine.reset_cause() != machine.SOFT_RESET:
    hostname = _get_mdns_hostname(lan)
    try:
        network.hostname(hostname)
    except:
        pass
    print('Hostname set before LAN activate:', hostname)
    lan.active(True)

# Define convenient reset function
def reset():
  machine.reset()
