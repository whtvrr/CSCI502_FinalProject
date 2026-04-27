#!/usr/bin/env python3
"""
AL5B Robotic Arm Controller
============================
Connection: RPi -> USB-RS232 -> SSC-32 (DB9)
Port: /dev/ttyUSB1

Threads:
  ServoThread     - moves servos
  PlaybackThread  - plays back recorded movements
  StatusThread    - draws status panel in terminal

Movement keys:
  A/D  -- base           W/S  -- shoulder
  R/F  -- elbow          T/G  -- wrist
  Q/E  -- gripper

Commands:
  1        -- recording on/off
  2        -- playback (loop)
  3        -- stop playback
  0        -- smooth return home
  +/-      -- speed
  Space    -- emergency stop
  5/6/7    -- go to preset 1/2/3
  8/9/K    -- save preset 1/2/3
  Esc      -- exit
"""

import serial
import threading
import time
import logging
import sys
import tty
import termios
import select

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

PORT     = "/dev/ttyUSB1"
BAUDRATE = 115200

BASE     = 0
SHOULDER = 1
ELBOW    = 2
WRIST    = 3
GRIPPER  = 4

SERVO_NAMES = {
    BASE:     "Base   ",
    SHOULDER: "Shoulder",
    ELBOW:    "Elbow  ",
    WRIST:    "Wrist  ",
    GRIPPER:  "Gripper",
}

HOME = {BASE: 1500, SHOULDER: 1700, ELBOW: 1500, WRIST: 1500, GRIPPER: 1500}

LIMITS = {
    BASE:     (600,  2400),
    SHOULDER: (800,  2200),
    ELBOW:    (800,  2200),
    WRIST:    (700,  2300),
    GRIPPER:  (900,  2100),
}

STEP     = 30
STEP_MIN = 5
STEP_MAX = 80
STEP_INC = 5
HZ       = 50

# Smooth HOME: interpolation steps
HOME_STEPS    = 30    # number of steps
HOME_STEP_MS  = 40    # ms between steps

pos      = dict(HOME)
pos_lock = threading.Lock()

frames    = []
recording = False
playing   = False
estop     = False
going_home = False   # smooth movement home

# Presets: 3 slots, initially None
presets = {1: None, 2: None, 3: None}

held      = set()
held_lock = threading.Lock()

stop_flag = threading.Event()

# Last message for status panel
last_msg      = ""
last_msg_lock = threading.Lock()

ssc = serial.Serial(PORT, BAUDRATE, timeout=1.0)
time.sleep(2)


def send(positions, t_ms=80):
    if estop:
        return
    cmd = " ".join(f"#{ch} P{int(pw)}" for ch, pw in sorted(positions.items()))
    cmd += f" T{t_ms}\r"
    ssc.write(cmd.encode("ascii"))


def set_msg(text):
    global last_msg
    with last_msg_lock:
        last_msg = text


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def smooth_home():
    """Smoothly interpolates current position to HOME in HOME_STEPS steps."""
    global going_home, estop
    estop = False
    going_home = True
    set_msg("Smooth return home...")

    with pos_lock:
        start = dict(pos)

    for step in range(1, HOME_STEPS + 1):
        if stop_flag.is_set() or estop:
            break
        t = step / HOME_STEPS
        interp = {sid: int(start[sid] + (HOME[sid] - start[sid]) * t) for sid in HOME}
        with pos_lock:
            pos.update(interp)
        send(interp, t_ms=HOME_STEP_MS)
        time.sleep(HOME_STEP_MS / 1000)

    with pos_lock:
        pos.update(HOME)
    send(HOME, t_ms=100)
    going_home = False
    set_msg("Home!")


# ── Thread 1: Servos ──────────────────────────────────────────────────────────

def servo_thread():
    interval = 1.0 / HZ
    last_ts  = time.monotonic()

    while not stop_flag.is_set():
        t0 = time.monotonic()

        if not playing and not estop and not going_home:
            with held_lock:
                current = set(held)

            changed = False
            with pos_lock:
                if 'a' in current: pos[BASE]     = clamp(pos[BASE]     - STEP, *LIMITS[BASE]);     changed = True
                if 'd' in current: pos[BASE]     = clamp(pos[BASE]     + STEP, *LIMITS[BASE]);     changed = True
                if 'w' in current: pos[SHOULDER] = clamp(pos[SHOULDER] + STEP, *LIMITS[SHOULDER]); changed = True
                if 's' in current: pos[SHOULDER] = clamp(pos[SHOULDER] - STEP, *LIMITS[SHOULDER]); changed = True
                if 'r' in current: pos[ELBOW]    = clamp(pos[ELBOW]    + STEP, *LIMITS[ELBOW]);    changed = True
                if 'f' in current: pos[ELBOW]    = clamp(pos[ELBOW]    - STEP, *LIMITS[ELBOW]);    changed = True
                if 't' in current: pos[WRIST]    = clamp(pos[WRIST]    + STEP, *LIMITS[WRIST]);    changed = True
                if 'g' in current: pos[WRIST]    = clamp(pos[WRIST]    - STEP, *LIMITS[WRIST]);    changed = True
                if 'q' in current: pos[GRIPPER]  = clamp(pos[GRIPPER]  - STEP, *LIMITS[GRIPPER]);  changed = True
                if 'e' in current: pos[GRIPPER]  = clamp(pos[GRIPPER]  + STEP, *LIMITS[GRIPPER]);  changed = True
                snapshot = dict(pos)

            if changed:
                send(snapshot, t_ms=80)
                if recording:
                    now   = time.monotonic()
                    delay = round(now - last_ts, 4)
                    last_ts = now
                    frames.append({"pos": dict(snapshot), "delay": max(delay, 0.02)})

        elapsed = time.monotonic() - t0
        time.sleep(max(0.0, interval - elapsed))


# ── Thread 2: Playback ────────────────────────────────────────────────────────

def playback_thread():
    global playing
    while not stop_flag.is_set():
        if not playing or not frames:
            time.sleep(0.05)
            continue

        for frame in frames:
            if stop_flag.is_set() or not playing or estop:
                break
            with pos_lock:
                pos.update(frame["pos"])
            send(frame["pos"], t_ms=int(frame["delay"] * 1000) or 80)
            time.sleep(frame["delay"])
        else:
            continue


# ── Thread 3: Status Panel ────────────────────────────────────────────────────

def status_thread():
    NUM_LINES = 16  # how many lines the panel takes
    # First print empty placeholder lines
    sys.stdout.write("\r\n" * NUM_LINES)
    sys.stdout.flush()

    while not stop_flag.is_set():
        time.sleep(0.15)

        # Mode
        if estop:
            mode    = "!! EMERGENCY STOP !!"
            mode_c  = "\033[31m"   # red
        elif going_home:
            mode    = ">> Smooth return home..."
            mode_c  = "\033[33m"   # yellow
        elif playing:
            mode    = ">> PLAYBACK (3 - stop)"
            mode_c  = "\033[32m"   # green
        elif recording:
            mode    = f"** RECORDING  {len(frames)} frames (1 - stop)"
            mode_c  = "\033[31m"
        else:
            mode    = "   Manual control"
            mode_c  = "\033[36m"   # cyan
        reset = "\033[0m"

        # Presets
        preset_str = ""
        for i in range(1, 4):
            preset_str += f"  [{i}]{'OK' if presets[i] else '--'}"

        # Speed
        filled = STEP // 5
        empty  = (STEP_MAX // 5) - filled
        spd_bar = "\033[32m" + "|" * filled + reset + "\033[90m" + "|" * empty + reset

        with pos_lock:
            p = dict(pos)

        with last_msg_lock:
            msg = last_msg

        # Строим панель
        W = 42
        sep = "+" + "-" * W + "+"

        lines = []
        lines.append(f"\r{sep}\r\n")
        lines.append(f"\r|  {mode_c}{mode:<38}{reset}  |\r\n")
        lines.append(f"\r|  Speed [{spd_bar}] {str(STEP):<3}  Presets:{preset_str}  |\r\n")
        lines.append(f"\r|" + "-" * W + "|\r\n")

        for sid, name in SERVO_NAMES.items():
            val    = p[sid]
            lo, hi = LIMITS[sid]
            pct    = int((val - lo) / (hi - lo) * 16)
            bar    = "\033[34m" + "#" * pct + reset + "\033[90m" + "-" * (16 - pct) + reset
            lines.append(f"\r|  {name}: [{bar}] {val:<5}|\r\n")

        lines.append(f"\r|" + "-" * W + "|\r\n")
        lines.append(f"\r|  {msg:<40}|\r\n")
        lines.append(f"\r{sep}\r\n")

        # Перемещаем курсор вверх и перерисовываем
        up = f"\033[{NUM_LINES}A"
        sys.stdout.write(up + "".join(lines))
        sys.stdout.flush()


# ── Main thread: Keyboard ─────────────────────────────────────────────────────

def read_keys():
    global recording, playing, frames, estop, STEP

    MOVE_KEYS = set('adwsrftgqe')
    # Preset keys: lowercase -> go to, uppercase -> save
    PRESET_GO  = {'5': 1, '6': 2, '7': 3}   # go to preset
    PRESET_SET = {'8': 1, '9': 2, 'k': 3}   # save preset

    fd  = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setraw(fd)

    help_text = (
        "\r+------------------------------------------+\r\n"
        "\r|  A/D W/S R/F T/G Q/E  -- movement       |\r\n"
        "\r|  1 -- record    2 -- play    3 -- stop   |\r\n"
        "\r|  0 -- smooth home    +/- -- speed        |\r\n"
        "\r|  5/6/7 -- go to preset 1/2/3             |\r\n"
        "\r|  8/9/K -- save preset 1/2/3              |\r\n"
        "\r|  Space -- STOP    Esc -- exit            |\r\n"
        "\r+------------------------------------------+\r\n"
    )
    sys.stdout.write(help_text)
    sys.stdout.flush()

    try:
        while not stop_flag.is_set():
            r, _, _ = select.select([sys.stdin], [], [], 0.02)

            if r:
                ch = sys.stdin.read(1)
                cl = ch.lower()

                if ch == '\x1b':
                    stop_flag.set()
                    break

                elif ch == ' ':
                    if estop:
                        estop = False
                        set_msg("Control restored")
                    else:
                        estop = True
                        with held_lock:
                            held.clear()
                        set_msg("!! EMERGENCY STOP !!")

                elif cl in MOVE_KEYS and not estop and not going_home:
                    with held_lock:
                        held.add(cl)
                    def release(k=cl):
                        time.sleep(0.12)
                        with held_lock:
                            held.discard(k)
                    threading.Thread(target=release, daemon=True).start()

                elif ch == '1':
                    if not recording:
                        frames.clear()
                        recording = True
                        set_msg("Recording started")
                    else:
                        recording = False
                        set_msg(f"Recording stopped — {len(frames)} frames")

                elif ch == '2':
                    if frames:
                        playing = True
                        recording = False
                        set_msg(f"Playback ({len(frames)} frames)")
                    else:
                        set_msg("No recorded frames!")

                elif ch == '3':
                    playing = False
                    set_msg("Playback stopped")

                elif ch == '+':
                    STEP = min(STEP + STEP_INC, STEP_MAX)
                    set_msg(f"Speed: {STEP}")

                elif ch == '-':
                    STEP = max(STEP - STEP_INC, STEP_MIN)
                    set_msg(f"Speed: {STEP}")

                elif ch == '0':
                    playing = False
                    recording = False
                    threading.Thread(target=smooth_home, daemon=True).start()

                # Presets — go to (lowercase)
                elif ch in PRESET_GO:
                    slot = PRESET_GO[ch]
                    if presets[slot]:
                        target = presets[slot]
                        with pos_lock:
                            pos.update(target)
                        send(target, t_ms=800)
                        set_msg(f"Preset {slot} loaded")
                    else:
                        set_msg(f"Preset {slot} empty — press 8/9/K to save")

                # Presets — save (uppercase = Shift)
                elif ch in PRESET_SET:
                    slot = PRESET_SET[ch]
                    with pos_lock:
                        presets[slot] = dict(pos)
                    set_msg(f"Preset {slot} saved!")

            else:
                with held_lock:
                    held.clear()

    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ── Startup ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    set_msg("Initialization...")
    threading.Thread(target=smooth_home, daemon=True).start()
    time.sleep(0.5)

    threads = [
        threading.Thread(target=servo_thread,    name="ServoThread",    daemon=True),
        threading.Thread(target=playback_thread, name="PlaybackThread", daemon=True),
        threading.Thread(target=status_thread,   name="StatusThread",   daemon=True),
    ]

    for t in threads:
        t.start()

    try:
        read_keys()
    except KeyboardInterrupt:
        stop_flag.set()

    stop_flag.set()
    for t in threads:
        t.join(timeout=2.0)

    smooth_home()
    ssc.close()
