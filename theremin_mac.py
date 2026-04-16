import argparse
import json
import math
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import serial
import sounddevice as sd
from serial.tools import list_ports


DEFAULT_CONFIG = {
    "serial_port": None,
    "baudrate": 115200,
    "pitch_hand": {
        "raw_min": 5300.0,
        "raw_max": 10000.0,
        "hz_min": 220.0,
        "hz_max": 880.0,
        "invert": False,
    },
    "volume_hand": {
        "raw_min": 4700.0,
        "raw_max": 6000.0,
        "level_min": 0.0,
        "level_max": 0.9,
        "invert": False,
    },
    "audio": {
        "sample_rate": 48000,
        "block_size": 512,
        "status_hz": 4.0,
        "stale_after_s": 0.5,
    },
    "smoothing": {
        "pitch_ms": 18.0,
        "volume_ms": 35.0,
    },
    "startup": {
        "calibrate_on_start": False,
        "countdown_s": 3.0,
        "capture_s": 1.25,
    },
}

SAMPLE_LINE_RE = re.compile(r"^\s*-?\d+(?:\.\d+)?,\s*-?\d+(?:\.\d+)?\s*$")


class SharedState:
    def __init__(self):
        self.lock = threading.Lock()
        self.raw_pitch = 0.0
        self.raw_volume = 0.0
        self.freq_hz = 440.0
        self.level = 0.0
        self.last_data_time = 0.0
        self.error = None


class ThereminSynth:
    def __init__(self, shared_state, config):
        self.shared_state = shared_state
        self.config = config
        self.phase = 0.0
        self.current_freq = float(config["pitch_hand"]["hz_min"])
        self.current_level = 0.0
        self.sample_rate = int(config["audio"]["sample_rate"])
        self.stale_after_s = float(config["audio"]["stale_after_s"])

    def callback(self, outdata, frames, _time_info, status):
        if status:
            print(status, file=sys.stderr)

        with self.shared_state.lock:
            target_freq = self.shared_state.freq_hz
            target_level = self.shared_state.level
            last_data_time = self.shared_state.last_data_time

        if time.time() - last_data_time > self.stale_after_s:
            target_level = 0.0

        freq = np.linspace(self.current_freq, target_freq, frames, endpoint=False, dtype=np.float32)
        level = np.linspace(self.current_level, target_level, frames, endpoint=False, dtype=np.float32)
        phase_steps = (2.0 * math.pi * freq) / self.sample_rate
        phases = self.phase + np.cumsum(phase_steps, dtype=np.float32)
        mono = np.sin(phases).astype(np.float32) * level

        outdata[:, 0] = mono
        outdata[:, 1] = mono

        self.phase = float(phases[-1] % (2.0 * math.pi))
        self.current_freq = float(target_freq)
        self.current_level = float(target_level)


def merge_dict(base, override):
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def ensure_config(path):
    if not path.exists():
        path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
    with path.open() as fh:
        return merge_dict(DEFAULT_CONFIG, json.load(fh))


def save_config(path, config):
    path.write_text(json.dumps(config, indent=2) + "\n")


def find_serial_port(preferred, wait_seconds=12.0):
    if preferred:
        return preferred

    deadline = time.time() + wait_seconds
    saw_ports = False
    while time.time() < deadline:
        candidates = []
        for port in list_ports.comports():
            text = " ".join(
                filter(
                    None,
                    [port.device, port.description, port.manufacturer, port.product],
                )
            ).lower()
            score = 0
            if "usbmodem" in port.device.lower():
                score += 5
            if "pico" in text:
                score += 3
            if "rp2" in text:
                score += 3
            if "usb" in text:
                score += 1
            candidates.append((score, port.device))

        candidates.sort(reverse=True)
        if candidates:
            saw_ports = True
            if candidates[0][0] > 0:
                return candidates[0][1]

        if os.path.isdir("/Volumes/RPI-RP2"):
            raise RuntimeError(
                "Pico is in BOOTSEL storage mode (RPI-RP2). Unplug it and plug it back in without holding BOOTSEL."
            )

        time.sleep(0.5)

    if not saw_ports:
        raise RuntimeError("No serial ports found. Replug the Pico normally and try again.")
    raise RuntimeError("No obvious Pico serial port found. Pass --port explicitly if needed.")


def install_pico_script(port, script_path):
    cmd = [
        sys.executable,
        "-m",
        "mpremote",
        "connect",
        port,
        "fs",
        "cp",
        str(script_path),
        ":theremin_pico.py",
    ]
    subprocess.run(cmd, check=True)
    boot_path = Path(__file__).with_name("pico_main.py")
    boot_cmd = [
        sys.executable,
        "-m",
        "mpremote",
        "connect",
        port,
        "fs",
        "cp",
        str(boot_path),
        ":main.py",
    ]
    subprocess.run(boot_cmd, check=True)


def map_range(raw_value, hand_config):
    raw_min = float(hand_config["raw_min"])
    raw_max = float(hand_config["raw_max"])
    if raw_max == raw_min:
        return 0.0

    normalized = (raw_value - raw_min) / (raw_max - raw_min)
    normalized = max(0.0, min(1.0, normalized))
    if hand_config.get("invert", False):
        normalized = 1.0 - normalized
    return normalized


def pitch_from_raw(raw_value, config):
    normalized = map_range(raw_value, config["pitch_hand"])
    hz_min = float(config["pitch_hand"]["hz_min"])
    hz_max = float(config["pitch_hand"]["hz_max"])
    if hz_min <= 0.0 or hz_max <= 0.0:
        raise RuntimeError("Pitch frequency limits must be positive.")
    return hz_min * ((hz_max / hz_min) ** normalized)


def level_from_raw(raw_value, config):
    normalized = map_range(raw_value, config["volume_hand"])
    level_min = float(config["volume_hand"]["level_min"])
    level_max = float(config["volume_hand"]["level_max"])
    return level_min + (level_max - level_min) * (normalized ** 2)


def smooth_value(current, target, dt, smoothing_ms):
    if current is None:
        return target
    if smoothing_ms <= 0.0 or dt <= 0.0:
        return target
    tau = smoothing_ms / 1000.0
    alpha = 1.0 - math.exp(-dt / tau)
    return current + (target - current) * alpha


def start_pico_stream(ser):
    ser.reset_input_buffer()
    ser.write(b"\x03")
    ser.flush()
    time.sleep(0.1)
    ser.reset_input_buffer()
    ser.write(b"\x04")
    ser.flush()
    time.sleep(0.8)


def serial_reader(port, config, shared_state, stop_event, start_pico):
    smoothed_freq = None
    smoothed_level = None
    last_update_time = None

    while not stop_event.is_set():
        try:
            with serial.Serial(port, int(config["baudrate"]), timeout=1) as ser:
                time.sleep(0.4)
                if start_pico:
                    start_pico_stream(ser)

                with shared_state.lock:
                    shared_state.error = None

                smoothed_freq = None
                smoothed_level = None
                last_update_time = None

                while not stop_event.is_set():
                    line = ser.readline()
                    if not line:
                        continue
                    text = line.decode("utf-8", errors="replace").strip()
                    if not text or text.startswith((">>>", "...", "MicroPython", "Type \"help()\"")):
                        continue
                    if text.startswith("#"):
                        continue
                    if not SAMPLE_LINE_RE.match(text):
                        continue

                    left, right = text.split(",", 1)
                    try:
                        raw_pitch = float(left)
                        raw_volume = float(right)
                    except ValueError:
                        continue

                    target_freq_hz = pitch_from_raw(raw_pitch, config)
                    target_level = level_from_raw(raw_volume, config)

                    now = time.time()
                    dt = 0.0 if last_update_time is None else now - last_update_time
                    last_update_time = now

                    smoothed_freq = smooth_value(
                        smoothed_freq,
                        target_freq_hz,
                        dt,
                        float(config["smoothing"]["pitch_ms"]),
                    )
                    smoothed_level = smooth_value(
                        smoothed_level,
                        target_level,
                        dt,
                        float(config["smoothing"]["volume_ms"]),
                    )

                    with shared_state.lock:
                        shared_state.raw_pitch = raw_pitch
                        shared_state.raw_volume = raw_volume
                        shared_state.freq_hz = smoothed_freq
                        shared_state.level = smoothed_level
                        shared_state.last_data_time = now
        except serial.SerialException:
            if stop_event.is_set():
                return
            time.sleep(0.5)
        except Exception as exc:
            with shared_state.lock:
                shared_state.error = exc
            stop_event.set()
            return


def print_calibration_loop(shared_state, stop_event):
    min_pitch = math.inf
    max_pitch = -math.inf
    min_volume = math.inf
    max_volume = -math.inf
    last_seen = 0.0

    while not stop_event.is_set():
        with shared_state.lock:
            raw_pitch = shared_state.raw_pitch
            raw_volume = shared_state.raw_volume
            seen = shared_state.last_data_time
            error = shared_state.error

        if error:
            raise error

        if seen and seen != last_seen:
            last_seen = seen
            min_pitch = min(min_pitch, raw_pitch)
            max_pitch = max(max_pitch, raw_pitch)
            min_volume = min(min_volume, raw_volume)
            max_volume = max(max_volume, raw_volume)
            print(
                "pitch raw={:.2f} range=[{:.2f}, {:.2f}]   volume raw={:.2f} range=[{:.2f}, {:.2f}]".format(
                    raw_pitch,
                    min_pitch,
                    max_pitch,
                    raw_volume,
                    min_volume,
                    max_volume,
                ),
                flush=True,
            )
        time.sleep(0.02)


def wait_for_samples(shared_state, stop_event, timeout_s=8.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline and not stop_event.is_set():
        with shared_state.lock:
            seen = shared_state.last_data_time
            error = shared_state.error
        if error:
            raise error
        if seen:
            return
        time.sleep(0.05)
    raise RuntimeError("Timed out waiting for Pico samples.")


def countdown(seconds, message):
    print(message, flush=True)
    whole_seconds = max(1, int(math.ceil(seconds)))
    for remaining in range(whole_seconds, 0, -1):
        print(f"  {remaining}...", flush=True)
        time.sleep(1.0)


def capture_pose(shared_state, stop_event, duration_s):
    end_time = time.time() + duration_s
    pitch_values = []
    volume_values = []
    last_seen = 0.0

    while time.time() < end_time and not stop_event.is_set():
        with shared_state.lock:
            raw_pitch = shared_state.raw_pitch
            raw_volume = shared_state.raw_volume
            seen = shared_state.last_data_time
            error = shared_state.error

        if error:
            raise error

        if seen and seen != last_seen:
            last_seen = seen
            pitch_values.append(raw_pitch)
            volume_values.append(raw_volume)

        time.sleep(0.01)

    if not pitch_values or not volume_values:
        raise RuntimeError("No calibration samples were captured.")

    return {
        "pitch": statistics.median(pitch_values),
        "volume": statistics.median(volume_values),
    }


def calibrate_range(first_value, second_value, minimum_span):
    raw_min = min(first_value, second_value)
    raw_max = max(first_value, second_value)
    span = raw_max - raw_min
    if span >= minimum_span:
        return raw_min, raw_max

    midpoint = (raw_min + raw_max) * 0.5
    half_span = minimum_span * 0.5
    return midpoint - half_span, midpoint + half_span


def apply_guided_calibration(shared_state, stop_event, config, config_path):
    wait_for_samples(shared_state, stop_event)

    countdown(
        float(config["startup"]["countdown_s"]),
        "Guided calibration: get ready to place both hands at their closest playing positions.",
    )
    close_capture = capture_pose(shared_state, stop_event, float(config["startup"]["capture_s"]))

    countdown(
        float(config["startup"]["countdown_s"]),
        "Now move both hands to their farthest playing positions.",
    )
    far_capture = capture_pose(shared_state, stop_event, float(config["startup"]["capture_s"]))

    pitch_min, pitch_max = calibrate_range(close_capture["pitch"], far_capture["pitch"], minimum_span=60.0)
    volume_min, volume_max = calibrate_range(close_capture["volume"], far_capture["volume"], minimum_span=80.0)
    config["pitch_hand"]["raw_min"] = pitch_min
    config["pitch_hand"]["raw_max"] = pitch_max
    config["volume_hand"]["raw_min"] = volume_min
    config["volume_hand"]["raw_max"] = volume_max
    save_config(config_path, config)

    print(
        (
            "Saved calibration: "
            "pitch=[{:.2f}, {:.2f}] volume=[{:.2f}, {:.2f}]"
        ).format(
            float(config["pitch_hand"]["raw_min"]),
            float(config["pitch_hand"]["raw_max"]),
            float(config["volume_hand"]["raw_min"]),
            float(config["volume_hand"]["raw_max"]),
        ),
        flush=True,
    )


def run_audio_loop(shared_state, config, stop_event):
    synth = ThereminSynth(shared_state, config)
    sample_rate = int(config["audio"]["sample_rate"])
    block_size = int(config["audio"]["block_size"])
    status_interval = 1.0 / float(config["audio"]["status_hz"])
    last_print = 0.0

    with sd.OutputStream(
        samplerate=sample_rate,
        blocksize=block_size,
        channels=2,
        dtype="float32",
        callback=synth.callback,
    ):
        while not stop_event.is_set():
            with shared_state.lock:
                raw_pitch = shared_state.raw_pitch
                raw_volume = shared_state.raw_volume
                freq_hz = shared_state.freq_hz
                level = shared_state.level
                error = shared_state.error

            if error:
                raise error

            now = time.time()
            if now - last_print >= status_interval:
                last_print = now
                print(
                    "\rpitch raw={:7.2f} -> {:7.2f} Hz   volume raw={:7.2f} -> {:0.2f}    ".format(
                        raw_pitch,
                        freq_hz,
                        raw_volume,
                        level,
                    ),
                    end="",
                    flush=True,
                )
            time.sleep(0.05)

    print()


def list_audio_devices():
    print(sd.query_devices())


def build_parser():
    parser = argparse.ArgumentParser(description="Two-sensor Pico theremin on Mac audio.")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("theremin_config.json"))
    parser.add_argument("--port", help="Serial port for the Pico, e.g. /dev/cu.usbmodem2101")
    parser.add_argument("--install-pico", action="store_true", help="Copy theremin_pico.py to the Pico.")
    parser.add_argument(
        "--start-pico",
        action="store_true",
        help="Start theremin_pico.py over the serial REPL. This is now the default behavior.",
    )
    parser.add_argument("--calibrate", action="store_true", help="Print raw values and rolling min/max instead of audio.")
    parser.add_argument(
        "--calibrate-on-start",
        action="store_true",
        help="Run a guided close/far calibration and save the new ranges before starting audio.",
    )
    parser.add_argument("--list-audio-devices", action="store_true", help="List output devices and exit.")
    return parser


def main():
    args = build_parser().parse_args()

    if args.list_audio_devices:
        list_audio_devices()
        return

    config = ensure_config(args.config)
    port = find_serial_port(args.port or config.get("serial_port"))
    pico_script = Path(__file__).with_name("theremin_pico.py")
    start_pico = True
    calibrate_on_start = bool(args.calibrate_on_start or config["startup"]["calibrate_on_start"])

    if args.install_pico:
        install_pico_script(port, pico_script)
        if not args.calibrate:
            print(f"Installed theremin_pico.py to {port}")
            return

    shared_state = SharedState()
    stop_event = threading.Event()
    reader = threading.Thread(
        target=serial_reader,
        args=(port, config, shared_state, stop_event, start_pico),
        daemon=True,
    )
    reader.start()

    try:
        if args.calibrate:
            print_calibration_loop(shared_state, stop_event)
        else:
            if calibrate_on_start:
                apply_guided_calibration(shared_state, stop_event, config, args.config)
            run_audio_loop(shared_state, config, stop_event)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        reader.join(timeout=1.0)


if __name__ == "__main__":
    main()
