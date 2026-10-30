"""python -m jlc              the desktop app
python -m jlc <command>    the command line (list, show, widths, z, rank, update, prices, validate)
Also the entry script of the packaged build (packaging/JLCPCB-Stackup-Solver.spec)."""
import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()        # worker processes of the packaged build
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        from jlc.cli import main
    else:
        from jlc.ui.app import main
    main()
