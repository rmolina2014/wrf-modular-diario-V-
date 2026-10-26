"""Carga y presentación de los valores del pronóstico.

Lo usan la app de Streamlit y el índice HTML estático, para que ambos muestren la misma
tabla: una fila por hora local y una columna por variable meteorológica.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from src.calidad.qc_rules import LIMITES_FISICOS

FMT_TIEMPO = "%Y-%m-%d_%H:%M:%S"
PATRON_CASO = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}z$")

ARCHIVO_ESTACIONES = "valores_pronostico_estaciones.csv"
ARCHIVO_DOMINIO = "valores_pronostico_dominio.csv"

FASES = {"asimilacion": "Asimilación", "pronostico_libre": "Pronóstico libre"}

COLUMNAS_ESTACION = {
    "t2_c": "T2 (°C)",
    "rh_pct": "HR (%)",
    "psfc_hpa": "PSFC (hPa)",
    "viento_kmh": "Viento (km/h)",
    "direccion_grados": "Dir. viento (°)",
    "lluvia_hora_mm": "Lluvia 1 h (mm)",
    "lluvia_acum_mm": "Lluvia acum. (mm)",
}

COLUMNAS_DOMINIO = {
    "t2_min_c": "T2 mín (°C)", "t2_max_c": "T2 máx (°C)", "t2_media_c": "T2 media (°C)",
    "rh_min_pct": "HR mín (%)", "rh_max_pct": "HR máx (%)", "rh_media_pct": "HR media (%)",
    "psfc_min_hpa": "PSFC mín (hPa)", "psfc_max_hpa": "PSFC máx (hPa)",
    "psfc_media_hpa": "PSFC media (hPa)",
    "viento_max_kmh": "Viento máx (km/h)", "viento_media_kmh": "Viento media (km/h)",
    "lluvia_acum_max_mm": "Lluvia acum. máx (mm)", "lluvia_acum_media_mm": "Lluvia acum. media (mm)",
}

# Variable de la tabla de estaciones -> clave de LIMITES_FISICOS (rangos del QC de observaciones).
_LIMITE = {"t2_c": "temp", "rh_pct": "humedad", "psfc_hpa": "presion_absoluta", "viento_kmh": "viento"}


def listar_casos(outputs_dir: Path) -> List[Path]:
    """Carpetas ``YYYY-MM-DD_HHz`` de ``outputs/``, de la más reciente a la más antigua."""
    outputs_dir = Path(outputs_dir)
    if not outputs_dir.is_dir():
        return []
    casos = [d for d in outputs_dir.iterdir() if d.is_dir() and PATRON_CASO.match(d.name)]
    return sorted(casos, key=lambda d: d.name, reverse=True)


def cargar_estado(caso_dir: Path) -> Dict[str, Any]:
    """Contenido de ``estado.json`` (vacío si no existe o está dañado)."""
    try:
        return json.loads((Path(caso_dir) / "estado.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def cargar_valores(caso_dir: Path, tipo: str = "estaciones") -> Optional[pd.DataFrame]:
    """Lee el CSV de valores del caso. ``tipo``: ``estaciones`` o ``dominio``. None si no existe."""
    nombre = ARCHIVO_ESTACIONES if tipo == "estaciones" else ARCHIVO_DOMINIO
    ruta = Path(caso_dir) / nombre
    if not ruta.exists():
        return None
    df = pd.read_csv(ruta)
    df["hora_local"] = pd.to_datetime(df["fecha_hora_local"], format=FMT_TIEMPO, errors="coerce")
    df["hora_utc"] = pd.to_datetime(df["fecha_hora_utc"], format=FMT_TIEMPO, errors="coerce")
    if tipo == "estaciones":
        df = df.sort_values(["estacion", "hora_utc"]).reset_index(drop=True)
        # La lluvia del CSV es acumulada desde el inicio del modelo; la de 1 h es su diferencia.
        dif = df.groupby("estacion")["lluvia_acum_mm"].diff()
        df["lluvia_hora_mm"] = dif.fillna(df["lluvia_acum_mm"]).clip(lower=0).round(2)
    else:
        df = df.sort_values("hora_utc").reset_index(drop=True)
    return df


def estaciones_disponibles(df: pd.DataFrame) -> List[str]:
    return sorted(df["estacion"].dropna().unique().tolist())


def filtrar(df: pd.DataFrame, estacion: Optional[str] = None,
            dia_objetivo: Optional[str] = None) -> pd.DataFrame:
    """Filas de una estación y/o del día local indicado (``YYYY-MM-DD``), ordenadas por hora."""
    out = df
    if estacion is not None and "estacion" in out.columns:
        out = out[out["estacion"] == estacion]
    if dia_objetivo:
        out = out[out["hora_local"].dt.strftime("%Y-%m-%d") == str(dia_objetivo)]
    return out.sort_values("hora_utc").reset_index(drop=True)


def fuera_de_rango(df: pd.DataFrame) -> pd.DataFrame:
    """Máscara booleana (mismo índice) de valores fuera del rango físico plausible.

    Sirve para señalar artefactos del modelo, p. ej. vientos de cientos de km/h.
    """
    mascara = pd.DataFrame(False, index=df.index, columns=list(_LIMITE))
    for col, clave in _LIMITE.items():
        if col in df.columns:
            vmin, vmax = LIMITES_FISICOS[clave]
            s = pd.to_numeric(df[col], errors="coerce")
            mascara[col] = (s < vmin) | (s > vmax)
    return mascara


def tabla_presentacion(df: pd.DataFrame, tipo: str = "estaciones") -> pd.DataFrame:
    """Tabla lista para mostrar: hora local, fase y las variables, con rótulos legibles."""
    columnas = COLUMNAS_ESTACION if tipo == "estaciones" else COLUMNAS_DOMINIO
    out = pd.DataFrame({
        "Hora local": df["hora_local"].dt.strftime("%d/%m %H:%M"),
        "Fase": df["fase"].map(FASES).fillna(df["fase"]),
    })
    for col, rotulo in columnas.items():
        if col in df.columns:
            out[rotulo] = df[col].values
    return out
