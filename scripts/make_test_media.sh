#!/usr/bin/env bash
# Build a small, clearly licensed test-media set for the cloud/Linux acceptance tests.
# Every file gets a provenance record (source, license, sha256) in provenance.json.
#
#   scripts/make_test_media.sh testmedia
#
# Sources (all installed from the Ubuntu archive):
#   footage : Freedoom (BSD-3-Clause) attract-mode demo playback, rendered by dsda-doom (GPL-2+)
#             and screen-recorded with FFmpeg under Xvfb, game audio captured via PulseAudio.
#   music   : Pingus tracker modules (pingus-data, GPL-2.0-or-later), decoded with libopenmpt.
#   sfx     : sound-theme-freedesktop (per-file CC-BY-3.0 / CC-BY-SA-3.0, see copyright file)
#             and Pingus sound effects (GPL-2.0-or-later).
# Linux only. It is not needed on the Mac: there you import your own recordings.
set -euo pipefail
OUT="${1:-testmedia}"
mkdir -p "$OUT/footage" "$OUT/music" "$OUT/sfx"
OUT="$(cd "$OUT" && pwd)"
WORK="$(mktemp -d)"
trap 'kill $(jobs -p) 2>/dev/null || true; rm -rf "$WORK"' EXIT

need() { command -v "$1" >/dev/null || { echo "missing: $1 (apt install $2)"; exit 1; }; }
need Xvfb xvfb; need pulseaudio pulseaudio; need ffmpeg ffmpeg; need /usr/games/dsda-doom dsda-doom

# ---- footage --------------------------------------------------------------
pulseaudio --check 2>/dev/null || pulseaudio --start --exit-idle-time=-1 2>/dev/null
pactl list short sinks | grep -q '^.*\srec\s' || pactl load-module module-null-sink sink_name=rec >/dev/null
pactl set-default-sink rec
DISP=":97"
Xvfb "$DISP" -screen 0 1280x720x24 >/dev/null 2>&1 &
sleep 2

record() { # wad seconds outfile
  DISPLAY="$DISP" SDL_AUDIODRIVER=pulseaudio timeout $(( $2 + 8 )) /usr/games/dsda-doom \
    -iwad "/usr/share/games/doom/$1" -width 1280 -height 720 -fullscreen -nomouse \
    >"$WORK/$1.log" 2>&1 &
  local game=$!
  sleep 3
  ffmpeg -y -loglevel error -f x11grab -framerate 30 -video_size 1280x720 -i "$DISP" \
    -f pulse -i rec.monitor -t "$2" -c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p \
    -c:a aac -b:a 160k -movflags +faststart "$3"
  wait "$game" 2>/dev/null || true   # only the game; a bare `wait` would block on Xvfb forever
}
[ -f "$OUT/footage/freedoom2_attract.mp4" ] || record freedoom2.wad 150 "$OUT/footage/freedoom2_attract.mp4"
[ -f "$OUT/footage/freedoom1_attract.mp4" ] || record freedoom1.wad 120 "$OUT/footage/freedoom1_attract.mp4"

# ---- music ----------------------------------------------------------------
PINGUS="$WORK/pingus"
if [ ! -f "$OUT/music/pingus_rough_journey.ogg" ]; then
  (cd "$WORK" && apt-get download -qq pingus-data >/dev/null 2>&1) && dpkg-deb -x "$WORK"/pingus-data_*.deb "$PINGUS"
  M="$PINGUS/usr/share/games/pingus/data/music"
  for t in rough_journey sorcerer the_big_march_in_space; do
    ffmpeg -y -loglevel error -i "$M/$t.it" -t 90 -ac 2 -ar 48000 -c:a libvorbis -q:a 6 "$OUT/music/pingus_$t.ogg"
  done
  for s in splash ohno chink plop; do
    ffmpeg -y -loglevel error -i "$PINGUS/usr/share/games/pingus/data/sounds/$s.wav" -ar 48000 "$OUT/sfx/pingus_$s.wav"
  done
fi
for s in bell complete camera-shutter dialog-warning device-added; do
  [ -f "$OUT/sfx/fd_$s.oga" ] || cp "/usr/share/sounds/freedesktop/stereo/$s.oga" "$OUT/sfx/fd_$s.oga"
done

# ---- provenance -----------------------------------------------------------
python3 - "$OUT" <<'PY'
import hashlib, json, os, sys
root = sys.argv[1]
lic = {
  "footage/freedoom2_attract.mp4": ("Freedoom Phase 2 attract-mode demo, recorded locally (dsda-doom 0.27.5)", "BSD-3-Clause (Freedoom assets)", "https://github.com/freedoom/freedoom"),
  "footage/freedoom1_attract.mp4": ("Freedoom Phase 1 attract-mode demo, recorded locally (dsda-doom 0.27.5)", "BSD-3-Clause (Freedoom assets)", "https://github.com/freedoom/freedoom"),
}
fd = {"bell": ("Dr. Richard Boulanger et al", "CC-BY-3.0"), "complete": ("Dr. Richard Boulanger et al", "CC-BY-3.0"),
      "camera-shutter": ("freesound user horsthorstensen", "CC-BY-SA-3.0"), "dialog-warning": ("sound-theme-freedesktop", "see /usr/share/doc/sound-theme-freedesktop/copyright"),
      "device-added": ("Ivica Bukvic", "CC-BY-SA-3.0")}
out = []
for dp, _, fs in os.walk(root):
    for f in sorted(fs):
        p = os.path.join(dp, f); rel = os.path.relpath(p, root)
        if rel == "provenance.json": continue
        h = hashlib.sha256(open(p, "rb").read()).hexdigest()
        if rel in lic: title, license_, url = lic[rel]; author = "Contributors to the Freedoom project"
        elif rel.startswith("music/pingus_") or rel.startswith("sfx/pingus_"):
            title = f"Pingus game data: {f}"; license_ = "GPL-2.0-or-later"; url = "https://pingus.seul.org (Debian pingus-data 0.7.6)"; author = "Ingo Ruhnke and Pingus contributors"
        elif rel.startswith("sfx/fd_"):
            k = f[3:-4]; author, license_ = fd.get(k, ("sound-theme-freedesktop", "unknown")); title = f"freedesktop sound theme: {k}"; url = "Debian sound-theme-freedesktop 0.8"
        else: continue
        out.append({"path": rel, "sha256": h, "title": title, "author": author, "license": license_, "source": url,
                    "evidence": "Debian package copyright file" })
json.dump(out, open(os.path.join(root, "provenance.json"), "w"), indent=2)
print(f"{len(out)} files recorded in provenance.json")
PY
