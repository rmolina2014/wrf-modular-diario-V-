#!/usr/bin/env python3
"""Producto visual del pronóstico diario a partir de los wrfout (corrida nudged).

Genera una lámina por tiempo válido con T2 (C), RH (%), PSFC (hPa) y viento a 10 m
(barbs + velocidad), para el dominio San Juan. Opcionalmente superpone el valor
observado de las estaciones cuando se pasa --obs-json.

Uso:
    python scripts/generar_producto.py \
        --nudged-dir outputs/2026-10-13_12z/nudged \
        --output outputs/2026-10-13_12z/productos \
        [--obs-json ...] [--estaciones-json config/estaciones.json] \
        [--valid-time 2026-10-14_00:00:00]   # repetible; default: todos los wrfout
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import xarray as xr
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def _t2_c(t2_k):
    return t2_k - 273.15


def _rh(ds):
    q2 = ds["Q2"].values[0]
    t2 = ds["T2"].values[0]
    psfc = ds["PSFC"].values[0]
    es = 611.2 * np.exp(17.67 * (t2 - 273.15) / (t2 - 29.65))
    qs = 0.622 * es / (psfc - 0.378 * es)
    rh = (q2 / qs) * 100.0
    return np.clip(rh, 0, 100)


def _viento(ds):
    u = ds["U10"].values[0]
    v = ds["V10"].values[0]
    return u, v, np.sqrt(u ** 2 + v ** 2)


def _formato_ejes(ax, lon2d, lat2d):
    ax.set_xlim(lon2d.min(), lon2d.max())
    ax.set_ylim(lat2d.min(), lat2d.max())
    ax.set_xlabel("Longitud", fontsize=8)
    ax.set_ylabel("Latitud", fontsize=8)
    ax.tick_params(labelsize=7)


def _superponer_obs(ax, obs_list, var, factor=1.0):
    if not obs_list:
        return
    for o in obs_list:
        val = o.get(var)
        if val is None:
            continue
        ax.plot(o["lon"], o["lat"], "ko", ms=6, mfc="none", zorder=5)
        ax.annotate(f"{val * factor:.1f}", (o["lon"], o["lat"]),
                    textcoords="offset points", xytext=(6, 6), fontsize=7, zorder=6)


def _panel_t2(ax, ds, obs):
    t2 = _t2_c(ds["T2"].values[0])
    lon2d, lat2d = ds["XLONG"].values[0], ds["XLAT"].values[0]
    hgt = ds["HGT"].values[0]
    cf = ax.contourf(lon2d, lat2d, t2, levels=40, cmap="RdYlBu_r")
    ax.contour(lon2d, lat2d, hgt, levels=10, colors="grey", linewidths=0.4, alpha=0.5)
    ax.set_title("T2 (C)", fontsize=11)
    _superponer_obs(ax, obs, "temp")
    _formato_ejes(ax, lon2d, lat2d)
    return cf


def _panel_rh(ax, ds, obs):
    rh = _rh(ds)
    lon2d, lat2d = ds["XLONG"].values[0], ds["XLAT"].values[0]
    cf = ax.contourf(lon2d, lat2d, rh, levels=20, cmap="BrBG")
    ax.set_title("RH (%)", fontsize=11)
    _superponer_obs(ax, obs, "rh")
    _formato_ejes(ax, lon2d, lat2d)
    return cf


def _panel_psfc(ax, ds, obs):
    psfc = ds["PSFC"].values[0] / 100.0
    lon2d, lat2d = ds["XLONG"].values[0], ds["XLAT"].values[0]
    cf = ax.contourf(lon2d, lat2d, psfc, levels=30, cmap="YlGnBu")
    ax.set_title("PSFC (hPa)", fontsize=11)
    _superponer_obs(ax, obs, "psfc")
    _formato_ejes(ax, lon2d, lat2d)
    return cf


def _panel_viento(ax, ds, obs):
    u, v, wspd = _viento(ds)
    lon2d, lat2d = ds["XLONG"].values[0], ds["XLAT"].values[0]
    cf = ax.contourf(lon2d, lat2d, wspd, levels=25, cmap="Purples")
    stride = 7
    ax.barbs(lon2d[::stride, ::stride], lat2d[::stride, ::stride],
             u[::stride, ::stride], v[::stride, ::stride],
             length=4.5, alpha=0.8, linewidth=0.6)
    ax.set_title("Viento 10 m (m/s)", fontsize=11)
    _superponer_obs(ax, obs, "speed")
    _formato_ejes(ax, lon2d, lat2d)
    return cf


def _etiqueta_tiempo(vt, huso_h):
    """'2026-10-23_03:00:00' -> '2026-10-23 03:00 UTC (2026-10-23 00:00 hora local)'."""
    from datetime import datetime, timedelta

    t = datetime.strptime(vt, "%Y-%m-%d_%H:%M:%S")
    local = t + timedelta(hours=huso_h)
    return f"{t:%Y-%m-%d %H:%M} UTC ({local:%Y-%m-%d %H:%M} hora local)"


def _cargar_obs(obs_json, estaciones_json, valid_time, ventana_min=30):
    if not obs_json or not valid_time:
        return []
    import json as _json
    from datetime import datetime, timedelta, timezone

    with open(obs_json, "r", encoding="utf-8") as f:
        obs = _json.load(f)
    with open(estaciones_json, "r", encoding="utf-8") as f:
        meta = _json.load(f)

    vt = datetime.strptime(valid_time, "%Y-%m-%d_%H:%M:%S").replace(tzinfo=timezone.utc)
    marcas = {}
    for r in obs:
        try:
            ts = datetime.fromtimestamp(int(r["time_unix"]), tz=timezone.utc)
        except (KeyError, TypeError, ValueError):
            continue
        if abs((ts - vt).total_seconds()) <= ventana_min * 60:
            marcas[r["estacion"]] = r
    out = []
    for nombre, r in marcas.items():
        m = meta.get(nombre, {})
        out.append({
            "name": nombre, "lat": m.get("lat"), "lon": m.get("lon"), "elev": m.get("elev"),
            "temp": _num(r.get("temp")),
            "rh": _num(r.get("humedad")),
            "psfc": _num(r.get("presion_absoluta"), 0) or _num(r.get("presion_relativa")),
            "speed": _num(r.get("viento")),
        })
    return out


def _num(v, d=None):
    if v is None:
        return d
    try:
        return float(v)
    except (ValueError, TypeError):
        return d


def principal():
    p = argparse.ArgumentParser(description="Producto de pronóstico desde wrfout (mapas por tiempo válido).")
    p.add_argument("--nudged-dir", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--obs-json", default=None)
    p.add_argument("--estaciones-json", default="config/estaciones.json")
    p.add_argument("--huso-local", type=float, default=-3.0,
                   help="Huso (horas respecto de UTC) para el rótulo de hora local (default: -3).")
    p.add_argument("--valid-time", action="append", default=None,
                   help="Un tiempo válido (YYYY-MM-DD_HH:MM:SS). Repetible; default: todos los wrfout.")
    args = p.parse_args()

    nudged = Path(args.nudged_dir)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    wrfouts = sorted(nudged.glob("wrfout_d01_*"))
    if not wrfouts:
        print(f"No hay wrfout en {nudged}", file=sys.stderr)
        return 1
    if args.valid_time:
        seleccion = []
        for t in args.valid_time:
            w = nudged / f"wrfout_d01_{t}"
            if w.exists():
                seleccion.append(w)
        if not seleccion:
            print(f"No existe wrfout para {args.valid_time}", file=sys.stderr)
            return 1
        wrfouts = seleccion

    for wrfout in wrfouts:
        vt = wrfout.name.replace("wrfout_d01_", "")
        obs = _cargar_obs(args.obs_json, args.estaciones_json, vt) if args.obs_json else []
        ds = xr.open_dataset(str(wrfout))
        fig, axes = plt.subplots(2, 2, figsize=(13, 10))
        fig.suptitle(f"WRF San Juan 15 km - {_etiqueta_tiempo(vt, args.huso_local)}  "
                     f"[{'obs superpuestas' if obs else 'pronóstico'}]",
                     fontsize=13, y=0.98)
        cfs = [
            _panel_t2(axes[0, 0], ds, obs),
            _panel_rh(axes[0, 1], ds, obs),
            _panel_psfc(axes[1, 0], ds, obs),
            _panel_viento(axes[1, 1], ds, obs),
        ]
        for ax, cf in zip(axes.ravel(), cfs):
            fig.colorbar(cf, ax=ax, shrink=0.75, pad=0.03)
        fig.tight_layout(rect=[0, 0, 1, 0.95])
        dest = out / f"producto_{vt.replace(':', '').replace('-', '')}.png"
        fig.savefig(str(dest), dpi=140, bbox_inches="tight")
        plt.close(fig)
        ds.close()
        print(f"-> {dest}  (obs: {len(obs)})")

    imagenes = sorted(out.glob("producto_*.png"))
    try:
        # Índice con la tabla por hora/variable (lee los CSV del ciclo, que viven junto a productos/).
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from src.ui.html_tabla import construir_indice_html
        pagina = construir_indice_html(out.resolve().parent, imagenes)
    except Exception as e:  # el índice nunca debe romper la generación de mapas
        print(f"Aviso: índice con tabla no disponible ({e}); se escribe la galería simple.", file=sys.stderr)
        pagina = ("<html><head><meta charset='utf-8'></head><body style='font-family:sans-serif'>"
                  "<h1>Productos WRF - San Juan (pronóstico diario)</h1><ul>"
                  + "".join(f"<li><a href='{p.name}'><img src='{p.name}' width='320'></a></li>" for p in imagenes)
                  + "</ul></body></html>")
    (out / "indice.html").write_text(pagina, encoding="utf-8")
    print("Listo.")
    return 0


if __name__ == "__main__":
    sys.exit(principal())