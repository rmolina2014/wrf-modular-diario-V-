"""Generador de archivos OBS_DOMAIN101 (Formato 105 de WRF) para asimilación FDDA de superficie."""

import logging
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional
import pandas as pd

logger = logging.getLogger("asimilacion.obsnud")


class ObsNudWriter:
    """Convierte observaciones procesadas al formato OBS_DOMAIN101 (Surface FORMAT 105) de WRF."""

    def __init__(self, config_estaciones_path: Optional[str] = None):
        self.config_path = Path(config_estaciones_path or "config/estaciones.json")

    def _u_v(self, wspd_kmh: Optional[float], wdir_deg: Optional[float]):
        if wspd_kmh is None or wdir_deg is None or pd.isna(wspd_kmh) or pd.isna(wdir_deg):
            return -888888.0, -888888.0, 0.0, 0.0
        vel_ms = float(wspd_kmh) / 3.6
        rad = math.radians(float(wdir_deg))
        u = -vel_ms * math.sin(rad)
        v = -vel_ms * math.cos(rad)
        return u, v, 129.0, 129.0

    @staticmethod
    def _desfase_por_estacion(estaciones: List[str]) -> Dict[str, timedelta]:
        """Retorna desfase cero para todas las estaciones.

        Nota: una version anterior usaba offsets de 1 segundo por estacion
        para evitar colisiones de timestamp, pero WRF 4.5 falla con
        'Bad value during integer read' en module_date_time.f90 al releer
        archivos con timestamps no estandar (000001, 000002).
        El formato original con timestamps cada 5 minutos funciona correctamente.
        """
        return {est: timedelta(seconds=0) for est in set(estaciones)}

    def escribir_obsnud(self, observaciones: List[Dict[str, Any]], output_path: str = "data/processed/OBS_DOMAIN101") -> Path:
        """Escribe la lista de observaciones en el archivo destino con formato estricto FORMAT 105 de WRF.

        Las observaciones se ordenan cronológicamente por timestamp antes de escribir:
        el lector de WRF (wrf_fddaobs_in.F, formato 105) exige estricto orden temporal
        (un registro con TIMEOB anterior al último leído provoca 'in4dob STOP 111').

        Nota de comportamiento real (mejora 3.6 del informe_mejoras_f4):
        _desfase_por_estacion() devuelve desfase CERO para todas las estaciones (los
        timestamps quedan tal cual, cada 5 min). Una version anterior usaba offsets de
        1 s por estacion para evitar observaciones simultaneas de estaciones distintas,
        pero WRF 4.5 aborta con 'Bad value during integer read' en module_date_time.f90
        al releer timestamps no estandar (000001, 000002), por lo que el desfase fue
        eliminado. El caso de dos estaciones con el MISMO timestamp exacto NO queda
        protegido por ningun desfase; solo el orden cronologico estable; si ese escenario
        llegara a provocar un problema en un binario futuro, no estaría previsto.
        """
        dst_path = Path(output_path)
        dst_path.parent.mkdir(parents=True, exist_ok=True)

        def _timestamp(obs: Dict[str, Any]) -> str:
            dt_str = obs.get("datetime_str")
            if not dt_str:
                fecha = str(obs.get("fecha", "2026-01-01")).replace("-", "")
                hora = str(obs.get("hora", "00:00")).replace(":", "")
                if len(hora) == 4:
                    hora += "00"
                dt_str = f"{fecha}{hora}"
            else:
                dt_str = dt_str.replace("-", "").replace(":", "").replace(" ", "")
            return dt_str

        # Pre-procesar y ordenar por timestamp (formato YYYYMMDDHHMMSS, orden lexicográfico = temporal)
        prep = []
        desfases = self._desfase_por_estacion([str(o.get("estacion", "UNKNOWN")) for o in observaciones])
        for obs in observaciones:
            try:
                lat = float(obs.get("lat", 0.0))
                lon = float(obs.get("lon", 0.0))
                elev = float(obs.get("elev", 0.0))
            except (TypeError, ValueError):
                lat = lon = elev = 0.0

            t_c = obs.get("temp")
            if t_c is not None and pd.notna(t_c):
                t_k = float(t_c) + 273.15
                t_qc = 0.0
            else:
                t_k = -999999.0
                t_qc = -888888.0

            rh_val = obs.get("humedad")
            if rh_val is not None and pd.notna(rh_val):
                rh = float(rh_val)
                rh_qc = 0.0
            else:
                rh = -999999.0
                rh_qc = -888888.0

            p_val = obs.get("presion_absoluta")
            if p_val is None or pd.isna(p_val):
                p_val = obs.get("presion_relativa")

            if p_val is not None and pd.notna(p_val):
                psfc_pa = float(p_val) * 100.0
                psfc_qc = 0.0
            else:
                psfc_pa = -888888.0
                psfc_qc = -888888.0

            u, v, u_qc, v_qc = self._u_v(obs.get("viento"), obs.get("direcc"))
            nombre = str(obs.get("estacion", "UNKNOWN"))
            dt_str = _timestamp(obs)
            # Desfase per-estación = 0 s (ver _desfase_por_estacion); se conserva la suma
            # para que quede explícito que no hay manipulación de timestamps (mejora 3.6).
            dt_str = (datetime.strptime(dt_str, "%Y%m%d%H%M%S") + desfases[nombre]).strftime("%Y%m%d%H%M%S")
            prep.append((dt_str, nombre, lat, lon, elev, t_k, t_qc, rh, rh_qc,
                         psfc_pa, psfc_qc, u, u_qc, v, v_qc))

        prep.sort(key=lambda r: r[0])

        escritas = 0
        with open(dst_path, "w", encoding="utf-8") as f:
            for (dt_str, nombre, lat, lon, elev, t_k, t_qc, rh, rh_qc,
                 psfc_pa, psfc_qc, u, u_qc, v, v_qc) in prep:
                # 1. Timestamp (YYYYMMDDHHMMSS)
                f.write(f" {dt_str}\n")
                # 2. Coordenadas: FORMAT(2x,2(f9.4,1x))
                f.write(f"  {lat:9.4f} {lon:9.4f} \n")
                # 3. Identificador: FORMAT(2x,2(a40,3x))
                f.write(f"  {nombre:<40s}   {'SURFACE':<40s}   \n")
                # 4. Plataforma y elevación: FORMAT(2x,2(a16,2x),f8.0,2x,2(l4,2x),i5)
                #   2x, platform(a16), 2x, source(a16), 2x, elev(f8.0), 2x, is_sound(l4), 2x, bogus(l4), 2x, meas_count(i5)
                # "FM-12 SYNOP" en cols 7-11 del campo platform: asi es como
                # WRF (wrf_fddaobs_in.F) reconoce el tipo de plataforma (plfo=4);
                # escribir solo "SYNOP" al inicio del campo lo deja en "unknown".
                f.write(f"  {'FM-12 SYNOP':<16s}  {nombre:<16s}  {elev:>8.0f}  {'F':<4s}  {'F':<4s}  {1:>5d}\n")
                # 5. Datos FORMAT 105: 9 pares (valor, qc) = 18 valores
                # slp, slp_qc, ref_p, ref_p_qc, height, height_qc, temp, temp_qc, u, u_qc, v, v_qc, rh, rh_qc, psfc, psfc_qc, precip, precip_qc
                linea_datos = (
                    f" -888888.000 -888888.000 -888888.000 -888888.000"
                    f" {elev:>11.3f}       0.000"
                    f" {t_k:>11.3f} {t_qc:>11.3f}"
                    f" {u:>11.3f} {u_qc:>11.3f}"
                    f" {v:>11.3f} {v_qc:>11.3f}"
                    f" {rh:>11.3f} {rh_qc:>11.3f}"
                    f" {psfc_pa:>11.3f} {psfc_qc:>11.3f}"
                    f" -888888.000 -888888.000\n"
                )
                f.write(linea_datos)
                escritas += 1

            # Sin marcador de fin de archivo: el lector de WRF (wrf_fddaobs_in.F)
            # detecta el fin de las observaciones por EOF real (read ... end=111),
            # no por un registro -777777. Escribirlo se interpreta como una
            # observacion adicional invalida y provoca un error de formato
            # Fortran no controlado (wrf.exe termina con exit code 2).

        logger.info(f"OBS_DOMAIN101 generado en {dst_path} con {escritas} observaciones (orden cronológico).")
        return dst_path

    def generar_desde_dataframe(self, df: pd.DataFrame, config_estaciones: Optional[Dict[str, Any]] = None, output_path: str = "data/processed/OBS_DOMAIN101") -> Path:
        """Convierte directamente un DataFrame validado a OBS_DOMAIN101."""
        if config_estaciones is None and self.config_path.exists():
            import json
            with open(self.config_path, "r", encoding="utf-8") as f:
                config_estaciones = json.load(f)
        config_estaciones = config_estaciones or {}

        obs_list = []
        omitidas_holdout = 0
        for _, row in df.iterrows():
            nombre = str(row.get("estacion", "UNKNOWN"))
            meta = config_estaciones.get(nombre, {})
            # Hold-out espacial: las estaciones con rol "evaluacion" en
            # config/estaciones.json se reservan para validar generalizacion
            # y nunca se asimilan (no entran a OBS_DOMAIN101).
            if meta.get("rol") == "evaluacion":
                omitidas_holdout += 1
                continue
            obs_list.append({
                "estacion": nombre,
                "lat": meta.get("lat", row.get("lat", 0.0)),
                "lon": meta.get("lon", row.get("lon", 0.0)),
                "elev": meta.get("elev", row.get("elev", 0.0)),
                "fecha": row.get("fecha"),
                "hora": row.get("hora"),
                "temp": row.get("temp"),
                "humedad": row.get("humedad"),
                "presion_absoluta": row.get("presion_absoluta"),
                "presion_relativa": row.get("presion_relativa"),
                "viento": row.get("viento"),
                "direcc": row.get("direcc"),
            })

        if omitidas_holdout:
            logger.info(
                f"Hold-out espacial: {omitidas_holdout} observaciones de estaciones "
                f"'evaluacion' omitidas de la asimilacion (no van a {output_path})."
            )

        return self.escribir_obsnud(obs_list, output_path)
