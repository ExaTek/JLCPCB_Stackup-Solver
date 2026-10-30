"""python -m jlc: the desktop app (same as jlcapp.pyw)."""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from jlc.app import main
    main()
