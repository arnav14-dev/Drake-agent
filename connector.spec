# PyInstaller spec for the Fynn Drake connector.
#
#   pip install pyinstaller
#   pyinstaller --clean --noconfirm connector.spec
#   -> dist/FynnConnector.exe   (one file, no Python needed on the firm's PC)
#
# THE HIDDEN IMPORTS ARE NOT OPTIONAL.
# -----------------------------------
# `agent._load_form_map` resolves a Drake screen to its field-map module through
# `importlib.import_module(name)`. PyInstaller reads `import` statements; it cannot see a
# module named by a string at runtime. Left out, the exe builds cleanly, starts cleanly,
# pairs cleanly, polls cleanly — and then dies with ModuleNotFoundError on the first real
# document, AFTER claiming the job, which marks it running and blocks the firm.
#
# `case_connector_exe_carries_every_form` in simulate_headsdown.py compares this list
# against `agent._FORMS`, so adding a seventh form cannot silently ship a broken build.

block_cipher = None

FORM_MAP_MODULES = [
    'w2_map',      # W2   — Wages
    'int_map',     # INT  — Interest income
    'div_map',     # DIV  — Dividend income
    'r_map',       # 1099 — Retirement (1099-R)
    'ssa_map',     # SSA  — Social Security benefits
    'm1098_map',   # 1098 — Mortgage interest
]

a = Analysis(
    ['connector.py'],
    pathex=['.'],
    binaries=[],
    # binding.json describes DRAKE's UI (window title pattern, screen codes, navigation
    # keys) — not this machine — so it ships inside the exe. A copy placed beside the exe
    # still wins, for a firm on a Drake build that needs a tweak before we can release one.
    datas=[('binding.json', '.')],
    hiddenimports=[
        *FORM_MAP_MODULES,
        'agent',          # the entry path; connector.py imports it lazily inside cmd_run
        'drake_driver',
        'drake_nav',
        'form_plan',
        'protocol',
        'win32cred',      # the token store
        'win32timezone',  # pywin32 pulls this in at runtime, not at import
        'tkinter',        # setup window for a person with no terminal
        'tray',           # the tray icon and the log file; imported inside cmd_run
        'pystray',        # both are imported lazily and behind try/except, so a missing
        'PIL',            # one costs an icon, never a document — but ship them anyway
        'PIL.Image',
        'PIL.ImageDraw',
        'pystray._win32', # pystray picks its backend at runtime; PyInstaller sees none
    ],
    hookspath=[],
    runtime_hooks=[],
    # Nothing here needs a scientific stack or a test harness; excluding them keeps the
    # download small enough to email to an office.
    excludes=['numpy', 'pandas', 'matplotlib', 'pytest', 'simulate_headsdown', 'mutants'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='FynnConnector',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    # NO CONSOLE WINDOW — but only because both halves of its replacement now exist.
    #
    # The console was kept deliberately while this was new: it types into live tax returns
    # and an operator being able to watch beat a tidy desktop. It was always a bad
    # permanent answer, because a console window looks closeable and closing it kills the
    # connector silently, mid-batch, with the office assuming it is still running.
    #
    # `tray.py` replaces it with the two things that actually mattered: a tray icon whose
    # colour is the current state, and a log file holding every line both this and the
    # agent print — which outlives the window and answers "what did it type?".
    # `case_connector_exe_carries_every_form` enforces the pairing: this may only be False
    # while a tray module ships inside the exe.
    console=False,
    icon=None,
)
