# -*- mode: python ; coding: utf-8 -*-
import sys

# pynput and pystray pick their platform backend at runtime, so PyInstaller can't see it
if sys.platform == 'win32':
    pynput_backends = ['pynput.keyboard._win32', 'pynput.mouse._win32', 'pystray._win32']
elif sys.platform == 'darwin':
    pynput_backends = ['pynput.keyboard._darwin', 'pynput.mouse._darwin', 'pystray._darwin']
else:
    pynput_backends = ['pynput.keyboard._xorg', 'pynput.mouse._xorg', 'pystray._xorg']

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('Letters', 'Letters'), ('AutoWisp_Icon.ico', '.')],
    hiddenimports=pynput_backends,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='AutoWisp',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['AutoWisp_Icon.ico'],
)

if sys.platform == 'darwin':
    app = BUNDLE(
        exe,
        name='AutoWisp.app',
        icon='icon.icns',
        bundle_identifier=None,
    )
