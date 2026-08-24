if __package__:
    from .app import main
else:  # PyInstaller analyses the entry file as a script.
    from pathlib import Path
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from canhost.app import main

main()
