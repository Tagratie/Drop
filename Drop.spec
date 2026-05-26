# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_submodules

datas = [('visualizer', 'visualizer')]
binaries = [('ffmpeg.exe', '.'), ('yt-dlp.exe', '.')]
hiddenimports = ['moderngl', 'glfw', 'scipy', 'scipy.ndimage', 'soundcard', 'sounddevice', 'PIL.Image', 'PIL.ImageDraw', 'PIL.ImageFont']
datas += collect_data_files('moderngl')
binaries += collect_dynamic_libs('soundcard')
binaries += collect_dynamic_libs('sounddevice')
hiddenimports += collect_submodules('vlc')


a = Analysis(
    ['drop.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    name='Drop',
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
