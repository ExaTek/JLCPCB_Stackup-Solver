"""Start the JLCPCB Stackup Solver window (double-click on Windows, `python jlcapp.pyw` anywhere).
Also the entry script of the packaged build (JLCPCB-Stackup-Solver.spec)."""
import multiprocessing
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__":
    multiprocessing.freeze_support()        # worker processes of the packaged build
    from jlc.app import main
    main()
