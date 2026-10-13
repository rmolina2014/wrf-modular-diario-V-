"""Ejecutor y supervisor de real.exe y wrf.exe para simulaciones Control y Nudged."""

import os
import subprocess
import time
import logging
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger("modelo.wrf")

_BASH_LOG = "__WRF_RUN_LOG__"


class WRFRunner:
    """Orquestador de simulaciones WRF (local o vía contenedor Docker)."""

    def __init__(self, run_dir: Optional[str] = None,
                 env_bash: Optional[str] = None,
                 mpirun: Optional[str] = None,
                 np: Optional[str] = None,
                 timeout: int = 7200,
                 reintentos: int = 5):
        # PC roberto (WRF 4.0, en desuso): "/home/roberto/opencode/wrf/WRF-4.0/run"
        self.run_dir = Path(run_dir or os.environ.get("LOCAL_WRF_DIR", "/home/roberto/opencode/wrf/WRF-4.5/run"))  # PC roberto
        self.env_bash = env_bash or os.environ.get("WRF_ENV_BASH", "")
        self.mpirun = mpirun or os.environ.get("MPIRUN", "/usr/bin/mpirun")
        self.np = str(np or os.environ.get("WRF_NP", "1"))
        self.timeout = timeout
        self.reintentos = reintentos
        self.ultimos_reintentos = 0

    def verificar_entorno(self) -> Dict[str, bool]:
        """Verifica la presencia de ejecutables y archivos base de WRF."""
        if not self.run_dir.exists():
            return {"run_dir": False, "wrf_exe": False, "real_exe": False}

        return {
            "run_dir": True,
            "wrf_exe": (self.run_dir / "wrf.exe").exists(),
            "real_exe": (self.run_dir / "real.exe").exists(),
            "wrfinput_d01": (self.run_dir / "wrfinput_d01").exists(),
            "wrfbdy_d01": (self.run_dir / "wrfbdy_d01").exists(),
        }

    def ejecutar_comando_seguro(self, cmd: str, log_file: Path, env_vars: Optional[Dict[str, str]] = None) -> bool:
        """Ejecuta un comando dentro del shell con el entorno WRF operativo cargado.

        La salida se vuelca a un archivo (evita el pipe de subprocess, que dispara un
        SIGSEGV reproducible en wrf.exe con obs nudging). Sólo se devuelve si
        retorna código 0; el contenido se escribe en ``log_file``.
        """
        import tempfile
        env_load = f"source {self.env_bash} && " if Path(self.env_bash).exists() else ""
        # LD_LIBRARY_PATH vacío + unset de OMP: entorno operativo de esta PC
        full_cmd = ("export LD_LIBRARY_PATH='' "
                    "&& unset OMP_NUM_THREADS OMP_STACKSIZE KMP_STACKSIZE 2>/dev/null || true "
                    f"&& {env_load}export OMP_NUM_THREADS=1 "
                    f"&& {{ {cmd} ; }} > {_BASH_LOG} 2>&1")

        log_file.parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"  Ejecutando: {cmd} (Log: {log_file.name})")

        log_fd, log_path = tempfile.mkstemp(prefix="wrf_run_", suffix=".log")
        os.close(log_fd)
        full_cmd = full_cmd.replace(_BASH_LOG, log_path)
        try:
            proc = subprocess.run(
                ["bash", "-lc", full_cmd],
                cwd=str(self.run_dir),
                timeout=self.timeout,
            )
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            log_file.write_text(content, encoding="utf-8")
        except Exception as e:
            logger.error(f"Fallo al ejecutar '{cmd}': {e}")
            return False
        finally:
            if os.path.exists(log_path):
                os.remove(log_path)
        if proc.returncode != 0:
            logger.error(f"  Error (codigo {proc.returncode}): {content.strip()[-500:]}")
        else:
            for line in content.splitlines()[-10:]:
                logger.info(f"  {line}")
        return proc.returncode == 0

    def _verificar_success(self, mensaje: str) -> bool:
        """Verifica el mensaje de éxito en rsl.out.0000 si existe."""
        rsl = self.run_dir / "rsl.out.0000"
        if not rsl.exists():
            return False
        content = rsl.read_text(errors="ignore")
        if mensaje in content:
            return True
        tail = "\n".join(content.splitlines()[-15:])
        logger.error(f"  Sin '{mensaje}' en rsl.out.0000:\n{tail}")
        return False

    def ejecutar_real(
        self,
        log_path: Optional[Path] = None,
    ) -> bool:
        """Ejecuta real.exe para generar wrfinput_d01 y wrfbdy_d01.

        np=1 (default) ejecuta el binario directo (un solo proceso, como el setup operativo).
        """
        log = log_path or (self.run_dir / "real.log")
        if str(self.np) == "1":
            comando = "./real.exe"
        else:
            comando = f"{self.mpirun} -np {self.np} ./real.exe"
        logger.info("Paso: Corriendo real.exe...")

        exito = self.ejecutar_comando_seguro(comando, log)
        if exito:
            exito = ("SUCCESS COMPLETE REAL_EM INIT" in (log.read_text(errors="ignore") if log.exists() else "")
                     or self._verificar_success("SUCCESS COMPLETE REAL_EM INIT"))
        if exito:
            logger.info("real.exe finalizado exitosamente.")
        else:
            logger.error("Error en ejecución de real.exe. Revisar log.")
        return exito

    def ejecutar_wrf(
        self,
        nudged: bool = True,
        log_path: Optional[Path] = None,
        run_label: str = "",
        reintentos: Optional[int] = None,
    ) -> bool:
        """Ejecuta wrf.exe para simulación asimilada (nudged) o corrida de control.

        Nota: en este binario, wrf.exe con obs nudging puede abortar de forma INTERMITENTE
        con SIGSEGV (codigo 139) en mediation_integrate. Para dar robustez a la corrida,
        si el intento falla sin lograr 'SUCCESS COMPLETE WRF', se limpia el run dir y se
        reintenta (hasta 'reintentos' veces), aprovechando que una corrida limpia suele
        completar. Si 'reintentos' es 0, se comporta como antes (un solo intento).
        """
        tipo = "Nudged" if nudged else "Control"
        label = run_label or tipo.lower()
        log = log_path or (self.run_dir / f"wrf_{label}.log")
        reintentos = reintentos if reintentos is not None else self.reintentos
        if str(self.np) == "1":
            comando = "./wrf.exe"
        else:
            comando = f"{self.mpirun} -np {self.np} ./wrf.exe"

        intento_actual = 1
        while True:
            # Limpiar wrfout/rsl previos para que WRF no intente sobrescribir archivos
            # existentes (puede corromper las salidas ni contaminar el reintento).
            subprocess.run(
                ["bash", "-lc", f"cd {self.run_dir} && rm -f wrfout_d01_* rsl.*"],
                capture_output=True, text=True, timeout=30,
            )
            if reintentos > 0:
                logger.info(f"Paso: Corriendo wrf.exe ({tipo}) [intento {intento_actual}/{reintentos + 1}]...")
            else:
                logger.info(f"Paso: Corriendo wrf.exe ({tipo})...")
            start = time.time()
            ok = self.ejecutar_comando_seguro(comando, log)
            elapsed = time.strftime("%H:%M:%S", time.gmtime(time.time() - start))
            if ok:
                salida = log.read_text(errors="ignore") if log.exists() else ""
                ok = ("SUCCESS COMPLETE WRF" in salida
                      or self._verificar_success("SUCCESS COMPLETE WRF"))
            if ok:
                logger.info(f"  wrf.exe ({tipo}) completado en {elapsed} (intento {intento_actual})")
                self.ultimos_reintentos = intento_actual - 1
                logger.info(
                    f"  wrf.exe ({tipo}) reintentos consumidos: {self.ultimos_reintentos}"
                )
                return ok
            logger.error(f"  wrf.exe ({tipo}) FALLIDO en el intento {intento_actual} despues de {elapsed}")
            if intento_actual > reintentos:
                self.ultimos_reintentos = intento_actual - 1
                logger.error(f"  wrf.exe ({tipo}) finalizo con {self.ultimos_reintentos} reintentos")
                return False
            logger.warning(f"  Reintentando wrf.exe ({tipo}) ({intento_actual + 1}/{reintentos + 1})...")
            intento_actual += 1