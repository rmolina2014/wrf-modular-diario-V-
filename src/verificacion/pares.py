"""Construcción de pares modelo–observación para la verificación diferida."""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("verificacion.pares")

VARIABLES = {
    "t2_c": "T2m",
    "rh_pct": "RH2m",
    "psfc_hpa": "PSFC",
    "viento_kmh": "Viento10m",
    "direccion_grados": "DirViento10m",
}

RANGOS = {
    "temp_c": (-40.0, 55.0),
    "humedad_pct": (0.0, 100.0),
    "presion_absoluta_hpa": (400.0, 1100.0),
    "viento_kmh": (0.0, 250.0),
    "direcc_grados": (0.0, 360.0),
}


def cargar_catalogo(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def estaciones_holdout(catalogo: dict) -> set:
    return {n for n, meta in catalogo.items() if (meta or {}).get("rol") == "evaluacion"}


def leer_estado(caso_dir: Path) -> dict:
    p = Path(caso_dir) / "estado.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"No se pudo leer {p}: {e}")
        return {}


def inferir_t0(caso_dir: Path, asim_hours: int = 12) -> Optional[datetime]:
    """t0 desde estado.json o desde el nombre del caso (<fecha>_<ciclo>z)."""
    estado = leer_estado(caso_dir)
    if estado.get("t0"):
        return datetime.strptime(estado["t0"], "%Y-%m-%d %H:%M")
    partes = Path(caso_dir).name.split("_")
    try:
        fecha, ciclo = partes[0], partes[1].rstrip("z")
        return datetime.strptime(f"{fecha} {int(ciclo):02d}", "%Y-%m-%d %H") + timedelta(hours=asim_hours)
    except Exception:
        return None


def cargar_modelo(caso_dir: Path, t0: Optional[datetime] = None) -> pd.DataFrame:
    """CSV de valores del modelo con columnas normalizadas y lead/fase garantizados."""
    csv = Path(caso_dir) / "valores_pronostico_estaciones.csv"
    if not csv.exists():
        raise FileNotFoundError(f"No existe {csv}")
    df = pd.read_csv(csv)
    if "fecha_hora_utc" not in df.columns:
        raise ValueError(f"{csv.name} no tiene columna fecha_hora_utc")
    df["valid_utc"] = pd.to_datetime(df["fecha_hora_utc"], format="%Y-%m-%d_%H:%M:%S")
    if "lead_h" not in df.columns:
        if t0 is None:
            raise ValueError("El CSV no trae lead_h; indicá --t0 o un caso con estado.json")
        df["lead_h"] = (df["valid_utc"] - t0).dt.total_seconds() / 3600.0
    if "fase" not in df.columns:
        df["fase"] = np.where(df["lead_h"] <= 0, "asimilacion", "pronostico_libre")
    df["lead_h"] = pd.to_numeric(df["lead_h"], errors="coerce")
    return df


def _qc_obs(df: pd.DataFrame) -> pd.DataFrame:
    for col, (lo, hi) in RANGOS.items():
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
            df.loc[(df[col] < lo) | (df[col] > hi), col] = np.nan
    # Una estación sin temperatura ni viento no aporta a la verificación.
    validas = df[["temp_c", "viento_kmh"]].notna().any(axis=1)
    return df[validas].copy()


def cargar_obs(obs_dir: Path, inicio: datetime, fin: datetime,
               estaciones: Optional[Iterable[str]] = None) -> pd.DataFrame:
    """Histórico EcoWitt de `data/raw` dentro de [inicio, fin] (UTC)."""
    archivos = sorted(Path(obs_dir).glob("ecowitt_historico_*.csv"))
    if not archivos:
        raise FileNotFoundError(f"Sin históricos EcoWitt en {obs_dir}")

    frames = []
    for f in archivos:
        try:
            frames.append(pd.read_csv(f, usecols=lambda c: c in {
                "estacion", "hora_unix", "temp_c", "humedad_pct", "viento_kmh",
                "viento_rafaga_kmh", "direcc_grados", "presion_absoluta_hpa",
                "lluvia_diaria_mm"}))
        except Exception as e:
            logger.warning(f"No se pudo leer {f.name}: {e}")
    if not frames:
        raise ValueError(f"Ningún CSV de observaciones legible en {obs_dir}")

    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["estacion", "hora_unix"])
    df["ts"] = pd.to_datetime(df["hora_unix"], unit="s", utc=True).dt.tz_localize(None)
    df = df[(df["ts"] >= pd.Timestamp(inicio)) & (df["ts"] <= pd.Timestamp(fin))]

    if estaciones is not None:
        df = df[df["estacion"].isin(set(estaciones))]
    return _qc_obs(df)


def agregar_horario(obs: pd.DataFrame) -> pd.DataFrame:
    """Colapsa las obs de 10 min a medias horarias (viento vectorial)."""
    if obs.empty:
        return pd.DataFrame(columns=["estacion", "valid_utc", "t2_c", "rh_pct", "psfc_hpa",
                                     "viento_kmh", "direccion_grados", "n_obs"])
    df = obs.copy()
    df["valid_utc"] = df["ts"].dt.floor("h")
    base = df.groupby(["estacion", "valid_utc"]).agg(
        t2_c=("temp_c", "mean"),
        rh_pct=("humedad_pct", "mean"),
        psfc_hpa=("presion_absoluta_hpa", "mean"),
        n_obs=("temp_c", "size"),
    ).reset_index()

    v = df.dropna(subset=["viento_kmh", "direcc_grados"]).copy()
    if not v.empty:
        rad = np.deg2rad(v["direcc_grados"])
        v["u"] = -v["viento_kmh"] * np.sin(rad)
        v["w"] = -v["viento_kmh"] * np.cos(rad)
        w = v.groupby(["estacion", "valid_utc"]).agg(u=("u", "mean"), w=("w", "mean")).reset_index()
        w["viento_kmh"] = np.hypot(w["u"], w["w"])
        w["direccion_grados"] = (np.degrees(np.arctan2(-w["u"], -w["w"])) + 360.0) % 360.0
        base = base.merge(w[["estacion", "valid_utc", "viento_kmh", "direccion_grados"]],
                          on=["estacion", "valid_utc"], how="left")
    return base


def emparejar(modelo: pd.DataFrame, obs_horario: pd.DataFrame) -> pd.DataFrame:
    """Inner join por estación y hora exacta; conserva lead_h y fase del modelo."""
    cols = ["estacion", "valid_utc", "t2_c", "rh_pct", "psfc_hpa",
            "viento_kmh", "direccion_grados", "lead_h", "fase"]
    m = modelo[[c for c in cols if c in modelo.columns]].copy()
    o = obs_horario.rename(columns={c: f"obs_{c}" for c in
                                    ["t2_c", "rh_pct", "psfc_hpa", "viento_kmh", "direccion_grados"]})
    return m.merge(o, on=["estacion", "valid_utc"], how="inner")
