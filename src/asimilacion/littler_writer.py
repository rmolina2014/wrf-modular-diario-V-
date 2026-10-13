"""Generador de archivos en formato Little_R (ASCII Fortran) para asimilación WRF."""

import json
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Tuple, Optional
import pandas as pd

logger = logging.getLogger("asimilacion.littler")


class LittleRWriter:
    """Conversor de observaciones a formato Little_R."""

    def __init__(self, config_estaciones_path: Optional[str] = None):
        self.config_path = Path(config_estaciones_path or "config/estaciones.json")
        self.metadatos = self._cargar_metadatos()

    def _cargar_metadatos(self) -> Dict[str, Dict[str, Any]]:
        if self.config_path.exists():
            with open(self.config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        logger.warning(f"No se encontró archivo de metadatos en {self.config_path}")
        return {}

    def _u_v_desde_viento(self, velocidad_kmh: Optional[float], direccion_deg: Optional[float]) -> Tuple[float, float]:
        """Convierte velocidad (km/h) y dirección meteorológica (grados) a componentes u, v (m/s)."""
        if velocidad_kmh is None or direccion_deg is None or pd.isna(velocidad_kmh) or pd.isna(direccion_deg):
            return -888888.0, -888888.0

        vel_ms = float(velocidad_kmh) / 3.6
        rad = math.radians(float(direccion_deg))
        u = -vel_ms * math.sin(rad)
        v = -vel_ms * math.cos(rad)
        return u, v

    def generar_littler(self, df: pd.DataFrame, output_path: Optional[str] = None) -> Tuple[Path, int]:
        """Convierte un DataFrame de observaciones validadas en un archivo Little_R."""
        if not output_path:
            ts = datetime.now().strftime("%Y%m%d_%H%M")
            output_path = f"data/processed/littler_{ts}.txt"

        dst_path = Path(output_path)
        dst_path.parent.mkdir(parents=True, exist_ok=True)

        total_escritas = 0
        with open(dst_path, "w", encoding="utf-8") as f:
            for _, fila in df.iterrows():
                nombre = str(fila.get("estacion", "UNKNOWN"))
                meta = self.metadatos.get(nombre, {})

                lat = float(meta.get("lat", fila.get("lat", 0.0)))
                lon = float(meta.get("lon", fila.get("lon", 0.0)))
                elev = float(meta.get("elev", fila.get("elev", 0.0)))

                fecha = str(fila.get("fecha", datetime.now().strftime("%Y-%m-%d")))
                hora = str(fila.get("hora", "00:00"))
                hora_completa = hora + ":00" if len(hora) == 5 else hora

                # Conversión de unidades
                temp_val = fila.get("temp")
                temp_k = float(temp_val) + 273.15 if pd.notna(temp_val) else -888888.0

                rh_val = fila.get("humedad")
                rh = float(rh_val) if pd.notna(rh_val) else -888888.0

                p_val = fila.get("presion_absoluta")
                if pd.isna(p_val):
                    p_val = fila.get("presion_relativa")
                presion_pa = float(p_val) * 100.0 if pd.notna(p_val) else -888888.0

                u, v = self._u_v_desde_viento(fila.get("viento"), fila.get("direcc"))

                # 1. Cabecera
                header = f"{nombre:40s}{lat:12.5f}{lon:12.5f}{elev:12.5f} {fecha} {hora_completa}     1"
                f.write(header + "\n")

                # 2. Datos en superficie (Nivel 1 de 1)
                linea_datos = (
                    f"     1     1"
                    f"{temp_k:15.3f}"
                    f"{rh:15.3f}"
                    f"{presion_pa:15.3f}"
                    f"{u:15.3f}"
                    f"{v:15.3f}"
                    f"      -888888.0"
                    f"      -888888.0"
                )
                f.write(linea_datos + "\n")

                # 3. Fin de registro
                f.write("     0     0\n")
                total_escritas += 1

        logger.info(f"Archivo Little_R generado en {dst_path} con {total_escritas} registros.")
        return dst_path, total_escritas
