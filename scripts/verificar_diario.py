#!/usr/bin/env python
"""Verificación diferida de una corrida contra el histórico EcoWitt.

Se ejecuta cuando las observaciones del día objetivo ya existen (D+2 en UTC).
Usa el CSV `valores_pronostico_estaciones.csv` de la corrida y el histórico de
`data/raw`, y escribe métricas, un informe y una figura en `<caso>/verificacion/`.

Uso:
  python scripts/verificar_diario.py --caso outputs/2026-10-22_06z
  python scripts/verificar_diario.py --caso outputs/2026-10-22_06z --sin-holdout
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.verificacion import metricas as M  # noqa: E402
from src.verificacion import pares as P  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("verificacion")

CONTINUAS = ["t2_c", "rh_pct", "psfc_hpa", "viento_kmh"]
DIRECCION = "direccion_grados"


def _fmt(v, nd=2):
    return "—" if v is None or (isinstance(v, float) and not np.isfinite(v)) else f"{v:.{nd}f}"


def _metricas_var(df: pd.DataFrame, var: str) -> dict:
    if var not in df.columns or f"obs_{var}" not in df.columns:
        return {"n": 0, "bias": None, "mae": None, "rmse": None, "r": None}
    if var == DIRECCION:
        return M.resumen_direccion(df[f"obs_{var}"], df[var])
    return M.resumen(df[f"obs_{var}"], df[var])


def _filas_metricas(df: pd.DataFrame, conjunto: str, ciclo: str) -> list:
    filas = []
    base = {
        "conjunto": conjunto, "ciclo": ciclo,
    }
    for var in CONTINUAS + [DIRECCION]:
        if var not in df.columns or f"obs_{var}" not in df.columns:
            continue
        for lead, sub in df.groupby("lead_h"):
            r = _metricas_var(sub, var)
            filas.append({**base, "variable": var, "agrupacion": "todos", "estacion": "",
                          "lead_h": float(lead), **r})
            for est, s in sub.groupby("estacion"):
                r = _metricas_var(s, var)
                filas.append({**base, "variable": var, "agrupacion": "estacion", "estacion": est,
                              "lead_h": float(lead), **r})
    return filas


def _persistencia(df: pd.DataFrame, obs_horario: pd.DataFrame, t0: datetime) -> pd.DataFrame:
    """Reemplaza el modelo por la observación de t0 (o la primera disponible)."""
    if df.empty:
        return df
    hora_t0 = pd.Timestamp(t0).floor("h")
    filas = []
    for est, s in df.sort_values("valid_utc").groupby("estacion"):
        s = s.copy()
        previas = obs_horario[(obs_horario["estacion"] == est) &
                              (obs_horario["valid_utc"] <= hora_t0)].sort_values("valid_utc")
        if not previas.empty:
            ini = {var: previas.iloc[-1].get(var) for var in CONTINUAS + [DIRECCION]}
        else:
            ini = {var: s.iloc[0][f"obs_{var}"] for var in CONTINUAS + [DIRECCION]}
        for var in CONTINUAS + [DIRECCION]:
            if var in s.columns:
                s[var] = ini.get(var)
        filas.append(s)
    return pd.concat(filas, ignore_index=True)


def _climatologia(obs_dir: Path, inicio: datetime, estaciones: set, dias: int = 30):
    """Media por estación y hora del día con las obs anteriores al período objetivo."""
    desde = inicio - timedelta(days=dias)
    try:
        obs = P.cargar_obs(obs_dir, desde, inicio - timedelta(minutes=1), estaciones=estaciones)
    except Exception as e:
        logger.warning(f"Sin climatología: {e}")
        return None
    if obs.empty or obs["ts"].nunique() < 48 or obs["ts"].dt.normalize().nunique() < 3:
        return None
    obs = obs.copy()
    obs["hora"] = obs["ts"].dt.hour
    base = obs.groupby(["estacion", "hora"]).agg(
        t2_c=("temp_c", "mean"), rh_pct=("humedad_pct", "mean"),
        psfc_hpa=("presion_absoluta_hpa", "mean")).reset_index()
    v = obs.dropna(subset=["viento_kmh", "direcc_grados"]).copy()
    if not v.empty:
        rad = np.deg2rad(v["direcc_grados"])
        v["u"] = -v["viento_kmh"] * np.sin(rad)
        v["w"] = -v["viento_kmh"] * np.cos(rad)
        w = v.groupby(["estacion", "hora"]).agg(u=("u", "mean"), w=("w", "mean")).reset_index()
        w["viento_kmh"] = np.hypot(w["u"], w["w"])
        w["direccion_grados"] = (np.degrees(np.arctan2(-w["u"], -w["w"])) + 360.0) % 360.0
        base = base.merge(w[["estacion", "hora", "viento_kmh", "direccion_grados"]],
                          on=["estacion", "hora"], how="left")
    return base


def _aplicar_climatologia(df: pd.DataFrame, clima: pd.DataFrame) -> pd.DataFrame:
    if clima is None or df.empty:
        return pd.DataFrame()
    s = df.copy()
    s["hora"] = s["valid_utc"].dt.hour
    s = s.merge(clima, on=["estacion", "hora"], how="left", suffixes=("", "_clima"))
    for var in CONTINUAS + [DIRECCION]:
        if f"{var}_clima" in s.columns:
            s[var] = s[f"{var}_clima"]
    return s


def _extremos_ventana(m: pd.DataFrame, o: pd.DataFrame, ini: datetime,
                     fin: datetime, fecha, ventana: str):
    """Tmin/Tmax de una ventana si modelo y obs cubren ≥90 % de las horas."""
    esperadas = list(pd.date_range(pd.Timestamp(ini), pd.Timestamp(fin), freq="h", closed="left"))
    if not esperadas:
        return None
    horas_obs = set(o["ts"].dt.floor("h"))
    horas_mod = set(m["valid_utc"].dt.floor("h"))
    cob_obs = len(horas_obs & set(esperadas)) / len(esperadas)
    cob_mod = len(horas_mod & set(esperadas)) / len(esperadas)
    if cob_obs < 0.9 or cob_mod < 0.9 or m["t2_c"].notna().sum() < 2:
        return None
    return {
        "fecha": str(fecha), "ventana": ventana, "estacion": str(m["estacion"].iloc[0]),
        "tmin_modelo": float(m["t2_c"].min()),
        "tmin_obs": float(o["temp_c"].min()),
        "err_tmin": float(m["t2_c"].min() - o["temp_c"].min()),
        "tmax_modelo": float(m["t2_c"].max()),
        "tmax_obs": float(o["temp_c"].max()),
        "err_tmax": float(m["t2_c"].max() - o["temp_c"].max()),
        "cobertura": round(min(cob_obs, cob_mod), 3),
        "n_obs": int(len(o)),
    }


def _extremos(modelo: pd.DataFrame, obs: pd.DataFrame, inicio: datetime,
              fin: datetime) -> pd.DataFrame:
    """Tmin/Tmax por ventana SMN (00–12Z y 12–00Z), solo con cobertura completa."""
    filas = []
    dia = inicio.date()
    while dia <= fin.date():
        for nombre, hora_ini, hora_fin in (("00-12Z", 0, 12), ("12-00Z", 12, 24)):
            ini = datetime.combine(dia, datetime.min.time()) + timedelta(hours=hora_ini)
            term = datetime.combine(dia, datetime.min.time()) + timedelta(hours=hora_fin)
            if ini < inicio - timedelta(minutes=30) or term > fin + timedelta(hours=1):
                continue
            for est, m in modelo.groupby("estacion"):
                o = obs[(obs["estacion"] == est) & (obs["ts"] >= ini) & (obs["ts"] < term)]
                mm = m[(m["valid_utc"] >= ini) & (m["valid_utc"] < term)]
                if o.empty or mm.empty:
                    continue
                r = _extremos_ventana(mm, o, ini, term, dia, nombre)
                if r:
                    filas.append(r)
        dia += timedelta(days=1)
    return pd.DataFrame(filas)


def _lluvia_diaria(modelo: pd.DataFrame, obs: pd.DataFrame, inicio: datetime,
                   fin: datetime, huso: float = -3.0) -> pd.DataFrame:
    """Lluvia 24 h del día local: diferencia del acumulado del modelo y máximo diario EcoWitt."""
    if "lluvia_acum_mm" not in modelo.columns:
        return pd.DataFrame()
    filas = []
    dia = (inicio + timedelta(hours=huso)).date()
    ultimo = (fin + timedelta(hours=huso)).date()
    while dia <= ultimo:
        ini = datetime.combine(dia, datetime.min.time()) - timedelta(hours=huso)
        term = ini + timedelta(days=1)
        if ini < inicio or term > fin + timedelta(hours=1):
            dia += timedelta(days=1)
            continue
        for est, m in modelo.groupby("estacion"):
            s = m.set_index("valid_utc")["lluvia_acum_mm"]
            if ini not in s.index or term not in s.index:
                continue
            mod = float(s.loc[term] - s.loc[ini])
            o = obs[(obs["estacion"] == est) & (obs["ts"] >= ini) & (obs["ts"] < term)]
            if o.empty:
                continue
            horas = o["ts"].dt.floor("h").nunique()
            if horas < 22:
                continue
            filas.append({
                "fecha_local": str(dia), "estacion": est,
                "lluvia_modelo_mm": round(mod, 2),
                "lluvia_obs_mm": round(float(o["lluvia_diaria_mm"].max()), 2),
                "error_mm": round(mod - float(o["lluvia_diaria_mm"].max()), 2),
                "cobertura_horas": int(horas),
            })
        dia += timedelta(days=1)
    return pd.DataFrame(filas)


def _figura(df: pd.DataFrame, destino: Path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if "t2_c" not in df.columns:
        return None
    rmse = []
    for lead, s in df.groupby("lead_h"):
        r = M.resumen(s["obs_t2_c"], s["t2_c"])
        rmse.append((lead, r["rmse"]))
    if not rmse:
        return None
    rmse.sort()
    x = [r[0] for r in rmse]
    y = [r[1] for r in rmse]
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.plot(x, y, marker="o", color="#1f77b4")
    ax.set_xlabel("Plazo de pronóstico (h desde t0)")
    ax.set_ylabel("RMSE T2m (°C)")
    ax.set_title("RMSE de temperatura a 2 m por plazo")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    destino.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(destino, dpi=130)
    plt.close(fig)
    return destino


def _informe(destino: Path, meta: dict, df: pd.DataFrame, extremos: pd.DataFrame,
             lluvia: pd.DataFrame, baselines: dict):
    lineas = [
        f"# Verificación — {meta['caso']}",
        "",
        f"- **t0:** {meta['t0']:%Y-%m-%d %H:%M} UTC",
        f"- **Período verificado:** {meta['inicio']:%Y-%m-%d %H:%M} a {meta['fin']:%Y-%m-%d %H:%M} UTC",
        f"- **Conjunto:** {meta['conjunto']} ({meta['n_estaciones']} estaciones)",
        f"- **Pares horarios:** {len(df)}",
        "",
        "## Métricas globales",
        "",
        "| Variable | n | Bias | MAE | RMSE | r |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for var in CONTINUAS:
        r = _metricas_var(df, var)
        lineas.append(f"| {P.VARIABLES.get(var, var)} | {r['n']} | {_fmt(r['bias'])} | "
                      f"{_fmt(r['mae'])} | {_fmt(r['rmse'])} | {_fmt(r['r'], 3)} |")
    r = _metricas_var(df, DIRECCION)
    lineas.append(f"| {P.VARIABLES[DIRECCION]} | {r['n']} | {_fmt(r['bias'])} | {_fmt(r['mae'])} | — | — |")

    lineas += ["", "## T2m por plazo", "", "| Lead (h) | n | Bias | MAE | RMSE |", "|---:|---:|---:|---:|---:|"]
    for lead, s in df.groupby("lead_h"):
        r = _metricas_var(s, "t2_c")
        lineas.append(f"| {lead:g} | {r['n']} | {_fmt(r['bias'])} | {_fmt(r['mae'])} | {_fmt(r['rmse'])} |")

    if not extremos.empty:
        lineas += ["", "## Extremos por ventana SMN", "",
                   "| Fecha | Ventana | Estación | Tmin mod | Tmin obs | Error | Tmax mod | Tmax obs | Error |",
                   "|---|---|---|---:|---:|---:|---:|---:|---:|"]
        for _, r in extremos.iterrows():
            lineas.append(f"| {r['fecha']} | {r['ventana']} | {r['estacion']} | "
                          f"{_fmt(r['tmin_modelo'])} | {_fmt(r['tmin_obs'])} | {_fmt(r['err_tmin'])} | "
                          f"{_fmt(r['tmax_modelo'])} | {_fmt(r['tmax_obs'])} | {_fmt(r['err_tmax'])} |")

    if not lluvia.empty:
        lineas += ["", "## Lluvia 24 h (día local)", "",
                   "| Fecha | n | Bias (mm) | MAE (mm) | RMSE (mm) |", "|---|---:|---:|---:|---:|"]
        for fecha, sub in lluvia.groupby("fecha_local"):
            r = M.resumen(sub["lluvia_obs_mm"], sub["lluvia_modelo_mm"])
            lineas.append(f"| {fecha} | {r['n']} | {_fmt(r['bias'])} | {_fmt(r['mae'])} | {_fmt(r['rmse'])} |")

    if baselines:
        lineas += ["", "## Líneas base (RMSE T2m)", "", "| Lead (h) | Persistencia | Climatología | Modelo |",
                   "|---:|---:|---:|---:|"]
        for lead in sorted(df["lead_h"].unique()):
            vals = []
            for nombre in ("persistencia", "climatologia"):
                sub = baselines.get(nombre)
                vals.append(_fmt(sub.get(lead, {}).get("rmse")) if sub is not None else "—")
            r = _metricas_var(df[df["lead_h"] == lead], "t2_c")
            lineas.append(f"| {lead:g} | {vals[0]} | {vals[1]} | {_fmt(r['rmse'])} |")

    lineas += ["", "## Notas", "",
               "- Verificación diferida: las observaciones del día objetivo se descargan después de la corrida.",
               "- Viento observado y modelado a 10 m; la altura real del sensor EcoWitt puede diferir.",
               "- Tmin/Tmax se informan solo si la obs cubre ≥90 % de las horas del pronóstico; "
               "el modelo usa salida horaria y las obs datos de 10 min.",
               "- PSFC compara presión de superficie del punto de grilla con la presión absoluta "
               "de la estación; la diferencia de altura introduce un sesgo sistemático.",
               ""]
    destino.write_text("\n".join(lineas), encoding="utf-8")


def _rmse_por_lead(df: pd.DataFrame, var: str) -> dict:
    out = {}
    for lead, s in df.groupby("lead_h"):
        out[float(lead)] = _metricas_var(s, var)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--caso", type=Path, default=None, help="directorio outputs/<fecha>_<ciclo>z")
    ap.add_argument("--fecha", default=None, help="alternativa a --caso (YYYY-MM-DD)")
    ap.add_argument("--ciclo", default=None, help="alternativa a --caso (00/06/12/18)")
    ap.add_argument("--t0", default=None, help="t0 UTC (YYYY-MM-DD_HH:MM:SS) si el CSV no trae lead_h")
    ap.add_argument("--obs-dir", type=Path, default=ROOT / "data" / "raw")
    ap.add_argument("--estaciones-json", type=Path, default=ROOT / "config" / "estaciones.json")
    ap.add_argument("--salida", type=Path, default=None, help="default: <caso>/verificacion")
    ap.add_argument("--sin-holdout", action="store_true", help="verificar con todas las estaciones")
    ap.add_argument("--clima-dias", type=int, default=30)
    args = ap.parse_args()

    if args.caso is None:
        if not (args.fecha and args.ciclo):
            ap.error("indicá --caso o --fecha junto con --ciclo")
        args.caso = ROOT / "outputs" / f"{args.fecha}_{int(args.ciclo):02d}z"
    caso = args.caso.resolve()
    salida = (args.salida or (caso / "verificacion")).resolve()
    salida.mkdir(parents=True, exist_ok=True)

    t0_cli = datetime.strptime(args.t0, "%Y-%m-%d_%H:%M:%S") if args.t0 else None
    t0 = t0_cli or P.inferir_t0(caso)
    modelo = P.cargar_modelo(caso, t0=t0)
    if t0 is None:
        t0 = modelo.loc[modelo["fase"] == "asimilacion", "valid_utc"].max()
    if t0 is None or pd.isna(t0):
        raise SystemExit("No se pudo determinar t0")

    pron = modelo[modelo["lead_h"] >= 0].copy()
    if pron.empty:
        raise SystemExit("El CSV no tiene filas de pronóstico (lead_h >= 0)")
    inicio, fin = pron["valid_utc"].min().to_pydatetime(), pron["valid_utc"].max().to_pydatetime()

    catalogo = P.cargar_catalogo(args.estaciones_json)
    evaluacion = P.estaciones_holdout(catalogo)
    usar_holdout = bool(evaluacion) and not args.sin_holdout
    disponibles = set(modelo["estacion"].unique())
    if usar_holdout and not (evaluacion & disponibles):
        logger.warning("No hay estaciones de evaluación en el CSV; se verifica con todas (asimilado).")
        usar_holdout = False
    estaciones = (evaluacion & disponibles) if usar_holdout else disponibles
    conjunto = "holdout" if usar_holdout else "asimilado"

    obs = P.cargar_obs(args.obs_dir, inicio - timedelta(minutes=30),
                       fin + timedelta(hours=1), estaciones=estaciones)
    obs_h = P.agregar_horario(obs)
    df = P.emparejar(pron[pron["estacion"].isin(estaciones)], obs_h)
    if df.empty:
        raise SystemExit("No hay pares modelo–observación: revisar fechas, estaciones y data/raw")
    df = df.sort_values(["estacion", "valid_utc"])

    ciclo = str(P.leer_estado(caso).get("ciclo") or caso.name.split("_")[-1].rstrip("z")).zfill(2)
    filas = _filas_metricas(df, conjunto, ciclo)

    persist = _persistencia(df, obs_h, t0)
    if not persist.empty:
        filas += _filas_metricas(persist, "persistencia", ciclo)

    clima = _climatologia(args.obs_dir, inicio, set(estaciones), args.clima_dias)
    baselines = {}
    if clima is not None:
        clim = _aplicar_climatologia(df, clima)
        if not clim.empty:
            filas += _filas_metricas(clim, "climatologia", ciclo)

    tabla = pd.DataFrame(filas)
    tabla.to_csv(salida / "metricas.csv", index=False)

    pron_est = pron[pron["estacion"].isin(estaciones)]
    extremos = _extremos(pron_est, obs, inicio, fin)
    lluvia = _lluvia_diaria(pron_est, obs, inicio, fin)
    for nombre, tabla in (("extremos.csv", extremos), ("lluvia.csv", lluvia)):
        if tabla.empty:
            (salida / nombre).unlink(missing_ok=True)
        else:
            tabla.to_csv(salida / nombre, index=False)

    baselines = {
        "persistencia": _rmse_por_lead(persist, "t2_c") if not persist.empty else None,
        "climatologia": _rmse_por_lead(_aplicar_climatologia(df, clima), "t2_c") if clima is not None else None,
    }
    meta = {
        "caso": caso.name, "t0": t0, "inicio": inicio, "fin": fin,
        "conjunto": conjunto, "n_estaciones": int(df["estacion"].nunique()),
        "n_pares": int(len(df)), "ciclo": ciclo,
        "variables": {var: _metricas_var(df, var) for var in CONTINUAS + [DIRECCION]},
        "lluvia": (M.resumen(lluvia["lluvia_obs_mm"], lluvia["lluvia_modelo_mm"])
                   if not lluvia.empty else {"n": 0}),
    }
    resumen = {**meta, "t0": t0.isoformat(), "inicio": inicio.isoformat(), "fin": fin.isoformat(),
               "baselines_rmse_t2": {k: {str(l): v["rmse"] for l, v in (b or {}).items()}
                                     for k, b in baselines.items()}}
    (salida / "resumen.json").write_text(json.dumps(resumen, indent=1, ensure_ascii=False,
                                                    default=str), encoding="utf-8")
    _informe(salida / "informe.md", meta, df, extremos, lluvia, baselines)
    fig = _figura(df, salida / "figuras" / "rmse_t2_por_lead.png")

    logger.info(f"Verificación escrita en {salida} ({len(df)} pares, {conjunto})")
    for var in CONTINUAS:
        r = meta["variables"][var]
        logger.info(f"  {P.VARIABLES[var]}: n={r['n']} bias={_fmt(r['bias'])} "
                    f"mae={_fmt(r['mae'])} rmse={_fmt(r['rmse'])} r={_fmt(r['r'], 3)}")
    if fig:
        logger.info(f"  figura: {fig}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
