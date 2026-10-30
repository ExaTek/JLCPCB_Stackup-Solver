# JLCPCB Stackup Solver

Desktop tool that finds the cheapest **orderable JLCPCB stackup** for your impedances, computes
the trace widths and gaps for every signal layer, and exports the stack with the widths to
**Altium Designer** (`.stackup`, Windows) or **KiCad** (project, Linux/macOS). Works offline; an optional button checks the widths with
JLCPCB's own online Si9000 calculator. Interface in English and Russian.

![screenshot](docs/screenshot.png)

## Download and run

Grab the archive for your system from [Releases](../../releases), unpack it anywhere you can
write to and start the program. Nothing to install, no Python needed.

| system | archive | start |
|---|---|---|
| Windows 10/11 x64 | `JLCPCB-Stackup-Solver-windows-x64.zip` | `JLCPCB-Stackup-Solver.exe` (SmartScreen: *More info → Run anyway*) |
| Linux x64, glibc 2.35+ (Ubuntu 22.04+, Debian 12+, Fedora 36+) | `JLCPCB-Stackup-Solver-linux-x64.tar.gz` | `./JLCPCB-Stackup-Solver` |

```
JLCPCB-Stackup-Solver/
├── JLCPCB-Stackup-Solver(.exe)   the program
├── lib/                          Python, Qt, numpy/scipy
└── data/                         everything that changes
    ├── jlcpcb_stackups.zip       stack database: Settings → Update stack database
    ├── jlc_calc_cache.json       answers of JLC's online calculator
    └── settings.json, cache/, logs/   created by the program
```

Keep `data/` next to the program; to move to a new version, copy your `data/` over.

### From source (any OS with Python 3.10+)

```
git clone https://github.com/ExaTek/JLCPCB_Stackup-Solver && cd JLCPCB_Stackup-Solver
run.bat          # Windows
./run.sh         # Linux / macOS
```

The script creates `.venv` and installs `requirements.txt` on the first run
(by hand: `pip install -r requirements.txt`, then `python -m jlc`).

## What it does

- 4–10 layers, board thickness, outer/inner copper, layer roles (S / G / P) with presets.
- Impedance requirements: single-ended or differential, target Ω and tolerance, on which layers,
  auto-sized (smallest manufacturable W/S, JLC minimum 3.5/3.5 mil) or fixed geometry.
- Results grouped as *suitable / almost / unsuitable*, ranked by JLC's stackup surcharge
  (from the quote form, 5 pcs 100×100 mm); filter for stackups without surcharge.
- Windows: Altium export (Layer Stack Manager → File → Load Stackup): layers, dielectrics,
  solder mask, finish, impedance profiles `S50`, `DIFF90`, … with widths on every signal layer.
- Linux/macOS: KiCad 7+ project — `.kicad_pcb` with the stackup, `.kicad_pro` with net classes
  `S50`, `DIFF90`, …, `.kicad_dru` with the width and pair gap of every class on every signal layer.
  Open it, or pull it into your board with Board Setup → Import Settings.
- Stack database refresh from JLCPCB in Settings; mil ↔ mm; RU ↔ EN.

The offline field solver matches JLC's Si9000 within ±0.3 % (see [details](docs/details.ru.md)).

## Repository

| folder | |
|---|---|
| `jlc/core` | stackup model, field solver, width search |
| `jlc/jlcpcb` | JLCPCB online API, stack database update, Si9000 answer cache |
| `jlc/export` | Altium `.stackup` and KiCad project writers |
| `jlc/ui` | Qt window, RU/EN texts |
| `jlc/cli.py` | command line |
| `data` | stack database and JLC answers shipped with the program |
| `packaging` | PyInstaller spec |
| `docs` | details (in Russian), screenshot, example export |

## Command line

```
python -m jlc list   --layers 8 --thick 1.6 [--inner 0.5] [--free]
python -m jlc widths JLC081611-1080B [--se 40,50] [--diff 80,90,100] [--online]
python -m jlc update                 # refresh data/jlcpcb_stackups.zip (~7 min)
python -m jlc --help
```

## Build

```
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm packaging/JLCPCB-Stackup-Solver.spec    # -> dist/JLCPCB-Stackup-Solver/
dist/JLCPCB-Stackup-Solver/JLCPCB-Stackup-Solver --selftest     # headless check, exit code 0 = ok
```

GitHub Actions builds and self-tests both versions on every push; a `v*` tag publishes a release.

## Notes

Not affiliated with or endorsed by JLCPCB. The stack database is public data from JLCPCB's
impedance calculator and quote form; prices and stackups change, so check the final order on
jlcpcb.com.

---

**Кратко по-русски.** Программа подбирает самый дешёвый заказываемый стек JLCPCB под нужные
импедансы, считает ширины и зазоры на всех сигнальных слоях и сохраняет стек с профилями
импеданса для Altium Designer (Windows) или проект KiCad (Linux/macOS). Скачайте архив для своей системы в [Releases](../../releases),
распакуйте и запустите — установка не нужна. Папка `data/` рядом с программой хранит базу стеков
(обновляется в «Настройках»), настройки, кэш и журналы. Интерфейс переключается кнопкой RU/EN.
Подробности — в [docs/details.ru.md](docs/details.ru.md).

## License

[MIT](LICENSE)
