"""GNSS receiver card: the fix state first, then position, time and satellite signals."""
FG = '#e9eff8'; MUTED = '#9aa9bf'; GREEN = '#80e5bd'; GOLD = '#ffd077'; RED = '#ff9b9b'
TRACKED = '#5d7896'
ROWS = 16            # most per column; two columns
FULL_SCALE = 50      # dB-Hz that fills a signal bar


def clock(seconds):
    minutes, seconds = divmod(int(seconds), 60)
    return f'{minutes}:{seconds:02d}'


def headline(state, running, error=''):
    """(label, colour, detail): the working state in one banner."""
    if error:
        return 'RECEIVER ERROR', RED, 'See the message below, then Retry.'
    status = state.get('status')
    if status is None or status == 'waiting':
        return 'STARTING', MUTED, 'Waiting for the first sentence…'
    if status == 'silent':
        return 'NO DATA', RED, f"Receiver silent for {clock(state['silent_seconds'])}"
    strongest = max((s['snr'] or 0 for s in state['satellites']), default=0)
    if status == 'fix':
        label = f"{state['fix_type']}D FIX" if state.get('fix_type') in (2, 3) else 'FIX'
        detail = f"{state['used']} satellites used"
        if state.get('hdop') is not None:
            detail += f" · HDOP {state['hdop']:.1f}"
        color = GREEN
    else:
        label = 'NO FIX'; color = GOLD
        if state.get('position_age') is not None:
            detail = f"Lost {clock(state['position_age'])} ago"
        else:
            detail = f"Searching {clock(state['elapsed'])}"
        detail += f" · {state['in_view']} in view"
        if strongest:
            detail += f' · best {strongest} dB-Hz'
    if not running:
        detail = 'Stopped · ' + detail
    return label, color, detail


def position_text(state):
    latitude, longitude = state.get('latitude'), state.get('longitude')
    if latitude is None or longitude is None:
        return '—'
    return (f"{abs(latitude):.6f}° {'N' if latitude >= 0 else 'S'}   "
            f"{abs(longitude):.6f}° {'E' if longitude >= 0 else 'W'}")


def draw(renderer, draw, state, running, error=''):
    text = renderer.text
    label, color, detail = headline(state, running, error)
    draw.rounded_rectangle((16, 152, 464, 238), radius=9, fill='#131c28', outline=color, width=2)
    draw.rounded_rectangle((16, 152, 30, 238), radius=9, fill=color)
    text(draw, label, (46, 160), 32, color, width=400, max_lines=1, bold=True)
    text(draw, detail, (46, 204), 17, FG, width=400, max_lines=1)

    # Last known values stay visible, dimmed, when there is no current fix.
    value = FG if state.get('status') == 'fix' and not error else MUTED
    altitude = state.get('altitude_m')
    text(draw, 'POSITION', (24, 252), 13, MUTED, max_lines=1)
    text(draw, position_text(state), (24, 270), 24, value, max_lines=1, bold=True)
    text(draw, 'ALTITUDE (MSL)', (24, 312), 13, MUTED, max_lines=1)
    text(draw, '—' if altitude is None else f'{altitude:.1f} m', (24, 330), 24, value, width=150, max_lines=1, bold=True)
    text(draw, 'TIME (UTC)', (190, 312), 13, MUTED, max_lines=1)
    text(draw, state.get('utc') or '—', (190, 330), 24, value, width=266, max_lines=1, bold=True)

    satellites = [] if error else state.get('satellites', [])     # no stale bars under an error
    used = sum(s['used'] for s in satellites)
    text(draw, f"SATELLITES · {used} used of {len(satellites)} in view · elevation, dB-Hz",
         (24, 374), 13, MUTED, max_lines=1)
    if not satellites:
        text(draw, 'No satellites reported', (24, 400), 19, MUTED, max_lines=1)
    rows = min(ROWS, max(8, (len(satellites) + 1) // 2))     # strongest half on the left
    for i, satellite in enumerate(satellites[:2 * rows]):
        x = 24 + (i // rows) * 222; y = 398 + (i % rows) * 24
        used = satellite['used']; snr = satellite['snr']
        text(draw, satellite['label'], (x, y), 15, FG if used else MUTED, width=44, max_lines=1, bold=used)
        if satellite['elevation'] is not None:
            text(draw, f"{satellite['elevation']}°", (x + 44, y + 2), 13, MUTED, width=32, max_lines=1)
        if used and snr is None and satellite['elevation'] is None:
            text(draw, 'no signal data', (x + 80, y + 2), 13, MUTED, width=120, max_lines=1)
            continue
        draw.rectangle((x + 80, y + 4, x + 176, y + 15), fill='#1b2635')
        if snr:
            draw.rectangle((x + 80, y + 4, x + 80 + round(96 * min(snr, FULL_SCALE) / FULL_SCALE), y + 15),
                           fill=GREEN if used else TRACKED)
        if snr is not None:
            text(draw, str(snr), (x + 184, y), 15, FG if snr else MUTED, width=28, max_lines=1)

    footer = f"{state.get('sentences', 0)} sentences"
    if state.get('rejected'):
        footer += f" · {state['rejected']} rejected (is another program reading the receiver?)"
    if len(satellites) > 2 * ROWS:
        footer += f' · {len(satellites) - 2 * ROWS} weaker not shown'
    text(draw, footer, (24, 790), 13, GOLD if state.get('rejected') else MUTED, max_lines=1)
