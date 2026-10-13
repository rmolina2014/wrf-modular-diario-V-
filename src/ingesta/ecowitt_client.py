"""Cliente HTTP robusto para la adquisición de datos de estaciones meteorológicas EcoWitt."""

import os
import json
import time
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("ingesta.ecowitt")

ECOWITT_CONFIG = Path(__file__).parent.parent.parent / "config" / "estaciones.json"

# Estaciones que usan las credenciales estándar (app_key/api_key).
# ECOHUMUS usa claves separadas (ECOWITT_APP_KEY_ECOHUMUS / API_KEY_ECOHUMUS).
ECOHUMUS = "ECOHUMUS"


def cargar_catalogo_estaciones(config_path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Carga el catálogo unificado de estaciones desde config/estaciones.json.

    Formato esperado por cada estación: {"lat", "lon", "elev", "mac"}.
    """
    ruta = Path(config_path or ECOWITT_CONFIG)
    if not ruta.exists():
        raise FileNotFoundError(f"Catálogo de estaciones no encontrado: {ruta}")
    with open(ruta, "r", encoding="utf-8") as f:
        catalogo = json.load(f)
    return {
        nombre: {
            "lat": float(meta.get("lat", 0.0)),
            "lon": float(meta.get("lon", 0.0)),
            "elev": float(meta.get("elev", 0.0)),
            "mac": meta.get("mac"),
        }
        for nombre, meta in catalogo.items()
    }


def _grupal(mac: str, app_key: str, api_key: str, fecha: str, call_back: str,
            ciclo: str = "5min") -> Optional[Dict[str, Any]]:
    """Obtiene un grupo (outdoor/wind/pressure/rainfall/solar) del histórico v3."""
    url = "https://api.ecowitt.net/api/v3/device/history"
    params = {
        "application_key": app_key,
        "api_key": api_key,
        "mac": mac,
        "start_date": f"{fecha} 00:00:00",
        "end_date": f"{fecha} 23:59:59",
        "cycle_type": ciclo,
        "call_back": call_back,
        "temp_unitid": "1",
        "pressure_unitid": "3",
        "wind_speed_unitid": "7",
        "rainfall_unitid": "12",
        "solar_irradiance_unitid": "16",
    }
    try:
        response = requests.get(url, params=params, timeout=60)
        response.raise_for_status()
        data = response.json()
        if data.get("code") == 0:
            return data
        logger.warning(f"API code {data.get('code')}: {data.get('msg', '?')} (MAC {mac}, {call_back})")
        return None
    except requests.exceptions.Timeout:
        logger.error(f"Timeout MAC {mac} group={call_back}")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Error MAC {mac} group={call_back}: {e}")
        return None


class EcowittIngestor:
    """Cliente para la API de EcoWitt (v3) con control de rate limit y manejo de errores."""

    _ultima_peticion = 0.0
    _min_intervalo = 1.0  # segundos entre peticiones

    def __init__(
        self,
        app_key: Optional[str] = None,
        api_key: Optional[str] = None,
        app_key_ecohumus: Optional[str] = None,
        api_key_ecohumus: Optional[str] = None,
    ):
        self.base_url_rt = "https://api.ecowitt.net/api/v3/device/real_time"
        self.base_url_hist = "https://api.ecowitt.net/api/v3/device/history"
        self.app_key = app_key or os.getenv("ECOWITT_APP_KEY")
        self.api_key = api_key or os.getenv("ECOWITT_API_KEY")
        self.app_key_ecohumus = app_key_ecohumus or os.getenv("ECOWITT_APP_KEY_ECOHUMUS")
        self.api_key_ecohumus = api_key_ecohumus or os.getenv("ECOWITT_API_KEY_ECOHUMUS")

    def _claves_para(self, nombre: str) -> Tuple[Optional[str], Optional[str]]:
        """Devuelve (app_key, api_key) según la estación (ECOHUMUS usa claves propias)."""
        if nombre == ECOHUMUS:
            return self.app_key_ecohumus, self.api_key_ecohumus
        return self.app_key, self.api_key

    def tiene_credenciales(self) -> bool:
        """Verifica si las credenciales principales están presentes."""
        return bool(self.app_key and self.api_key and len(self.app_key) >= 10 and len(self.api_key) >= 10)

    def _esperar_rate_limit(self):
        ahora = time.time()
        tiempo_transcurrido = ahora - self._ultima_peticion
        if tiempo_transcurrido < self._min_intervalo:
            time.sleep(self._min_intervalo - tiempo_transcurrido)
        self._ultima_peticion = time.time()

    def obtener_datos_tiempo_real(self, mac_address: str, app_key: Optional[str] = None, api_key: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Consulta datos de tiempo real para una MAC específica."""
        self._esperar_rate_limit()
        ak = app_key or self.app_key
        sk = api_key or self.api_key

        if not ak or not sk:
            logger.error("No hay credenciales configuradas para consultar EcoWitt.")
            return None

        params = {
            "application_key": ak,
            "api_key": sk,
            "mac": mac_address,
            "call_back": "all",
            "temp_unitid": "1",          # Celsius
            "pressure_unitid": "3",      # hPa
            "wind_speed_unitid": "7",    # km/h
            "rainfall_unitid": "12",     # mm
            "solar_irradiance_unitid": "16", # W/m2
        }

        try:
            response = requests.get(self.base_url_rt, params=params, timeout=30)
            response.raise_for_status()
            res_json = response.json()
            if res_json.get("code") == 0:
                return res_json
            logger.warning(f"EcoWitt API code {res_json.get('code')}: {res_json.get('msg')}")
            return None
        except Exception as e:
            logger.error(f"Error consultando MAC {mac_address}: {e}")
            return None

    def extraer_datos_estacion(self, data: Optional[Dict[str, Any]], nombre_estacion: str) -> Dict[str, Any]:
        """Normaliza la respuesta JSON de EcoWitt a un diccionario plano estandarizado."""
        if not data or "data" not in data:
            return {"estacion": nombre_estacion, "error": "Sin datos"}

        raw_data = data["data"]
        if isinstance(raw_data, list):
            if not raw_data:
                return {"estacion": nombre_estacion, "error": "Sin datos (lista vacía)"}
            raw_data = raw_data[0]

        timestamp = int(data.get("time", time.time()))
        dt = datetime.fromtimestamp(timestamp)

        return {
            "estacion": nombre_estacion,
            "time_unix": timestamp,
            "fecha": dt.strftime("%Y-%m-%d"),
            "hora": dt.strftime("%H:%M"),
            "temp": raw_data.get("outdoor", {}).get("temperature", {}).get("value"),
            "humedad": raw_data.get("outdoor", {}).get("humidity", {}).get("value"),
            "viento": raw_data.get("wind", {}).get("wind_speed", {}).get("value"),
            "viento_rafaga": raw_data.get("wind", {}).get("wind_gust", {}).get("value"),
            "direcc": raw_data.get("wind", {}).get("wind_direction", {}).get("value"),
            "presion_relativa": raw_data.get("pressure", {}).get("relative", {}).get("value"),
            "presion_absoluta": raw_data.get("pressure", {}).get("absolute", {}).get("value"),
            "rain_daily": raw_data.get("rainfall", {}).get("daily", {}).get("value"),
            "rain_monthly": raw_data.get("rainfall", {}).get("monthly", {}).get("value"),
            "solar": raw_data.get("solar_and_uvi", {}).get("solar", {}).get("value"),
            "termica": raw_data.get("outdoor", {}).get("feels_like", {}).get("value"),
            "rocio": raw_data.get("outdoor", {}).get("dew_point", {}).get("value"),
        }

    def consultar_todas_las_estaciones(self) -> List[Dict[str, Any]]:
        """Consulta todas las estaciones del catálogo unificado incluyendo ECOHUMUS."""
        catalogo = cargar_catalogo_estaciones()
        resultados = []

        for nombre, meta in catalogo.items():
            mac = meta.get("mac")
            if not mac:
                logger.warning(f"Estación {nombre} sin MAC en catálogo; omitida.")
                continue
            ak, sk = self._claves_para(nombre)
            data = self.obtener_datos_tiempo_real(mac, ak, sk)
            if data:
                resultados.append(self.extraer_datos_estacion(data, nombre))
            else:
                resultados.append({"estacion": nombre, "error": "Sin respuesta"})

        return resultados

    def guardar_json_crudo(self, resultados: List[Dict[str, Any]], output_dir: str = "data/raw") -> str:
        """Guarda la lista de observaciones crudas en formato JSON."""
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        archivo = os.path.join(output_dir, f"ecowitt_todos_{ts}.json")
        with open(archivo, "w", encoding="utf-8") as f:
            json.dump(resultados, f, indent=4, ensure_ascii=False)
        logger.info(f"Observaciones guardadas en: {archivo}")
        return archivo

    # ------------------------------------------------------------------
    # Datos históricos (endpoint v3/device/history)
    # ------------------------------------------------------------------
    @staticmethod
    def _get_list(data: Dict[str, Any], *keys: str) -> Dict[str, str]:
        """Navega el dict anidado de histórico para obtener un grupo 'list' {ts: value}."""
        d = data
        for k in keys:
            d = d.get(k, {})
        if isinstance(d, dict) and "list" in d:
            return d.get("list", {})
        return {}

    def obtener_historico_estacion(self, mac: str, nombre: str, fecha: str) -> Optional[Dict[str, Any]]:
        """Obtiene el histórico completo de 1 día para una estación (grupos separados).

        Retorna el dict crudo del grupo outdoor al estilo del script legado
        ({"code": 0, "data": {group: {...}}}).
        """
        grupos = ["outdoor", "wind", "pressure", "rainfall", "solar_and_uvi"]
        ak, sk = self._claves_para(nombre)
        if not ak or not sk:
            logger.warning(f"{nombre}: sin credenciales, omitida.")
            return None

        all_data: Dict[str, Any] = {}
        for grp in grupos:
            self._esperar_rate_limit()
            data = _grupal(mac, ak, sk, fecha, grp)
            if data and data.get("data"):
                all_data.update(data["data"])
            time.sleep(0.34)

        if not all_data:
            logger.warning(f"{nombre}: sin datos históricos para {fecha}.")
            return None

        return {"code": 0, "data": all_data}

    def descargar_historico(self, fecha: str, output_dir: str = "data/raw") -> Dict[str, Dict[str, Any]]:
        """Descarga el histórico diario de TODAS las estaciones del catálogo.

        Devuelve {nombre: respuesta} y guarda JSON/CSV en output_dir.
        """
        catalogo = cargar_catalogo_estaciones()
        fecha_file = fecha.replace("-", "")
        respuestas: Dict[str, Dict[str, Any]] = {}

        for nombre, meta in catalogo.items():
            mac = meta.get("mac")
            if not mac:
                logger.warning(f"Estación {nombre} sin MAC; omitida.")
                continue
            logger.info(f"Consultando {nombre} ({mac})...")
            data = self.obtener_historico_estacion(mac, nombre, fecha)
            respuestas[nombre] = data
            if data:
                datos = data.get("data", {})
                temp_list = self._get_list(datos, "outdoor", "temperature")
                n_valid = sum(1 for v in temp_list.values() if self._clean_value(v) is not None)
                logger.info(f"  {nombre}: {n_valid} registros válidos de {len(temp_list)}")
            else:
                logger.warning(f"  {nombre}: Sin datos")
            time.sleep(0.34)

        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        json_path = out / f"ecowitt_historico_{fecha_file}.json"
        json_completo: Dict[str, Any] = {
            "fechaconsulta": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "fechadatos": fecha,
            "estaciones": {},
        }
        for nombre, data in respuestas.items():
            if data and data.get("code") == 0:
                datos = data.get("data", {})
                temp_list = self._get_list(datos, "outdoor", "temperature")
                n_valid = sum(1 for v in temp_list.values() if self._clean_value(v) is not None)
                json_completo["estaciones"][nombre] = {
                    "mac": catalogo[nombre].get("mac", ""),
                    "registros_validos": n_valid,
                    "registros_total": len(temp_list),
                    "datos": datos,
                }
            else:
                json_completo["estaciones"][nombre] = {
                    "mac": catalogo[nombre].get("mac", ""),
                    "registros_validos": 0,
                    "registros_total": 0,
                    "error": "Sin datos",
                }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(json_completo, f, indent=2, ensure_ascii=False)
        logger.info(f"JSON guardado: {json_path}")

        csv_path = out / f"ecowitt_historico_{fecha_file}.csv"
        filas = self._convertir_a_csv(respuestas, catalogo, fecha)
        if filas:
            import csv
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=list(filas[0].keys()))
                writer.writeheader()
                writer.writerows(filas)
            logger.info(f"CSV guardado: {csv_path} ({len(filas)} filas)")
        else:
            logger.warning("No hay datos suficientes para guardar CSV")

        return respuestas

    @staticmethod
    def _clean_value(val: Any) -> Optional[float]:
        """Devuelve float si es válido, si no None."""
        if val is None or val == "-" or val == "" or val == "N/A":
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    def _convertir_a_csv(self, respuestas: Dict[str, Dict[str, Any]],
                         catalogo: Dict[str, Dict[str, Any]], fecha: str) -> List[Dict[str, str]]:
        """Convierte las respuestas históricas a filas CSV tabulares."""
        filas: List[Dict[str, str]] = []
        for nombre, data in respuestas.items():
            if not data or data.get("code") != 0:
                logger.warning(f"Sin datos para {nombre}")
                continue
            datos = data.get("data", {})
            temp_list = self._get_list(datos, "outdoor", "temperature")
            hum_list = self._get_list(datos, "outdoor", "humidity")
            wind_list = self._get_list(datos, "wind", "wind_speed")
            gust_list = self._get_list(datos, "wind", "wind_gust")
            dir_list = self._get_list(datos, "wind", "wind_direction")
            pres_rel_list = self._get_list(datos, "pressure", "relative")
            pres_abs_list = self._get_list(datos, "pressure", "absolute")
            rain_list = self._get_list(datos, "rainfall", "daily")
            solar_list = self._get_list(datos, "solar_and_uvi", "solar")

            all_sources = [temp_list, hum_list, wind_list, pres_rel_list]
            if not all_sources:
                continue
            all_ts = sorted(set().union(*(s.keys() for s in all_sources if s)))

            for ts in all_ts:
                try:
                    fecha_hora = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError, OverflowError):
                    continue
                filas.append({
                    "estacion": nombre,
                    "fecha": fecha,
                    "hora_unix": ts,
                    "fecha_hora": fecha_hora,
                    "temp_c": self._safe_str(temp_list.get(ts)),
                    "humedad_pct": self._safe_str(hum_list.get(ts)),
                    "viento_kmh": self._safe_str(wind_list.get(ts)),
                    "viento_rafaga_kmh": self._safe_str(gust_list.get(ts)),
                    "direcc_grados": self._safe_str(dir_list.get(ts)),
                    "presion_relativa_hpa": self._safe_str(pres_rel_list.get(ts)),
                    "presion_absoluta_hpa": self._safe_str(pres_abs_list.get(ts)),
                    "lluvia_diaria_mm": self._safe_str(rain_list.get(ts)),
                    "solar_wm2": self._safe_str(solar_list.get(ts)),
                })
        return filas

    @staticmethod
    def _safe_str(val: Any) -> str:
        """Convierte a string, vacío si None o guión."""
        if val is None or val == "-" or val == "":
            return ""
        return str(val)