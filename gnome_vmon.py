#!/usr/bin/env python3

"""GNOME Wayland virtual monitor helper for Dual Screen Mode.

On GNOME Wayland there is no krfb-virtualmonitor. Mutter can create a real
virtual monitor through its ScreenCast API (RecordVirtual), backed by a
PipeWire stream whose negotiated size becomes the monitor resolution. This
helper owns that session: while it runs, the virtual monitor exists; when it
exits, Mutter removes the monitor and falls back to the saved layout.

It also reads and applies the display layout through Mutter's DisplayConfig
API so the virtual monitor can be placed centered below the main display.

Subcommands:
  run --width W --height H --runtime-dir DIR --connector-file FILE
  status
  virtual-connector --connector-file FILE
  monitor-spec --connector CONNECTOR
  place --connector CONNECTOR --width W --height H
"""

import json
import os
import signal
import subprocess
import sys
import time

SCREENCAST_NAME = "org.gnome.Mutter.ScreenCast"
SCREENCAST_PATH = "/org/gnome/Mutter/ScreenCast"
SESSION_IFACE = "org.gnome.Mutter.ScreenCast.Session"
DISPLAYCONFIG_NAME = "org.gnome.Mutter.DisplayConfig"
DISPLAYCONFIG_PATH = "/org/gnome/Mutter/DisplayConfig"
VIRTUAL_PREFIX = "Meta-"
START_TIMEOUT_SECONDS = 25


def _unwrap(value):
    if isinstance(value, dict) and set(value.keys()) == {"type", "data"}:
        return _unwrap(value["data"])
    if isinstance(value, list):
        return [_unwrap(item) for item in value]
    if isinstance(value, dict):
        return {key: _unwrap(item) for key, item in value.items()}
    return value


def busctl_call_json(destination, path, interface, method, signature="", args=()):
    cmd = ["busctl", "--user", "--json=short", "call", destination, path, interface, method]
    if signature:
        cmd.append(signature)
        cmd.extend(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise RuntimeError("busctl %s failed: %s" % (method, result.stderr.strip() or result.stdout.strip()))
    text = result.stdout.strip()
    if not text:
        return None
    return _unwrap(json.loads(text))


def busctl_call(destination, path, interface, method, signature="", args=()):
    cmd = ["busctl", "--user", "call", destination, path, interface, method]
    if signature:
        cmd.append(signature)
        cmd.extend(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise RuntimeError("busctl %s failed: %s" % (method, result.stderr.strip() or result.stdout.strip()))
    return result.stdout.strip()


def get_state():
    data = busctl_call_json(
        DISPLAYCONFIG_NAME, DISPLAYCONFIG_PATH,
        DISPLAYCONFIG_NAME, "GetCurrentState",
    )
    if not isinstance(data, list) or len(data) < 4:
        raise RuntimeError("unexpected GetCurrentState reply")
    serial, monitors, logical_monitors, _properties = data
    return {
        "serial": serial,
        "monitors": monitors,
        "logical_monitors": logical_monitors,
    }


def monitor_entry(state, connector):
    for entry in state["monitors"]:
        if entry[0] == connector:
            return entry
    return None


def current_mode_id(state, connector):
    entry = monitor_entry(state, connector)
    if not entry:
        return None
    modes = entry[4] or []
    for mode in modes:
        props = mode[6] or {}
        if props.get("is-current"):
            return mode[0]
    for mode in modes:
        props = mode[6] or {}
        if props.get("is-preferred"):
            return mode[0]
    if modes:
        return modes[0][0]
    return None


def mode_size(state, connector):
    entry = monitor_entry(state, connector)
    if not entry:
        return None
    modes = entry[4] or []
    current = None
    for mode in modes:
        props = mode[6] or {}
        if props.get("is-current"):
            current = mode
            break
    if current is None and modes:
        current = modes[0]
    if current is None:
        return None
    return int(current[1]), int(current[2])


def virtual_connectors(state):
    return [entry[0] for entry in state["monitors"] if str(entry[0]).startswith(VIRTUAL_PREFIX)]


def logical_size(state, logical):
    _x, _y, scale, _transform, _primary, monitors, _props = logical
    if not scale or scale <= 0:
        scale = 1.0
    width = height = 0
    for spec in monitors:
        size = mode_size(state, spec[0])
        if size:
            width = max(width, size[0])
            height = max(height, size[1])
    if not width:
        return None
    return (int(round(width / scale)), int(round(height / scale)))


def find_virtual_logical(state, connector):
    for logical in state["logical_monitors"]:
        for spec in logical[5]:
            if spec[0] == connector:
                return logical
    return None


def find_main_logical(state):
    logicals = state["logical_monitors"]
    if not logicals:
        return None
    for logical in logicals:
        if logical[4]:
            return logical
    for logical in logicals:
        if logical[0] == 0 and logical[1] == 0:
            return logical
    return max(logicals, key=lambda item: (logical_size(state, item) or (0, 0))[0] * (logical_size(state, item) or (0, 0))[1])


def gvariant_monitors_config(state, override):
    """Serialize logical monitors for ApplyMonitorsConfig.

    override maps connector -> (x, y) for logical monitors that should move.
    """
    parts = []
    for logical in state["logical_monitors"]:
        x, y, scale, transform, primary, monitors, _props = logical
        if not scale or scale <= 0:
            scale = 1.0
        specs = []
        for spec in monitors:
            connector = spec[0]
            mode_id = current_mode_id(state, connector)
            if not mode_id:
                continue
            if connector in override:
                x, y = override[connector]
            specs.append("('%s', '%s', [])" % (connector, mode_id))
        parts.append("(%d, %d, %s, %d, %s, [%s])" % (
            int(x), int(y), repr(float(scale)), int(transform), "true" if primary else "false",
            ", ".join(specs),
        ))
    return "[%s]" % ", ".join(parts)


def place_virtual(state, connector, width, height):
    virtual_logical = find_virtual_logical(state, connector)
    main = find_main_logical(state)
    if virtual_logical is None or main is None:
        return False
    virt_size = logical_size(state, virtual_logical)
    main_size = logical_size(state, main)
    if not virt_size or not main_size:
        # Fall back to the requested resolution when the current mode cannot
        # be read; placement is cosmetic and must never break startup.
        virt_size = (int(width), int(height))
        main_size = main_size or virt_size
    main_x, main_y = int(main[0]), int(main[1])
    target_x = main_x + max(0, (main_size[0] - virt_size[0]) // 2)
    target_y = main_y + main_size[1]
    config = gvariant_monitors_config(state, {connector: (target_x, target_y)})
    busctl_call(
        DISPLAYCONFIG_NAME, DISPLAYCONFIG_PATH, DISPLAYCONFIG_NAME,
        "ApplyMonitorsConfig", "uua(iiduba(ssa{sv}))a{sv}",
        [str(state["serial"]), "1", config, "[]"],
    )
    return True


def cmd_status(_args):
    state = get_state()
    monitors = []
    for entry in state["monitors"]:
        connector, vendor, product, serial = entry[0], entry[1], entry[2], entry[3]
        modes = []
        for mode in entry[4] or []:
            props = mode[6] or {}
            modes.append({
                "id": mode[0],
                "width": mode[1],
                "height": mode[2],
                "refresh_rate": mode[3],
                "is_current": bool(props.get("is-current")),
                "is_preferred": bool(props.get("is-preferred")),
            })
        monitors.append({
            "connector": connector,
            "vendor": vendor,
            "product": product,
            "serial": serial,
            "modes": modes,
        })
    logicals = []
    for logical in state["logical_monitors"]:
        logicals.append({
            "x": logical[0],
            "y": logical[1],
            "scale": logical[2],
            "transform": logical[3],
            "primary": bool(logical[4]),
            "monitors": [spec[0] for spec in logical[5]],
        })
    print(json.dumps({
        "serial": state["serial"],
        "monitors": monitors,
        "logical_monitors": logicals,
    }))
    return 0


def cmd_virtual_connector(args):
    connector_file = args[args.index("--connector-file") + 1]
    state = get_state()
    virtuals = virtual_connectors(state)
    saved = ""
    try:
        with open(connector_file, "r", encoding="utf-8") as handle:
            saved = handle.read().strip()
    except OSError:
        saved = ""
    if saved and saved in virtuals:
        print(saved)
        return 0
    if len(virtuals) == 1:
        print(virtuals[0])
        return 0
    if virtuals:
        print(virtuals[0])
        return 0
    return 0


def cmd_monitor_spec(args):
    connector = args[args.index("--connector") + 1]
    state = get_state()
    entry = monitor_entry(state, connector)
    if not entry:
        return 1
    print("%s\t%s\t%s" % (entry[1], entry[2], entry[3]))
    return 0


def cmd_place(args):
    connector = args[args.index("--connector") + 1]
    width = int(args[args.index("--width") + 1])
    height = int(args[args.index("--height") + 1])
    state = get_state()
    place_virtual(state, connector, width, height)
    return 0


def start_consumer(stream_path, width, height, log_path):
    cmd = [
        "gst-launch-1.0",
        "pipewiresrc", "path=%s" % stream_path, "!",
        "videoconvert", "!",
        "video/x-raw,format=BGRx,width=%d,height=%d,framerate=60/1" % (width, height), "!",
        "fakesink",
    ]
    log = open(log_path, "ab", buffering=0)
    try:
        return subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL)
    except OSError as error:
        raise RuntimeError("could not start the PipeWire consumer (gst-launch-1.0): %s" % error)


def cmd_run(args):
    width = int(args[args.index("--width") + 1])
    height = int(args[args.index("--height") + 1])
    runtime_dir = args[args.index("--runtime-dir") + 1]
    connector_file = args[args.index("--connector-file") + 1]

    state = get_state()
    existing = set(virtual_connectors(state))
    if existing:
        print("a virtual monitor already exists: %s" % ", ".join(sorted(existing)), file=sys.stderr)
        return 4

    session_path = busctl_call_json(
        SCREENCAST_NAME, SCREENCAST_PATH, SCREENCAST_NAME, "CreateSession", "a{sv}", ["{}"],
    )
    if not session_path:
        raise RuntimeError("Mutter did not return a ScreenCast session")
    stream_path = busctl_call_json(
        SCREENCAST_NAME, session_path, SESSION_IFACE, "RecordVirtual", "a{sv}", ["{}"],
    )
    if not stream_path:
        raise RuntimeError("Mutter did not return a virtual stream")
    busctl_call(SCREENCAST_NAME, session_path, SESSION_IFACE, "Start")

    consumer = start_consumer(stream_path, width, height, os.path.join(runtime_dir, "vmonitor-consumer.log"))

    stopping = {"flag": False}

    def handle_signal(_signum, _frame):
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    connector = None
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline and not stopping["flag"]:
        try:
            state = get_state()
        except RuntimeError:
            time.sleep(0.5)
            continue
        new_virtuals = [item for item in virtual_connectors(state) if item not in existing]
        if new_virtuals:
            connector = new_virtuals[0]
            break
        if consumer.poll() is not None:
            break
        time.sleep(0.25)

    if connector is None:
        try:
            busctl_call(SCREENCAST_NAME, session_path, SESSION_IFACE, "Close")
        except RuntimeError:
            pass
        consumer.terminate()
        print("the virtual monitor did not appear; is pipewire-gstreamer installed?", file=sys.stderr)
        return 5

    try:
        with open(connector_file, "w", encoding="utf-8") as handle:
            handle.write(connector + "\n")
        os.chmod(connector_file, 0o600)
    except OSError:
        pass

    try:
        place_virtual(get_state(), connector, width, height)
    except RuntimeError as error:
        print("virtual monitor created, but placement failed: %s" % error, file=sys.stderr)

    exit_code = 0
    while not stopping["flag"]:
        time.sleep(1)
        try:
            state = get_state()
        except RuntimeError:
            continue
        if connector not in virtual_connectors(state):
            exit_code = 1
            break

    try:
        busctl_call(SCREENCAST_NAME, session_path, SESSION_IFACE, "Close")
    except RuntimeError:
        pass
    if consumer.poll() is None:
        consumer.terminate()
        try:
            consumer.wait(timeout=5)
        except subprocess.TimeoutExpired:
            consumer.kill()
    try:
        os.unlink(connector_file)
    except OSError:
        pass
    return exit_code


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    command = sys.argv[1]
    args = sys.argv[2:]
    try:
        if command == "run":
            return cmd_run(args)
        if command == "status":
            return cmd_status(args)
        if command == "virtual-connector":
            return cmd_virtual_connector(args)
        if command == "monitor-spec":
            return cmd_monitor_spec(args)
        if command == "place":
            return cmd_place(args)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 3
    print("unknown command: %s" % command, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
