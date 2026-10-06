# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['agent_gui.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('autocorrect_whitelist.txt', '.'),
    ],
hiddenimports=[
    'pystray._win32',
    'PIL._tkinter_finder',
    'customtkinter',

    # TTS / audio
    'edge_tts',
    'edge_tts.communicate',
    'edge_tts.constants',
    'edge_tts.drm',
    'edge_tts.exceptions',
    'edge_tts.models',
    'edge_tts.srt_utils',
    'edge_tts.submaker',
    'edge_tts.typing',
    'edge_tts.util',
    'aiohttp',
    'certifi',
    'sounddevice',
    'soundfile',
    'numpy',
    'pydub',
], Q  Q
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib',
        'scipy',
        'pandas',
        'IPython',
        'jupyter',
        'notebook',
        'pytest',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AgentBot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
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
    upx=False,
    upx_exclude=[],
    name='AgentBot',
)