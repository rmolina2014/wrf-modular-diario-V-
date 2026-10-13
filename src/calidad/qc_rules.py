"""Reglas de Control de Calidad (QA/QC) para observaciones de superficie en San Juan."""

import logging
from typing import Dict, Any, Tuple
import pandas as pd
import numpy as np

logger = logging.getLogger("calidad.qc_rules")

# Límites climatológicos y físicos para el Valle de Tulum y precordillera de San Juan
LIMITES_FISICOS = {
    "temp": (-15.0, 50.0),            # °C
    "humedad": (1.0, 100.0),          # %
    "presion_absoluta": (750.0, 1050.0), # hPa (adecuado para 600m - 1000m s.n.m.)
    "presion_relativa": (850.0, 1080.0), # hPa
    "viento": (0.0, 150.0),           # km/h
    "viento_rafaga": (0.0, 200.0),    # km/h
    "direcc": (0.0, 360.0),           # Grados
    "rain_daily": (0.0, 300.0),       # mm
    "solar": (0.0, 1500.0),           # W/m2
}


def validar_limites_fisicos(valor: Any, variable: str) -> Tuple[bool, str]:
    """Evalúa si un valor numérico cae dentro del rango físico plausible."""
    if valor is None or pd.isna(valor):
        return True, "OK (None)"

    try:
        val_float = float(valor)
    except (ValueError, TypeError):
        return False, "Tipo no numérico"

    if variable in LIMITES_FISICOS:
        v_min, v_max = LIMITES_FISICOS[variable]
        if val_float < v_min or val_float > v_max:
            return False, f"Fuera de rango [{v_min}, {v_max}]: {val_float}"

    return True, "OK"


def aplicar_control_calidad(df: pd.DataFrame) -> pd.DataFrame:
    """Aplica control de calidad a un DataFrame de observaciones, marcando valores espurios como NaN."""
    df_qc = df.copy()

    for col, (v_min, v_max) in LIMITES_FISICOS.items():
        if col in df_qc.columns:
            # Coerción numérica
            s = pd.to_numeric(df_qc[col], errors="coerce")
            # Filtrar fuera de límites
            mascara_invalida = (s < v_min) | (s > v_max)
            num_invalidos = mascara_invalida.sum()
            if num_invalidos > 0:
                logger.warning(f"QC: Se anularon {num_invalidos} valores fuera de rango en columna '{col}'")
                s[mascara_invalida] = np.nan
            df_qc[col] = s

    return df_qc


def filtrar_observaciones_validas(df: pd.DataFrame, variables_clave: Tuple[str, ...] = ("temp", "presion_absoluta")) -> pd.DataFrame:
    """Retorna solo las filas que cuentan con al menos las variables meteorológicas esenciales."""
    mascara = pd.Series(True, index=df.index)
    for var in variables_clave:
        if var in df.columns:
            mascara = mascara & df[var].notna()
    return df[mascara].copy()
