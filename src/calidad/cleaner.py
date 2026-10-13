"""Limpieza, normalización y exportación de observaciones meteorológicas."""

import os
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple
import pandas as pd

from .qc_rules import aplicar_control_calidad

logger = logging.getLogger("calidad.cleaner")


class ObservacionesCleaner:
    """Procesa, valida y exporta observaciones crudas a formatos estructurados."""

    def __init__(self, output_dir: str = "data/processed"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def procesar_json(self, ruta_archivo: str) -> pd.DataFrame:
        """Carga y procesa un archivo JSON de observaciones crudas."""
        ruta = Path(ruta_archivo)
        if not ruta.exists():
            raise FileNotFoundError(f"Archivo no encontrado: {ruta_archivo}")

        with open(ruta, "r", encoding="utf-8") as f:
            data = json.load(f)

        logger.info(f"Cargados {len(data)} registros de {ruta.name}")
        valid_data = [obs for obs in data if isinstance(obs, dict) and "error" not in obs]
        if not valid_data:
            raise ValueError("No se encontraron registros válidos de estaciones en el JSON")

        df = pd.DataFrame(valid_data)

        # Normalización de timestamp
        if "fecha" in df.columns and "hora" in df.columns:
            df["timestamp"] = pd.to_datetime(df["fecha"].astype(str) + " " + df["hora"].astype(str), errors="coerce")
        elif "fecha_hora" in df.columns:
            df["timestamp"] = pd.to_datetime(df["fecha_hora"], errors="coerce")
            df["fecha"] = df["timestamp"].dt.strftime("%Y-%m-%d")
            df["hora"] = df["timestamp"].dt.strftime("%H:%M")

        # Aplicación de filtros QA/QC
        df_qc = aplicar_control_calidad(df)
        logger.info(f"Limpieza completada: {len(df_qc)} registros procesados.")
        return df_qc

    def procesar_csv_historico(self, ruta_csv: str) -> pd.DataFrame:
        """Carga y normaliza un archivo CSV histórico de EcoWitt."""
        ruta = Path(ruta_csv)
        if not ruta.exists():
            raise FileNotFoundError(f"CSV histórico no encontrado: {ruta_csv}")

        df = pd.read_csv(ruta)

        columnas_map = {
            "temp_c": "temp",
            "humedad_pct": "humedad",
            "viento_kmh": "viento",
            "viento_rafaga_kmh": "viento_rafaga",
            "direcc_grados": "direcc",
            "presion_relativa_hpa": "presion_relativa",
            "presion_absoluta_hpa": "presion_absoluta",
            "lluvia_diaria_mm": "rain_daily",
            "solar_wm2": "solar",
        }
        df = df.rename(columns=columnas_map)

        if "fecha_hora" in df.columns:
            df["timestamp"] = pd.to_datetime(df["fecha_hora"], errors="coerce")
            df["fecha"] = df["timestamp"].dt.strftime("%Y-%m-%d")
            df["hora"] = df["timestamp"].dt.strftime("%H:%M")

        df_qc = aplicar_control_calidad(df)
        return df_qc

    def guardar_procesado(self, df: pd.DataFrame, nombre_base: Optional[str] = None) -> Tuple[Path, Path]:
        """Exporta el DataFrame depurado a CSV y Excel."""
        if not nombre_base:
            nombre_base = datetime.now().strftime("%Y%m%d_%H%M")

        csv_path = self.output_dir / f"datos_validados_{nombre_base}.csv"
        df.to_csv(csv_path, index=False, encoding="utf-8")

        excel_path = self.output_dir / f"datos_validados_{nombre_base}.xlsx"
        df.to_excel(excel_path, index=False, engine="openpyxl")

        logger.info(f"Archivos guardados en: {csv_path.name} y {excel_path.name}")
        return csv_path, excel_path
