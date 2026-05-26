# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_dynamic_libs

binaries = []
binaries += collect_dynamic_libs('soundcard')
binaries += collect_dynamic_libs('sounddevice')
# glfw3.dll is loaded by the glfw package via ctypes — PyInstaller's
# auto-scanner doesn't catch ctypes loads, so pull it in explicitly.
binaries += collect_dynamic_libs('glfw')


a = Analysis(
    ['visualizer\\main.py'],
    pathex=['visualizer'],
    binaries=binaries,
    datas=[('visualizer/shaders', 'shaders')],
    hiddenimports=[],
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
    name='visualizer',
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
    icon=['drop.ico'],
)
