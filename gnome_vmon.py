#!/usr/bin/env python3

"""GNOME Wayland virtual monitor helper for Dual Screen Mode.

On GNOME Wayland there is no krfb-virtualmonitor. Mutter can create a real
virtual monitor through its ScreenCast API (RecordVirtual), backed by a
PipeWire stream whose negotiated size becomes the monitor resolution. This
helper owns that session: while it runs, the virtual monitor exists; when it
exits, Mutter removes the monitor and falls back to the saved layout.

It also reads and applies the display layout through Mutter's DisplayConfig
API so the virtual monitor can be placed centered below the main display.

The D-Bus replies are parsed defensively: different GNOME versions render
slightly different struct shapes, so field access goes through helpers that
tolerate short or differently nested entries instead of crashing.

Subcommands:
  run --width W --height H --runtime-dir DIR --connector-file FILE
  status            (prints parsed JSON; adds "raw" when the shape is unknown)
  raw               (prints the raw busctl reply, for debugging)
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


def fields(entry):
    """Return a struct entry as a list, tolerating unexpected shapes."""
    if isinstance(entry, list):
        return entry
    if isinstance(entry, tuple):
        return list(entry)
    return []


def get_state():
    data = busctl_call_json(
        DISPLAYCONFIG_NAME, DISPLAYCONFIG_PATH,
        DISPLAYCONFIG_NAME, "GetCurrentState",
    )
    if not isinstance(data, list) or len(data) < 3:
        raise RuntimeError("unexpected GetCurrentState reply: %r" % (data,))
    monitors = data[1] if isinstance(data[1], list) else []
    logical_monitors = data[2] if isinstance(data[2], list) else []
    return {
        "serial": data[0],
        "monitors": monitors,
        "logical_monitors": logical_monitors,
        "raw": data,
    }


def monitor_parts(entry):
    """Split a monitor entry into (spec, modes, props).

    Current mutter nests the spec: ((ssss) modes props). Tolerate the flat
    (ssss modes props) layout as well.
    """
    entry_f = fields(entry)
    if not entry_f:
        return [], [], {}
    if isinstance(entry_f[0], list):
        spec = entry_f[0]
        modes = entry_f[1] if len(entry_f) > 1 and isinstance(entry_f[1], list) else []
        props = entry_f[2] if len(entry_f) > 2 and isinstance(entry_f[2], dict) else {}
        return spec, modes, props
    spec = entry_f[:4]
    modes = entry_f[4] if len(entry_f) > 4 and isinstance(entry_f[4], list) else []
    props = entry_f[5] if len(entry_f) > 5 and isinstance(entry_f[5], dict) else {}
    return spec, modes, props


def connector_of(entry):
    spec, _modes, _props = monitor_parts(entry)
    if spec and isinstance(spec[0], str):
        return spec[0]
    return None


def monitor_entry(state, connector):
    for entry in state["monitors"]:
        if connector_of(entry) == connector:
            return fields(entry)
    return None


def mode_fields(mode):
    return fields(mode)


def current_mode(state, connector):
    entry = monitor_entry(state, connector)
    if entry is None:
        return None
    _spec, modes, _props = monitor_parts(entry)
    current = None
    preferred = None
    first = None
    for mode in modes:
        mode_f = mode_fields(mode)
        if len(mode_f) < 4:
            continue
        if first is None:
            first = mode_f
        props = mode_f[6] if len(mode_f) > 6 and isinstance(mode_f[6], dict) else {}
        if props.get("is-current"):
            current = mode_f
        if props.get("is-preferred") and preferred is None:
            preferred = mode_f
    chosen = current or preferred or first
    if not chosen:
        return None
    return {
        "id": chosen[0],
        "width": int(chosen[1]),
        "height": int(chosen[2]),
    }


def current_mode_id(state, connector):
    mode = current_mode(state, connector)
    return mode["id"] if mode else None


def mode_size(state, connector):
    mode = current_mode(state, connector)
    if not mode:
        return None
    return mode["width"], mode["height"]


def _scan_strings(obj, out):
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _scan_strings(item, out)
    elif isinstance(obj, dict):
        for item in obj.values():
            _scan_strings(item, out)


def all_connectors(state):
    found = []
    for entry in state["monitors"]:
        connector = connector_of(entry)
        if connector and connector not in found:
            found.append(connector)
    return found


def looks_virtual(name):
    lowered = name.lower()
    return lowered.startswith(VIRTUAL_PREFIX.lower()) or "virtual" in lowered or "headless" in lowered


def virtual_connectors(state):
    found = []
    for connector in all_connectors(state):
        if looks_virtual(connector) and connector not in found:
            found.append(connector)
    if not found:
        # Shape fallback: scan the whole reply for virtual connector names.
        strings = []
        _scan_strings(state["raw"], strings)
        for item in strings:
            if looks_virtual(item) and item not in found:
                found.append(item)
    return found


def logical_fields(logical):
    return fields(logical)


def logical_size(state, logical):
    logical_f = logical_fields(logical)
    if len(logical_f) < 6:
        return None
    scale = logical_f[2]
    if not isinstance(scale, (int, float)) or scale <= 0:
        scale = 1.0
    width = height = 0
    monitors = logical_f[5] if isinstance(logical_f[5], list) else []
    for spec in monitors:
        spec_f = fields(spec)
        connector = spec_f[0] if spec_f and isinstance(spec_f[0], str) else (spec if isinstance(spec, str) else None)
        if not connector:
            continue
        size = mode_size(state, connector)
        if size:
            width = max(width, size[0])
            height = max(height, size[1])
    if not width:
        return None
    return (int(round(width / scale)), int(round(height / scale)))


def find_virtual_logical(state, connector):
    for logical in state["logical_monitors"]:
        logical_f = logical_fields(logical)
        if len(logical_f) < 6:
            continue
        monitors = logical_f[5] if isinstance(logical_f[5], list) else []
        for spec in monitors:
            spec_f = fields(spec)
            spec_connector = spec_f[0] if spec_f and isinstance(spec_f[0], str) else (spec if isinstance(spec, str) else None)
            if spec_connector == connector:
                return logical_f
    return None


def find_main_logical(state):
    logicals = [logical_fields(item) for item in state["logical_monitors"]]
    logicals = [item for item in logicals if len(item) >= 6]
    if not logicals:
        return None
    for logical in logicals:
        if logical[4]:
            return logical
    for logical in logicals:
        if logical[0] == 0 and logical[1] == 0:
            return logical
    def area(logical):
        size = logical_size(state, logical)
        return (size[0] * size[1]) if size else 0
    return max(logicals, key=area)


def gvariant_monitors_config(state, override):
    """Serialize logical monitors for ApplyMonitorsConfig.

    override maps connector -> (x, y) for logical monitors that should move.
    """
    parts = []
    for logical in state["logical_monitors"]:
        logical_f = logical_fields(logical)
        if len(logical_f) < 6:
            continue
        x, y, scale, transform, primary = logical_f[0], logical_f[1], logical_f[2], logical_f[3], logical_f[4]
        if not isinstance(scale, (int, float)) or scale <= 0:
            scale = 1.0
        monitors = logical_f[5] if isinstance(logical_f[5], list) else []
        specs = []
        for spec in monitors:
            spec_f = fields(spec)
            connector = spec_f[0] if spec_f and isinstance(spec_f[0], str) else (spec if isinstance(spec, str) else None)
            if not connector:
                continue
            mode_id = current_mode_id(state, connector)
            if not mode_id:
                continue
            if connector in override:
                x, y = override[connector]
            specs.append("('%s', '%s', [])" % (connector, mode_id))
        if not specs:
            continue
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
    if not config or config == "[]":
        return False
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
        connector = connector_of(entry)
        if connector is None:
            monitors.append({"raw": entry})
            continue
        spec, raw_modes, _props = monitor_parts(entry)
        vendor = spec[1] if len(spec) > 1 else ""
        product = spec[2] if len(spec) > 2 else ""
        serial = spec[3] if len(spec) > 3 else ""
        modes = []
        for mode in raw_modes:
            mode_f = mode_fields(mode)
            if len(mode_f) < 4:
                continue
            props = mode_f[6] if len(mode_f) > 6 and isinstance(mode_f[6], dict) else {}
            modes.append({
                "id": mode_f[0],
                "width": mode_f[1],
                "height": mode_f[2],
                "refresh_rate": mode_f[3],
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
        logical_f = logical_fields(logical)
        if len(logical_f) < 6:
            logicals.append({"raw": logical})
            continue
        monitors_list = logical_f[5] if isinstance(logical_f[5], list) else []
        connectors = []
        for spec in monitors_list:
            spec_f = fields(spec)
            connectors.append(spec_f[0] if spec_f and isinstance(spec_f[0], str) else (spec if isinstance(spec, str) else ""))
        logicals.append({
            "x": logical_f[0],
            "y": logical_f[1],
            "scale": logical_f[2],
            "transform": logical_f[3],
            "primary": bool(logical_f[4]),
            "monitors": connectors,
        })
    output = {
        "serial": state["serial"],
        "monitors": monitors,
        "logical_monitors": logicals,
    }
    if any("raw" in item for item in monitors) or any("raw" in item for item in logicals):
        output["raw"] = state["raw"]
    print(json.dumps(output))
    return 0


def cmd_raw(_args):
    data = busctl_call_json(
        DISPLAYCONFIG_NAME, DISPLAYCONFIG_PATH,
        DISPLAYCONFIG_NAME, "GetCurrentState",
    )
    print(json.dumps(data))
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
    if saved:
        strings = []
        _scan_strings(state["raw"], strings)
        if saved in strings or saved in all_connectors(state):
            print(saved)
            return 0
    if virtuals:
        print(virtuals[0])
    return 0


def cmd_monitor_spec(args):
    connector = args[args.index("--connector") + 1]
    state = get_state()
    entry = monitor_entry(state, connector)
    if entry is None:
        # Shape fallback: look for the connector anywhere in the raw reply.
        strings = []
        _scan_strings(state["raw"], strings)
        if connector not in strings:
            return 1
        spec = []
    else:
        spec, _modes, _props = monitor_parts(entry)
    vendor = spec[1] if len(spec) > 1 and isinstance(spec[1], str) else "Meta"
    product = spec[2] if len(spec) > 2 and isinstance(spec[2], str) else "Virtual Monitor"
    serial = spec[3] if len(spec) > 3 and isinstance(spec[3], str) else "0x00000000"
    print("%s\t%s\t%s" % (vendor, product, serial))
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
    existing = set(all_connectors(state))
    existing_virtuals = set(virtual_connectors(state))
    if existing_virtuals:
        print("a virtual monitor already exists: %s" % ", ".join(sorted(existing_virtuals)), file=sys.stderr)
        return 4

    session_path = busctl_call_json(
        SCREENCAST_NAME, SCREENCAST_PATH, SCREENCAST_NAME, "CreateSession", "a{sv}", ["[]"],
    )
    if not session_path:
        raise RuntimeError("Mutter did not return a ScreenCast session")
    stream_path = busctl_call_json(
        SCREENCAST_NAME, session_path, SESSION_IFACE, "RecordVirtual", "a{sv}", ["[]"],
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
        new_connectors = [item for item in all_connectors(state) if item not in existing]
        new_virtuals = [item for item in new_connectors if looks_virtual(item)]
        candidates = new_virtuals or new_connectors
        if candidates:
            connector = candidates[0]
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
    except (RuntimeError, ValueError, IndexError, TypeError) as error:
        print("virtual monitor created, but placement failed: %s" % error, file=sys.stderr)

    exit_code = 0
    while not stopping["flag"]:
        time.sleep(1)
        try:
            state = get_state()
        except RuntimeError:
            continue
        strings = []
        _scan_strings(state["raw"], strings)
        if connector not in all_connectors(state) and connector not in strings:
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
        if command == "raw":
            return cmd_raw(args)
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
