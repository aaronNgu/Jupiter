# luminque.spec
#
# Build with:
#   pyinstaller luminque.spec
#
# Must be run on a Windows machine (or Windows GitHub Actions runner).
# Windows-specific binaries (pynput._win32, psutil._pswindows, etc.) are only
# available on Windows and will be missing from Mac builds.
#
# Output: dist\luminque\ directory. The CI pipeline wraps this into
# luminque-installer.exe using a 7-Zip SFX archive.
#
# Debugging tips:
#   - Set debug=True and console=True to surface hidden import errors.
#   - Run luminque.exe from a cmd.exe window and read the traceback.
#   - Add missing modules to hiddenimports, then revert debug/console flags.

from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

# ---------------------------------------------------------------------------
# Data files
# ---------------------------------------------------------------------------

datas = []
datas += collect_data_files("certifi")      # SSL certs used by requests

# ---------------------------------------------------------------------------
# Hidden imports
# ---------------------------------------------------------------------------
# PyInstaller cannot trace dynamic imports inside pynput, psutil, PIL, or
# SQLAlchemy. List them explicitly. Add entries as missing imports are
# discovered during test builds.

hidden_imports = [
    # SQLAlchemy — dialect selected at runtime
    "sqlalchemy.dialects.sqlite",
    "sqlalchemy.dialects.sqlite.pysqlite",
    "sqlalchemy.orm",

    # pynput — Windows input backend, not statically traceable
    "pynput.keyboard._win32",
    "pynput.mouse._win32",

    # Pillow — C extension loaded via importlib
    "PIL._imaging",
    "PIL.Image",

    # psutil — Windows-specific C extension
    "psutil._pswindows",
    "psutil._psutil_windows",

    # keyring — Windows Credential Manager backend
    "keyring.backends.Windows",

    # tkinter — may need explicit listing on some Python builds
    "tkinter",
    "tkinter.messagebox",

    # mss — Windows backend selected at runtime
    "mss.windows",
    "mss.windows.gdi",   # mss >= 10 split the backend into a package
]

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

a = Analysis(
    ["luminque/main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "spacy",        # not bundled in Phase 1
        "torch",
        "IPython",
        "jupyter",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ---------------------------------------------------------------------------
# EXE + COLLECT  (--onedir)
# ---------------------------------------------------------------------------
# EXE contains only the bootloader and Python scripts.
# COLLECT assembles the full dist\luminque\ directory alongside it.

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,           # binaries/datas live in COLLECT, not the exe.
                                     # Without this, EXE builds a onefile-style
                                     # binary straight into dist\ and COLLECT
                                     # fails with OSError [Errno 22] re-copying it.
    name="luminque",
    debug=False,                      # TEMPORARY — revert before release
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    console=False,                    # TEMPORARY — revert before release
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,                # None = match build machine (x64)
    codesign_identity=None,          # signing is done on luminque-installer.exe post-build
    entitlements_file=None,
    # icon="assets/luminque.ico",    # uncomment once assets/luminque.ico is created
    # version="version_info.txt",    # uncomment once version_info.txt is created
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="luminque",                 # produces dist\luminque\
)
