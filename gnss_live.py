"""GNSS receiver state from the NMEA stream of the port's sdx55-gnss service."""
import os, select, termios, time
from pathlib import Path
from core import atomic_json

DEVICE = '/dev/gnss-sdx55'
TALKERS = {'GP': 'G', 'GL': 'R', 'GA': 'E', 'GB': 'C', 'BD': 'C', 'GQ': 'J', 'QZ': 'J', 'GI': 'I'}
GSA_SYSTEMS = {'1': 'G', '2': 'R', '3': 'E', '4': 'C', '5': 'J', '6': 'I'}
SILENT_AFTER = 5      # the receiver reports at 1 Hz
FIX_VALID = 3
SATELLITE_VALID = 5


def checksum_ok(line):
    body, star, digits = line[1:].rpartition('*')
    if not star:
        return False
    value = 0
    for char in body:
        value ^= ord(char)
    try:
        return int(digits, 16) == value
    except ValueError:
        return False


def degrees(value, hemisphere):
    """ddmm.mmmm with N/S/E/W to signed decimal degrees."""
    whole, minutes = divmod(float(value), 100)
    result = whole + minutes / 60
    return -result if hemisphere in ('S', 'W') else result


def number(value):
    return float(value) if value else None


def integer(value):
    return int(float(value)) if value else None


def identify(system, satellite):
    """Resolve NMEA's shared numbering to (system letter, number within it)."""
    if system in ('G', 'N'):      # GP also carries SBAS and QZSS; a GN GSA mixes systems
        if 33 <= satellite <= 64:
            return 'S', satellite + 87
        if 65 <= satellite <= 96:
            return 'R', satellite - 64
        if 193 <= satellite <= 202:
            return 'J', satellite - 192
        return 'G', satellite
    if system == 'R' and satellite >= 65:
        return 'R', satellite - 64
    return system, satellite


def utc_text(date, clock):
    if not clock or len(clock) < 6 or not clock[:6].isdigit():
        return None
    text = f'{clock[:2]}:{clock[2:4]}:{clock[4:6]}'
    if date and len(date) == 6 and date.isdigit():
        text = f'20{date[4:]}-{date[2:4]}-{date[:2]} ' + text
    return text


class Receiver:
    """Accumulates sentences; snapshot() is everything the card shows.

    Each fact carries the time it was last reported and expires on its own,
    so the result does not depend on the order of sentences within a second.
    """

    def __init__(self, now):
        self.started = now
        self.sentences = self.rejected = 0
        self.last_sentence = None
        self.satellites = {}
        self.used = {}
        self.fix_type = self.fix_type_time = None
        self.quality = 0
        self.quality_time = None
        self.position = None
        self.date = self.clock = None
        self.seconds_to_fix = None

    def feed(self, line, now):
        line = line.strip()
        if not line.startswith('$'):
            return
        if not checksum_ok(line):
            self.rejected += 1
            return
        fields = line[1:line.rindex('*')].split(',')
        talker, kind = fields[0][:2], fields[0][2:]
        self.sentences += 1
        self.last_sentence = now
        handler = {'GGA': self.gga, 'RMC': self.rmc, 'GSA': self.gsa, 'GSV': self.gsv}.get(kind)
        if handler:
            try:
                handler(talker, fields, now)
            except (ValueError, IndexError):
                self.rejected += 1

    def gga(self, talker, f, now):
        quality = integer(f[6]) or 0
        position = None
        if quality and f[2] and f[4]:
            position = dict(latitude=degrees(f[2], f[3]), longitude=degrees(f[4], f[5]),
                            altitude_m=number(f[9]), geoid_separation_m=number(f[11]),
                            hdop=number(f[8]), reported_satellites=integer(f[7]), time=now)
        # Nothing is taken from a sentence that did not parse as a whole.
        self.quality = quality if position else 0
        self.quality_time = now
        if f[1]:
            self.clock = f[1]
        if position:
            self.position = position
            if self.seconds_to_fix is None:
                self.seconds_to_fix = round(now - self.started, 1)

    def rmc(self, talker, f, now):
        if f[1]:
            self.clock = f[1]
        if f[9]:
            self.date = f[9]

    def gsa(self, talker, f, now):
        self.fix_type = integer(f[2])
        self.fix_type_time = now
        system = GSA_SYSTEMS.get(f[18]) if len(f) > 18 else None
        system = system or TALKERS.get(talker, 'N')
        keys = {identify(system, int(value)) for value in f[3:15] if value}
        if system == 'N':          # one GN sentence per constellation, told apart by its numbers
            system = min(keys)[0] if keys else None
        if system:
            self.used[system] = (keys, now)

    def gsv(self, talker, f, now):
        system = TALKERS.get(talker)
        if not system:
            return
        body = f[4:]
        if len(body) % 4 == 1:     # NMEA 4.1 signal ID
            body = body[:-1]
        for i in range(0, len(body) - 3, 4):
            if not body[i]:
                continue
            key = identify(system, int(body[i]))
            snr = integer(body[i + 3])
            old = self.satellites.get(key)
            # The same satellite is listed once per signal; keep its strongest.
            if old and now - old['time'] < .5 and (old['snr'] or 0) > (snr or 0):
                snr = old['snr']
            self.satellites[key] = dict(elevation=integer(body[i + 1]), azimuth=integer(body[i + 2]),
                                        snr=snr, time=now)

    def snapshot(self, now):
        silent = None if self.last_sentence is None else now - self.last_sentence
        if silent is None:
            status = 'waiting' if now - self.started < SILENT_AFTER else 'silent'
        elif silent > SILENT_AFTER:
            status = 'silent'
        elif self.quality and now - self.quality_time <= FIX_VALID:
            status = 'fix'
        else:
            status = 'searching'
        used = set()
        for keys, stamp in self.used.values():
            if now - stamp <= FIX_VALID:
                used |= keys
        self.satellites = {k: s for k, s in self.satellites.items() if now - s['time'] <= SATELLITE_VALID}
        # A satellite can be used in the fix without a signal report: the service
        # asks for GSV lists of GPS and GLONASS only.
        unlisted = dict.fromkeys(used - self.satellites.keys(), dict(elevation=None, azimuth=None, snr=None))
        satellites = [dict(label=f'{system}{slot:02d}', system=system, number=slot, elevation=s['elevation'],
                           azimuth=s['azimuth'], snr=s['snr'], used=(system, slot) in used)
                      for (system, slot), s in {**self.satellites, **unlisted}.items()]
        satellites.sort(key=lambda s: (not s['used'], -(s['snr'] or 0), s['system'], s['number']))
        position = self.position or {}
        fixed = status == 'fix'
        dimensions = self.fix_type_time is not None and now - self.fix_type_time <= FIX_VALID
        return {
            'kind': 'gnss', 'status': status,
            'fix_type': self.fix_type if fixed and dimensions else None,
            'quality': self.quality if fixed else 0,
            'latitude': position.get('latitude'), 'longitude': position.get('longitude'),
            'altitude_m': position.get('altitude_m'),
            'geoid_separation_m': position.get('geoid_separation_m'),
            'hdop': position.get('hdop'),
            'position_age': round(now - position['time'], 1) if position else None,
            'utc': utc_text(self.date, self.clock),
            'used': (len(used) or position.get('reported_satellites') or 0) if fixed else 0,
            'in_view': len(satellites), 'satellites': satellites,
            'silent_seconds': round(now - (self.started if silent is None else self.last_sentence), 1),
            'elapsed': round(now - self.started, 1), 'seconds_to_fix': self.seconds_to_fix,
            'sentences': self.sentences, 'rejected': self.rejected,
        }


def capture(runtime, log_path, device=DEVICE):
    """Follow the receiver until the stop file appears; publish state about twice a second."""
    runtime = Path(runtime)
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as error:
        raise RuntimeError(f'{device}: {error.strerror}. Is sdx55-gnss.service running?')
    try:
        # With no reader the pty keeps the oldest sentences and drops new ones.
        try:
            termios.tcflush(fd, termios.TCIFLUSH)
        except termios.error:
            pass
        receiver = Receiver(time.monotonic())
        pending = b''
        published = 0
        state = receiver.snapshot(receiver.started)
        atomic_json(runtime / 'scan.json', state)
        with open(log_path, 'ab') as log:
            while not (runtime / 'stop').exists():
                changed = False
                if select.select([fd], [], [], .25)[0]:
                    try:
                        data = os.read(fd, 4096)
                    except BlockingIOError:
                        data = None
                    except OSError:
                        data = b''
                    if data == b'':
                        raise RuntimeError('The NMEA stream closed. sdx55-gnss may have restarted; Retry.')
                    if data:
                        log.write(data)
                        log.flush()
                        *lines, pending = (pending + data).split(b'\n')
                        pending = pending[-512:]
                        now = time.monotonic()
                        for line in lines:
                            receiver.feed(line.decode('ascii', 'replace'), now)
                        changed = bool(lines)
                now = time.monotonic()
                if now - published >= (.5 if changed else 1):
                    state = receiver.snapshot(now)
                    atomic_json(runtime / 'scan.json', state)
                    published = now
        if not receiver.sentences:
            raise RuntimeError(f'No NMEA received from {device}.')
        state = receiver.snapshot(time.monotonic())
        return {**state, 'fix_seen': receiver.seconds_to_fix is not None}
    finally:
        os.close(fd)
