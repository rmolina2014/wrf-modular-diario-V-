"""Descargador de datos de pronóstico global GFS 0.25° (NOMADS / AWS S3)."""

import os
import logging
from pathlib import Path
from datetime import datetime
from typing import List, Optional
import requests

logger = logging.getLogger("ingesta.gfs")


class GFSDownloader:
    """Descargador de archivos GRIB2 GFS 0.25 grados para inicialización WPS."""

    def __init__(self, output_dir: str = "data/gfs"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def descargar_ciclo(
        self,
        fecha: str,  # YYYY-MM-DD
        ciclo_hora: str = "00",  # "00", "06", "12", "18"
        horas_pronostico: Optional[List[int]] = None,  # [0, 3, 6, 9, 12]
        fuente: str = "aws",  # "aws" o "nomads"
    ) -> List[Path]:
        """Descarga los archivos GRIB2 de un ciclo GFS."""
        if horas_pronostico is None:
            horas_pronostico = list(range(0, 13, 3))  # 0h a 12h cada 3h

        dt = datetime.strptime(fecha, "%Y-%m-%d")
        fecha_nodash = dt.strftime("%Y%m%d")
        archivos_descargados = []

        for fhour in horas_pronostico:
            fstr = f"{fhour:03d}"
            filename = f"gfs.t{ciclo_hora}z.pgrb2.0p25.f{fstr}"
            dst_path = self.output_dir / filename

            if dst_path.exists() and dst_path.stat().st_size > 10_000_000:
                logger.info(f"Archivo GFS ya existe: {dst_path.name}")
                archivos_descargados.append(dst_path)
                continue

            if fuente == "aws":
                url = f"https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{fecha_nodash}/{ciclo_hora}/atmos/gfs.t{ciclo_hora}z.pgrb2.0p25.f{fstr}"
            else:
                url = f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/gfs.{fecha_nodash}/{ciclo_hora}/atmos/gfs.t{ciclo_hora}z.pgrb2.0p25.f{fstr}"

            logger.info(f"Descargando {filename} desde {fuente.upper()}...")
            try:
                with requests.get(url, stream=True, timeout=60) as r:
                    r.raise_for_status()
                    with open(dst_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=1024 * 1024):
                            f.write(chunk)
                logger.info(f"Completado: {filename} ({dst_path.stat().st_size / 1e6:.1f} MB)")
                archivos_descargados.append(dst_path)
            except Exception as e:
                logger.error(f"Fallo al descargar {filename}: {e}")

        return archivos_descargados
