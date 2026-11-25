#!/bin/sh
# Install Picture Control Studio from this checkout (Linux): launcher, `ncp` command and app-menu entry.
# Needs: python3, PyGObject, GTK 4, libadwaita >= 1.5, numpy, Pillow, pycairo; LibRaw's dcraw_emu for RAW previews.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin"; APPS="$HOME/.local/share/applications"
mkdir -p "$BIN" "$APPS"

python3 - <<'PY' || { echo "Missing dependencies — see README.md (Run from source)."; exit 1; }
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk
import numpy, PIL, cairo
assert (Adw.MAJOR_VERSION, Adw.MINOR_VERSION) >= (1, 5), "libadwaita 1.5 or newer is required"
PY
command -v dcraw_emu >/dev/null || echo "Note: dcraw_emu (LibRaw) not found — NEF previews will use the camera JPEG."

chmod +x "$DIR/studio.py" "$DIR/ncp.py"
printf '#!/bin/sh\nexec python3 "%s/studio.py" "$@"\n' "$DIR" > "$BIN/picture-control-studio"
chmod +x "$BIN/picture-control-studio"
ln -sfn "$DIR/ncp.py" "$BIN/ncp"
cat > "$APPS/local.PictureControlStudio.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Picture Control Studio
Comment=Design Nikon Picture Controls, preview them on your NEFs and install them on the SD card
Exec=$BIN/picture-control-studio %F
Icon=camera-photo
Terminal=false
Categories=Graphics;Photography;
Keywords=nikon;picture control;fuji;film;ncp;np2;recipe;
MimeType=image/x-nikon-nef;image/jpeg;
StartupWMClass=local.PictureControlStudio
DESKTOP
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
echo "Installed: picture-control-studio, ncp, and an app-menu entry."
