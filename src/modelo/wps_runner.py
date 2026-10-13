"""Ejecutor y verificador de la cadena de preprocesamiento WPS (geogrid, ungrib, metgrid)."""

import os
import subprocess
import logging
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("modelo.wps")


class WPSRunner:
    """Orquestador de ejecutables de WPS para generación de archivos met_em."""

    def __init__(self, wps_dir: Optional[str] = None):
        # PC pgich: "/home/pgich/Build_WRF/WPS"
        # PC roberto (WPS 4.0, en desuso): "/home/roberto/opencode/wrf/WPS-4.0"
        self.wps_dir = Path(wps_dir or os.environ.get("WPS_DIR", "/home/roberto/opencode/wrf/WPS-4.5"))  # PC roberto

    def verificar_archivos_metgrid(self, search_dir: Optional[str] = None) -> List[Path]:
        """Verifica la existencia de archivos met_em.d01.*.nc generados."""
        dir_to_check = Path(search_dir or self.wps_dir)
        if not dir_to_check.exists():
            logger.warning(f"Directorio WPS no existe: {dir_to_check}")
            return []
        met_files = sorted(dir_to_check.glob("met_em.d01.*.nc"))
        logger.info(f"Encontrados {len(met_files)} archivos met_em en {dir_to_check}")
        return met_files

    def ejecutar_metgrid(self) -> bool:
        """Ejecuta metgrid.exe en el directorio WPS."""
        if not (self.wps_dir / "metgrid.exe").exists():
            logger.error(f"metgrid.exe no encontrado en {self.wps_dir}")
            return False

        logger.info("Ejecutando metgrid.exe...")
        try:
            res = subprocess.run(
                ["./metgrid.exe"],
                cwd=str(self.wps_dir),
                capture_output=True,
                text=True,
                timeout=300,
            )
            return res.returncode == 0
        except Exception as e:
            logger.error(f"Error al ejecutar metgrid.exe: {e}")
            return False
