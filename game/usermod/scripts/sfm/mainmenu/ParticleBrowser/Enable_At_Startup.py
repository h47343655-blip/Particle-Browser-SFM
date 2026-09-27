# Particle Browser - Install the menu hook on every SFM start (adds a block to <mod>/scripts/sfm/sfm_init.py).
import os as _pp_os
import sys as _pp_sys


def _pp_bootstrap():
    """Make sfm_particle_browser importable; SFM does not put scripts/sfm on sys.path."""
    candidates = []
    try:
        here = _pp_os.path.dirname(_pp_os.path.abspath(__file__))
        candidates += [here, _pp_os.path.dirname(here),
                       _pp_os.path.dirname(_pp_os.path.dirname(here))]
    except NameError:  # SFM may run scripts without __file__
        pass
    game = _pp_os.getcwd()  # SFM runs with SourceFilmmaker/game as the working directory
    try:
        mods = sorted(_pp_os.listdir(game))
    except OSError:
        mods = []
    for mod in ["usermod", "workshop"] + mods:
        candidates.append(_pp_os.path.join(game, mod, "scripts", "sfm"))
    for folder in candidates:
        if _pp_os.path.isfile(_pp_os.path.join(folder, "sfm_particle_browser", "__init__.py")):
            if folder not in _pp_sys.path:
                _pp_sys.path.insert(0, folder)
            return True
    print("[particle browser] package sfm_particle_browser not found, see README.")
    return False


if _pp_bootstrap():
    import sfm_particle_browser
    sfm_particle_browser.enable_startup()
