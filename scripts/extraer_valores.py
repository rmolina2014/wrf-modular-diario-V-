#!/usr/bin/env python
"""Extrae los valores del pronóstico (wrfout) a CSV legible.

Genera dos archivos junto a los wrfout:
  - valores_pronostico_estaciones.csv : T2, RH, PSFC, viento 10 m y lluvia
    acumulada (RAINC+RAINNC) en las estaciones del catálogo (punto de retícula
    más cercano) por tiempo válido.
  - valores_pronostico_dominio.csv    : mín/máx/media de las mismas variables
    sobre toda la malla del dominio por tiempo válido.

Uso:
  python scripts/extraer_valores.py --nudged-dir outputs/<caso>/nudged \
      --estaciones-json config/estaciones.json --output outputs/<caso>
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr


def rh_desde_q(q: np.ndarray, t_k: np.ndarray, p_pa: np.ndarray) -> np.ndarray:
    """Humedad relativa (%) a partir de mezcla específica, temperatura y presión."""
    tc = t_k - 273.15
    es = 611.2 * np.exp(17.67 * tc / (tc + 243.5))
    e = q * p_pa / (0.622 + 0.378 * q)
    return np.clip(e / es * 100.0, 0.0, 100.0)


def viento(u: np.ndarray, v: np.ndarray):
    """Rapidez (km/h) y dirección meteorológica de proveniencia (grados)."""
    spd_ms = np.hypot(u, v)
    dir_grados = (np.degrees(np.arctan2(-u, -v)) + 360.0) % 360.0
    return spd_ms * 3.6, dir_grados


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--nudged-dir", required=True, type=Path)
    ap.add_argument("--estaciones-json", default=Path("config/estaciones.json"), type=Path)
    ap.add_argument("--output", required=True, type=Path,
                    help="directorio de salida de los CSV")
    ap.add_argument("--t0", default=None,
                    help="fin de la asimilación, UTC (YYYY-MM-DD_HH:MM:SS). Si se indica, agrega "
                         "las columnas lead_h y fase (asimilacion / pronostico_libre).")
    ap.add_argument("--huso-local", type=float, default=-3.0,
                    help="huso (horas respecto de UTC) de la columna fecha_hora_local (default: -3)")
    args = ap.parse_args()
    t0 = datetime.strptime(args.t0, "%Y-%m-%d_%H:%M:%S") if args.t0 else None

    wrfout = sorted(p for p in args.nudged_dir.glob("wrfout_d01_*")
                    if re.search(r"\d{4}-\d{2}-\d{2}_\d{2}:\d{2}:\d{2}$", p.name))
    if not wrfout:
        print(f"Sin wrfout en {args.nudged_dir}", file=sys.stderr)
        return 1

    estaciones = json.loads(args.estaciones_json.read_text(encoding="utf-8"))
    if isinstance(estaciones, dict):
        estaciones = [dict(nombre=nombre, **datos)
                      for nombre, datos in estaciones.items()]

    args.output.mkdir(parents=True, exist_ok=True)
    filas_est, filas_dom = [], []

    for archivo in wrfout:
        marca = archivo.name.removeprefix("wrfout_d01_")
        dt = datetime.strptime(marca, "%Y-%m-%d_%H:%M:%S")
        tiempo = {"fecha_hora_utc": marca,
                  "fecha_hora_local": (dt + timedelta(hours=args.huso_local)).strftime("%Y-%m-%d_%H:%M:%S")}
        if t0 is not None:
            lead = (dt - t0).total_seconds() / 3600.0
            tiempo["lead_h"] = round(lead, 1)
            tiempo["fase"] = "asimilacion" if lead <= 0 else "pronostico_libre"
        with xr.open_dataset(archivo) as ds:
            t2 = ds["T2"].values
            q2 = ds["Q2"].values
            psfc = ds["PSFC"].values
            u10 = ds["U10"].values
            v10 = ds["V10"].values
            lluvia = None
            if "RAINC" in ds.variables and "RAINNC" in ds.variables:
                lluvia = ds["RAINC"].values + ds["RAINNC"].values
            # frames_per_outfile=1: un solo tiempo por wrfout
            if t2.ndim == 3:
                t2, q2, psfc, u10, v10 = (v[0] for v in (t2, q2, psfc, u10, v10))
                if lluvia is not None:
                    lluvia = lluvia[0]
            lat = ds["XLAT"].values
            lon = ds["XLONG"].values
            if lat.ndim == 3:
                lat, lon = lat[0], lon[0]
            rh = rh_desde_q(q2, t2, psfc)
            spd, dirg = viento(u10, v10)

            filas_dom.append({
                **tiempo,
                "t2_min_c": round(float(np.nanmin(t2) - 273.15), 2),
                "t2_max_c": round(float(np.nanmax(t2) - 273.15), 2),
                "t2_media_c": round(float(np.nanmean(t2) - 273.15), 2),
                "rh_min_pct": round(float(np.nanmin(rh)), 1),
                "rh_max_pct": round(float(np.nanmax(rh)), 1),
                "rh_media_pct": round(float(np.nanmean(rh)), 1),
                "psfc_min_hpa": round(float(np.nanmin(psfc)) / 100.0, 1),
                "psfc_max_hpa": round(float(np.nanmax(psfc)) / 100.0, 1),
                "psfc_media_hpa": round(float(np.nanmean(psfc)) / 100.0, 1),
                "viento_max_kmh": round(float(np.nanmax(spd)), 1),
                "viento_media_kmh": round(float(np.nanmean(spd)), 1),
                **({"lluvia_acum_max_mm": round(float(np.nanmax(lluvia)), 1),
                    "lluvia_acum_media_mm": round(float(np.nanmean(lluvia)), 2)}
                   if lluvia is not None else {}),
            })

            for est in estaciones:
                dist2 = ((lat - float(est["lat"])) ** 2
                         + (lon - float(est["lon"])) ** 2)
                j, i = np.unravel_index(int(np.nanargmin(dist2)), dist2.shape)
                filas_est.append({
                    **tiempo,
                    "estacion": est.get("nombre", est.get("estacion", "?")),
                    "lat": est["lat"],
                    "lon": est["lon"],
                    "t2_c": round(float(t2[j, i]) - 273.15, 2),
                    "rh_pct": round(float(rh[j, i]), 1),
                    "psfc_hpa": round(float(psfc[j, i]) / 100.0, 1),
                    "viento_kmh": round(float(spd[j, i]), 1),
                    "direccion_grados": round(float(dirg[j, i]), 0),
                    **({"lluvia_acum_mm": round(float(lluvia[j, i]), 1)}
                       if lluvia is not None else {}),
                })
        print(f"  {archivo.name}: ok")

    df_est = pd.DataFrame(filas_est).sort_values(["estacion", "fecha_hora_utc"])
    df_dom = pd.DataFrame(filas_dom).sort_values("fecha_hora_utc")
    salida_est = args.output / "valores_pronostico_estaciones.csv"
    salida_dom = args.output / "valores_pronostico_dominio.csv"
    df_est.to_csv(salida_est, index=False)
    df_dom.to_csv(salida_dom, index=False)
    print(f"CSV estaciones: {salida_est} ({len(df_est)} filas)")
    print(f"CSV dominio:    {salida_dom} ({len(df_dom)} filas)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
