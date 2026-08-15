# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


project_root = Path(SPECPATH).resolve().parents[1]
datas = collect_data_files("serverpilot")
datas.append((str(project_root / "desktop" / "assets" / "ServerPilot Icon.png"), "desktop/assets"))
datas.append((str(project_root / "desktop" / "windows" / "ui"), "desktop/windows/ui"))

a = Analysis(
    [str(project_root / "desktop" / "windows_launcher.py")],
    pathex=[str(project_root / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=collect_submodules("webview") + [
        "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.lifespan.on",
        "uvicorn.lifespan.off",
        "jinja2.ext",
        "tkinter",
        "tkinter.messagebox",
        "clr",
        "pythonnet",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "ruff"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ServerPilot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=str(project_root / "desktop" / "assets" / "ServerPilot Icon.png"),
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="ServerPilot",
)
