# PyInstaller spec for Picture Control Studio (Linux, Windows, macOS).
# Build from the repository root:  pyinstaller packaging/picture-control-studio.spec
import os
import shutil
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
dcraw = shutil.which("dcraw_emu")  # LibRaw's RAW developer, used for NEF previews
binaries = [(dcraw, "bin")] if dcraw else []
if not dcraw:
    print("WARNING: dcraw_emu not found - packaged app will fall back to the camera JPEG for NEFs")

a = Analysis(
    [os.path.join(ROOT, "studio.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=[(os.path.join(ROOT, "recipes.toml"), ".")],
    hiddenimports=["ncp"],
    hooksconfig={
        "gi": {
            "icons": ["Adwaita"],
            "themes": ["Adwaita"],
            "module-versions": {"Gtk": "4.0", "Gdk": "4.0", "Adw": "1", "Graphene": "1.0", "Gsk": "4.0"},
        },
    },
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="PictureControlStudio",
          console=False, icon=None)
coll = COLLECT(exe, a.binaries, a.datas, name="PictureControlStudio")
if sys.platform == "darwin":
    app = BUNDLE(coll, name="Picture Control Studio.app", bundle_identifier="local.PictureControlStudio",
                 info_plist={"NSHighResolutionCapable": True})
