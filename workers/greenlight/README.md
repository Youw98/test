# GreenLight-Gym2 worker

This directory is a **separate program** from KasFlex core. It is the only place in
the repository that imports [`gl-gym`](https://github.com/BartvLaatum/GreenLight-Gym2).

## Why it is separate

| Reason | Detail |
|---|---|
| Licence | `gl-gym` is **AGPL-3.0-or-later**. Code linked into it inherits that obligation. KasFlex core never imports it, so the core stays Apache-2.0. |
| Dependencies | `gl-gym` pins `numpy<2.0`. `power-grid-model` requires `numpy>=2.0`. The two cannot coexist in one environment. |

`worker.py` is distributed under **AGPL-3.0-or-later** (see the SPDX header in the
file). It imports nothing from KasFlex, so the copyleft surface is exactly this
file plus its own virtual environment.

## Setup

```bash
python3 -m venv .venv-greenlight
./.venv-greenlight/bin/pip install -r workers/greenlight/requirements.txt
```

Then point KasFlex at it:

```bash
export KASFLEX_GREENLIGHT_PYTHON=$PWD/.venv-greenlight/bin/python
kasflex run --config configs/scenario_westland_winter.yaml --greenhouse greenlight
```

## Protocol

One JSON object per line on stdin, one response per line on stdout.

```json
{"cmd": "simulate_day", "plan": {...}, "floor_area_m2": 50000, "seed": 0,
 "scenario": {"location": "Amsterdam", "growth_year": 2010, "start_day": 59}}
```

```json
{"ok": true, "outcome": {"heat_demand_kw": [...], "temp_c": [...], ...}}
```

Errors come back as `{"ok": false, "error": "...", "traceback": "..."}`; the worker
never dies on a bad request.
