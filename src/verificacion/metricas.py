"""Métricas continuas y categóricas para la verificación del pronóstico."""

from typing import Dict, Optional

import numpy as np


def _pares(obs, mod):
    o = np.asarray(obs, dtype=float)
    m = np.asarray(mod, dtype=float)
    ok = np.isfinite(o) & np.isfinite(m)
    return o[ok], m[ok]


def resumen(obs, mod) -> Dict[str, Optional[float]]:
    """Bias, MAE, RMSE y correlación sobre pares válidos."""
    o, m = _pares(obs, mod)
    n = int(len(o))
    if n == 0:
        return {"n": 0, "bias": None, "mae": None, "rmse": None, "r": None}
    d = m - o
    r = None
    if n >= 3 and np.std(o) > 0 and np.std(m) > 0:
        r = float(np.corrcoef(o, m)[0, 1])
    return {
        "n": n,
        "bias": float(np.mean(d)),
        "mae": float(np.mean(np.abs(d))),
        "rmse": float(np.sqrt(np.mean(d ** 2))),
        "r": r,
    }


def resumen_direccion(obs, mod) -> Dict[str, Optional[float]]:
    """Error circular de dirección (grados): MAE y bias con signo envuelto."""
    o, m = _pares(obs, mod)
    n = int(len(o))
    if n == 0:
        return {"n": 0, "bias": None, "mae": None}
    d = (m - o + 180.0) % 360.0 - 180.0
    return {"n": n, "bias": float(np.mean(d)), "mae": float(np.mean(np.abs(d)))}


def categoricas(obs, mod, umbral: float) -> Dict[str, Optional[float]]:
    """Tabla de contingencia y scores para un umbral (p. ej. lluvia > 1 mm)."""
    o, m = _pares(obs, mod)
    if len(o) == 0:
        return {"n": 0, "pod": None, "far": None, "csi": None, "ets": None, "fbi": None,
                "aciertos": 0, "falsas_alarmas": 0, "faltantes": 0, "rechazos": 0}
    o = o > umbral
    m = m > umbral
    a = int(np.sum(o & m))
    b = int(np.sum(~o & m))
    c = int(np.sum(o & ~m))
    d = int(np.sum(~o & ~m))
    n = a + b + c + d

    def _div(x, y):
        return float(x) / y if y else None

    e = (a + b) * (a + c) / n if n else 0.0
    return {
        "n": n,
        "aciertos": a,
        "falsas_alarmas": b,
        "faltantes": c,
        "rechazos": d,
        "pod": _div(a, a + c),
        "far": _div(b, a + b),
        "csi": _div(a, a + b + c),
        "fbi": _div(a + b, a + c),
        "ets": _div(a - e, a + b + c - e),
    }


def bootstrap_ci(obs, mod, estadistico: str = "bias", n_boot: int = 1000,
                 alpha: float = 0.05, seed: int = 0):
    """Intervalo de confianza percentil por bootstrap de pares (bias, mae o rmse)."""
    o, m = _pares(obs, mod)
    if len(o) < 3:
        return None, None
    rng = np.random.default_rng(seed)
    valores = []
    for _ in range(n_boot):
        idx = rng.integers(0, len(o), len(o))
        d = m[idx] - o[idx]
        if estadistico == "bias":
            valores.append(np.mean(d))
        elif estadistico == "mae":
            valores.append(np.mean(np.abs(d)))
        elif estadistico == "rmse":
            valores.append(np.sqrt(np.mean(d ** 2)))
        else:
            raise ValueError(f"estadístico desconocido: {estadistico}")
    lo = float(np.quantile(valores, alpha / 2))
    hi = float(np.quantile(valores, 1 - alpha / 2))
    return lo, hi
