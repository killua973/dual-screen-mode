#!/usr/bin/env bash

set -u

root_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
test_root="$(mktemp -d)"
fake_bin="$test_root/bin"
fake_state="$test_root/state"
test_home="$test_root/home"
runtime_dir="$test_root/runtime"
toggle_pid=""
mkdir -p "$fake_bin" "$fake_state" "$test_home" "$runtime_dir"

cleanup() {
    if [[ -n "$toggle_pid" ]]; then
        kill "$toggle_pid" 2>/dev/null || true
    fi
    rm -rf "$test_root"
}
trap cleanup EXIT

cat > "$fake_bin/busctl" <<'EOF'
#!/usr/bin/env bash
json=0
while [[ "${1:-}" == --* ]]; do
    [[ "$1" == *json* ]] && json=1
    shift
done
[[ "${1:-}" == "call" ]] || exit 1
shift
dest="$1"; path="$2"; iface="$3"; method="$4"
shift 4
case "$iface.$method" in
    org.gnome.Mutter.DisplayConfig.GetCurrentState)
        if [[ $json == 1 ]]; then
            if [[ -f "$FAKE_STATE/vmon-created" ]]; then
                printf '%s\n' '{"type":"x","data":[7,[[["eDP-1","BOE","0x0a1b","0x00000000"],[["1920x1080@60.000",1920,1080,60.0,1.0,[1.0],{"is-current":true,"is-preferred":true}]],{}],[["Meta-0","Meta","Virtual Monitor","0x00000000"],[["1280x800@60.000",1280,800,60.0,1.0,[1.0],{"is-current":true,"is-preferred":true}]],{}]],[[0,0,1.0,0,true,[["eDP-1","BOE","0x0a1b","0x00000000"]],{}],[1920,1080,1.0,0,false,[["Meta-0","Meta","Virtual Monitor","0x00000000"]],{}]],{}]}'
            else
                printf '%s\n' '{"type":"x","data":[7,[[["eDP-1","BOE","0x0a1b","0x00000000"],[["1920x1080@60.000",1920,1080,60.0,1.0,[1.0],{"is-current":true,"is-preferred":true}]],{}]],[[0,0,1.0,0,true,[["eDP-1","BOE","0x0a1b","0x00000000"]],{}]],{}]}'
            fi
        fi
        ;;
    org.gnome.Mutter.ScreenCast.CreateSession)
        [[ $json == 1 ]] && printf '%s\n' '{"type":"o","data":"/org/gnome/Mutter/ScreenCast/Session/1"}'
        ;;
    org.gnome.Mutter.ScreenCast.Session.RecordVirtual)
        touch "$FAKE_STATE/vmon-created"
        [[ $json == 1 ]] && printf '%s\n' '{"type":"o","data":"/org/gnome/Mutter/ScreenCast/Stream/1"}'
        ;;
    org.gnome.Mutter.ScreenCast.Session.Start)
        touch "$FAKE_STATE/vmon-started"
        ;;
    org.gnome.Mutter.ScreenCast.Session.Close)
        touch "$FAKE_STATE/vmon-closed"
        rm -f "$FAKE_STATE/vmon-created"
        ;;
    org.gnome.Mutter.DisplayConfig.ApplyMonitorsConfig)
        printf '%s\n' "$*" >> "$FAKE_STATE/apply.log"
        ;;
esac
exit 0
EOF

cat > "$fake_bin/gst-launch-1.0" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_STATE/consumer.log"
trap 'exit 0' TERM INT
while :; do sleep 1; done
EOF

cat > "$fake_bin/sunshine" <<'EOF'
#!/usr/bin/env bash
touch "$FAKE_STATE/sunshine"
trap 'rm -f "$FAKE_STATE/sunshine"; exit 0' TERM INT EXIT
while :; do sleep 1; done
EOF

cat > "$fake_bin/flatpak" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF

cat > "$fake_bin/gsettings" <<'EOF'
#!/usr/bin/env bash
state_key="$(printf '%s' "$2" | tr '/:' '__')"
case "${1:-}" in
    get)
        cat "$FAKE_STATE/gsettings/$state_key" 2>/dev/null || printf '%s\n' "['', '', '']"
        ;;
    set)
        mkdir -p "$FAKE_STATE/gsettings"
        printf '%s\n' "$4" > "$FAKE_STATE/gsettings/$state_key"
        printf '%s\n' "$2 $3 $4" >> "$FAKE_STATE/gsettings.log"
        ;;
    reset)
        rm -f "$FAKE_STATE/gsettings/$state_key"
        printf '%s\n' "$2 $3 reset" >> "$FAKE_STATE/gsettings.log"
        ;;
esac
EOF

cat > "$fake_bin/libinput" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "list-devices" ]]; then
    printf '%s\n' 'Device: Sunshine Virtual Touchscreen' 'Kernel: /dev/input/event28' ''
fi
EOF

cat > "$fake_bin/udevadm" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "info" ]]; then
    printf '%s\n' 'ID_VENDOR_ID=1234' 'ID_MODEL_ID=5678'
fi
EOF

cat > "$fake_bin/pactl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_STATE/pactl.log"
case "${1:-}" in
    get-default-sink) printf '%s\n' 'test-sink' ;;
    get-sink-volume) printf '%s\n' 'Volume: front-left: 65536 / 100% / 0.00 dB, front-right: 65536 / 100% / 0.00 dB' ;;
    get-sink-mute) printf 'Mute: %s\n' "$(cat "$FAKE_STATE/mute" 2>/dev/null || printf no)" ;;
    set-sink-mute) [[ "$3" == 1 ]] && printf '%s\n' yes > "$FAKE_STATE/mute" || printf '%s\n' no > "$FAKE_STATE/mute" ;;
    set-sink-volume) printf '%s\n' "$3" > "$FAKE_STATE/volume" ;;
esac
EOF

chmod +x "$fake_bin"/*
mkdir -p "$test_home/.config/dual-screen-mode" "$test_home/.config/sunshine" "$test_home/.local/share/dual-screen-mode"
cp "$root_dir/gnome_vmon.py" "$test_home/.local/share/dual-screen-mode/gnome_vmon.py"
printf '%s\n' 'resolution=1280x800' 'audio_mode=host' > "$test_home/.config/dual-screen-mode/settings.conf"
printf '%s\n' 'stream_audio = enabled' > "$test_home/.config/sunshine/sunshine.conf"

run_toggle() {
    HOME="$test_home" \
    XDG_RUNTIME_DIR="$runtime_dir" \
    FAKE_STATE="$fake_state" \
    XDG_CURRENT_DESKTOP="GNOME" \
    DUAL_SCREEN_DESKTOP="gnome" \
    PATH="$fake_bin:$PATH" \
        bash "$root_dir/dual-screen-mode.sh"
}

fail() {
    echo "FAIL $1" >&2
    exit 1
}

run_toggle
[[ -f "$runtime_dir/dual-screen-mode/active" ]] || fail "gnome mode did not activate"
[[ -f "$fake_state/vmon-created" ]] || fail "mutter virtual monitor was not created"
[[ -f "$fake_state/vmon-started" ]] || fail "screencast session was not started"
[[ -f "$fake_state/consumer.log" ]] || fail "pipewire consumer was not started"
grep -Eq 'width=1280,height=800' "$fake_state/consumer.log" || fail "consumer did not request 1280x800"
[[ "$(cat "$runtime_dir/dual-screen-mode/vmonitor.connector" 2>/dev/null)" == "Meta-0" ]] || fail "connector file does not name Meta-0"
grep -Fq 'capture = portal' "$test_home/.config/sunshine/sunshine.conf" || fail "sunshine capture was not set to portal"
grep -Fq 'output_name = Meta-0' "$test_home/.config/sunshine/sunshine.conf" || fail "sunshine output_name was not set to Meta-0"
grep -Eq '320, 1080' "$fake_state/apply.log" 2>/dev/null || fail "virtual monitor was not placed centered below the main display"
[[ "$(cat "$runtime_dir/dual-screen-mode/touch-status" 2>/dev/null)" == "mapped" ]] || fail "touch was not mapped"
grep -Fq "['Meta', 'Virtual Monitor', '0x00000000']" "$fake_state/gsettings.log" || fail "gsettings touch mapping did not target the virtual monitor"
[[ -f "$fake_state/sunshine" ]] || fail "native sunshine was not started"

run_toggle
[[ ! -f "$runtime_dir/dual-screen-mode/active" ]] || fail "gnome mode did not deactivate"
[[ -f "$fake_state/vmon-closed" ]] || fail "screencast session was not closed on disable"
[[ ! -f "$runtime_dir/dual-screen-mode/vmonitor.connector" ]] || fail "connector file was not removed"
grep -Fq 'stream_audio = enabled' "$test_home/.config/sunshine/sunshine.conf" || fail "original sunshine audio setting was not restored"
if grep -Eq '^[[:space:]]*capture[[:space:]]*=' "$test_home/.config/sunshine/sunshine.conf"; then
    fail "capture key was not removed on disable"
fi
if grep -Eq '^[[:space:]]*output_name[[:space:]]*=' "$test_home/.config/sunshine/sunshine.conf"; then
    fail "output_name key was not removed on disable"
fi
[[ ! -f "$runtime_dir/dual-screen-mode/touch-mappings" ]] || fail "touch mappings were not restored"
tail -n 1 "$fake_state/gsettings.log" | grep -Fq "output ['', '', '']" || fail "gsettings touch mapping was not restored to its previous value"

echo "PASS gnome wayland toggle lifecycle, placement, touch routing, and sunshine config"
