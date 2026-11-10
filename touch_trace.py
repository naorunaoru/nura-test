"""Append-only multitouch evidence, in raw and display coordinates."""
import json
import time
from actions import numbered_file


class TouchTrace:
    def __init__(self, directory, device, x_range, y_range, screen=(1440, 3168)):
        self.path = numbered_file(directory / 'logs', 'touchscreen', '.jsonl')
        self.file = self.path.open('a', buffering=1)
        self.ranges = (x_range, y_range)
        self.screen = screen
        self.count = 0
        self.started = time.monotonic_ns()
        self.write(dict(type='metadata', schema=1, device=device, screen=list(screen),
                        x_range=list(x_range), y_range=list(y_range),
                        origin='top-left', orientation='portrait', started_unix=time.time(),
                        started_monotonic_ns=self.started))

    def write(self, row):
        self.file.write(json.dumps(row, separators=(',', ':')) + '\n')

    def record(self, slot, point, phase, context, event_time=None):
        self.count += 1
        raw = [point['raw_x'], point['raw_y']]
        normalized = [(v-low)/(high-low) for v, (low, high) in zip(raw, self.ranges)]
        self.write(dict(type='touch', seq=self.count, elapsed_ns=time.monotonic_ns()-self.started,
                        event_time=event_time, slot=slot, tracking_id=point['tracking_id'],
                        phase=phase, raw=raw, normalized=normalized,
                        screen=[round(v*(size-1), 3) for v, size in zip(normalized, self.screen)],
                        **context))

    def dropped(self, context):
        self.write(dict(type='dropped', elapsed_ns=time.monotonic_ns()-self.started, **context))

    def close(self):
        self.file.close()
