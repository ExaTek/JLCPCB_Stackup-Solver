# JLCPCB Stackup Solver

Desktop tool that finds the cheapest **orderable JLCPCB stackup** for your impedances, computes
the trace widths and gaps for every signal layer, and exports the stack with impedance profiles
to **Altium Designer** (`.stackup`). Works offline; an optional button checks the widths with
JLCPCB's own online Si9000 calculator. Interface in English and Russian.

![screenshot](docs/screenshot.png)

## Download and run

Grab the file for your system from [Releases](../../releases). Nothing to install, no Python needed.

| system | file | start |
|---|---|---|
| Windows 10/11 x64 | `JLCPCB-Stackup-Solver-windows-x64.exe` | double-click (if SmartScreen asks: *More info → Run anyway*) |
| Linux x64 (glibc 2.35+: Ubuntu 22.04+, Debian 12+, Fedora 36+) | `JLCPCB-Stackup-Solver-linux-x64.tar.gz` | `tar xzf JLCPCB-Stackup-Solver-linux-x64.tar.gz && ./JLCPCB-Stackup-Solver` |

The first start takes a few seconds (the single file unpacks itself). Settings, caches and logs
go to `%LOCALAPPDATA%\JLCPCB-Stackup-Solver` (Windows) or `~/.local/share/JLCPCB-Stackup-Solver` (Linux).

### From source (any OS with Python 3.10+)

```
git clone <this repository> && cd JLCPCB_Stackup-Solver
run.bat          # Windows
./run.sh         # Linux / macOS
```

The script creates `.venv` and installs `requirements.txt` on the first run. Or by hand:
`pip install -r requirements.txt` and `python jlcapp.pyw`.

## What it does

- 4–10 layers, board thickness, outer/inner copper, layer roles (S / G / P) with presets.
- Impedance requirements: single-ended or differential, target Ω and tolerance, on which layers,
  auto-sized (smallest manufacturable W/S, JLC minimum 3.5/3.5 mil) or fixed geometry.
- Results grouped as *suitable / almost / unsuitable*, ranked by JLC's stackup surcharge
  (from the quote form, 5 pcs 100×100 mm); filter for stackups without surcharge.
- Altium export: layers, dielectrics, solder mask, finish, impedance profiles `S50`, `DIFF90`, …
  with widths on every signal layer.
- mil ↔ mm, RU ↔ EN.

The offline field solver matches JLC's Si9000 within ±0.3 % (see [details](docs/details.ru.md)).

## Command line

```
python jlcstack.py list   --layers 8 --thick 1.6 [--inner 0.5] [--free]
python jlcstack.py widths JLC081611-1080B [--se 40,50] [--diff 80,90,100] [--online]
python jlcstack.py update                 # refresh the JLC data in data/ (~7 min)
```

## Build the single-file app

```
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm JLCPCB-Stackup-Solver.spec      # -> dist/JLCPCB-Stackup-Solver[.exe]
dist/JLCPCB-Stackup-Solver --selftest                   # headless check, exit code 0 = ok
```

GitHub Actions builds and self-tests both versions on every push; a `v*` tag publishes a release.

## Notes

Not affiliated with or endorsed by JLCPCB. The stackup tables in `data/` are public data from
JLCPCB's impedance calculator and quote form; prices and stackups change, so check the final
order on jlcpcb.com.

---

**Кратко по-русски.** Программа подбирает самый дешёвый заказываемый стек JLCPCB под нужные
импедансы, считает ширины и зазоры на всех сигнальных слоях и сохраняет стек с профилями
импеданса для Altium Designer. Скачайте файл для своей системы в [Releases](../../releases) и
запустите, установка не нужна. Интерфейс переключается на русский кнопкой RU/EN.
Подробности — в [docs/details.ru.md](docs/details.ru.md).

## License

[MIT](LICENSE)
