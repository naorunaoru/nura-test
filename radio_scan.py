"""Convert radio discovery output into device rows, keeping raw logs separately."""
import re

ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
DEVICE = re.compile(r'\[(NEW|CHG|DEL)\] Device ([0-9A-Fa-f:]{17}) (.*)')


class BluetoothDevices:
    def __init__(self):
        self.devices = {}
        self.scanning = False

    def feed(self, line):
        line = ANSI.sub('', line).strip()
        if 'Discovery started' in line:
            self.scanning = True
        if 'Failed to start discovery' in line or 'Failed to set discovery filter' in line or 'No default controller available' in line:
            raise RuntimeError(line)
        match = DEVICE.search(line)
        if not match:
            return
        event, address, value = match.groups()
        address = address.upper()
        if event == 'DEL':
            # Keep evidence of devices already seen during this scan.
            return
        item = self.devices.setdefault(address, {'address': address, 'name': '', 'seen': False})
        if event == 'NEW':
            item['name'] = self.name(value, address)
            if self.scanning:
                item['seen'] = True
        elif value.startswith(('Name: ', 'Alias: ')):
            item['name'] = self.name(value.split(': ', 1)[1], address)
        elif value.startswith('RSSI: '):
            # BlueZ versions print either -61 or 0xffffffc3 (-61).
            signal = re.search(r'\((-?\d+)\)', value) or re.search(r'RSSI: (-?\d+)\s*$', value)
            if signal:
                item['rssi_dbm'] = int(signal.group(1))
                if self.scanning:
                    item['seen'] = True
        elif self.scanning and value.startswith(('ManufacturerData.', 'ServiceData.', 'TxPower: ')):
            item['seen'] = True

    def rows(self):
        return sorted(({k: v for k, v in d.items() if k != 'seen'}
                       for d in self.devices.values() if d['seen']),
                      key=lambda d: (not bool(d['name']), d['name'].casefold(), d['address']))

    @staticmethod
    def name(value, address):
        return '' if value.upper().replace('-', ':') == address or value == '<unknown>' else value


def split_nmcli(line):
    fields = ['']
    escaped = False
    for char in line:
        if escaped:
            fields[-1] += char
            escaped = False
        elif char == '\\':
            escaped = True
        elif char == ':':
            fields.append('')
        else:
            fields[-1] += char
    if escaped:
        fields[-1] += '\\'
    return fields


def wifi_rows(output):
    devices = {}
    for line in output.splitlines():
        fields = split_nmcli(line)
        if len(fields) != 6:
            continue
        address, ssid, channel, signal, security, active = fields
        try:
            row = dict(address=address, name=ssid, channel=int(channel),
                       signal_percent=int(signal), security=security or 'Open', connected=active == '*')
        except ValueError:
            continue
        devices[address] = row
    return sorted(devices.values(), key=lambda d: (-d['signal_percent'], d['address']))
