import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

app = FastAPI(
    title="Recorridos Online Consolidado API",
    description="Middleware que consulta PaxTracker y devuelve todos los recorridos (con sus paradas y puntos) consolidados en un único array, cacheado por TTL.",
    version="1.0.0",
)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.add_middleware(GZipMiddleware, minimum_size=1000)

API_BASE = "https://paxtracker.masterbus.net/api"
PAXTRACKER_EMAIL = os.environ.get("PAXTRACKER_EMAIL")
PAXTRACKER_PASSWORD = os.environ.get("PAXTRACKER_PASSWORD")
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "20"))
REFRESH_API_KEY = os.environ.get("REFRESH_API_KEY")

_cache: Dict[str, Any] = {
    "data": None,
    "ts": 0.0,
    "errors": [],
    "build_seconds": None,
    "building": False,
}
_build_lock = threading.Lock()
_token_lock = threading.Lock()
_token: Optional[str] = None


def _login() -> str:
    global _token
    if not PAXTRACKER_EMAIL or not PAXTRACKER_PASSWORD:
        raise HTTPException(
            status_code=500,
            detail="Faltan las variables de entorno PAXTRACKER_EMAIL / PAXTRACKER_PASSWORD.",
        )
    res = requests.post(
        f"{API_BASE}/auth/login",
        json={"email": PAXTRACKER_EMAIL, "password": PAXTRACKER_PASSWORD},
        timeout=15,
    )
    res.raise_for_status()
    token = res.json()["data"]["token"]
    with _token_lock:
        _token = token
    return token


def _get_token() -> str:
    with _token_lock:
        token = _token
    return token or _login()


def _authed_get(url: str, **kwargs) -> requests.Response:
    """GET con reintento de login una vez si el token venció (401)."""
    token = _get_token()
    res = requests.get(url, headers={"Authorization": f"Bearer {token}"}, **kwargs)
    if res.status_code == 401:
        token = _login()
        res = requests.get(url, headers={"Authorization": f"Bearer {token}"}, **kwargs)
    return res


def _extract_fields(version_data: Dict, route_meta: Dict) -> Dict:
    v = version_data.get("data", {}).get("version", {})
    company = v.get("company") or route_meta.get("company") or {}

    route_stops = [
        {
            "index": s.get("index"),
            "name": s.get("name"),
            "lat": s.get("lat"),
            "lon": s.get("lon"),
            "waypoint": s.get("waypoint", False),
            "times": s.get("times", []),
            "changed": s.get("changed"),
            "way": s.get("way"),
            "sort": s.get("sort"),
        }
        for s in v.get("route_stops", [])
    ]

    route_points = [
        {"lat": p.get("lat"), "lon": p.get("lon")} for p in (v.get("route_points") or [])
    ]

    return {
        "route_id": v.get("route_id") or route_meta.get("id"),
        "route_name": v.get("route_name") or route_meta.get("name"),
        "company": {
            "id": company.get("id"),
            "name": company.get("name"),
            "cuit": company.get("cuit"),
            "email": company.get("email"),
        },
        "route_direction": v.get("route_direction") or route_meta.get("direction"),
        "revision": v.get("revision"),
        "index": v.get("index"),
        "status": v.get("status"),
        "message": v.get("message"),
        "distance": v.get("distance"),
        "route_stops": route_stops,
        "route_points": route_points,
    }


def _fetch_routes_list() -> List[Dict]:
    res = _authed_get(f"{API_BASE}/routes", timeout=30)
    if res.status_code != 200:
        raise HTTPException(status_code=502, detail=f"No se pudo obtener el listado de rutas (HTTP {res.status_code}).")
    return res.json().get("data", {}).get("routes", [])


def _fetch_route_version(route_meta: Dict) -> Dict:
    rid = route_meta["id"]
    vid = route_meta["version_id"]
    url = f"{API_BASE}/routes/{rid}/versions/{vid}"
    res = _authed_get(url, timeout=30)
    if res.status_code != 200:
        raise RuntimeError(f"route {rid} version {vid}: HTTP {res.status_code}")
    return _extract_fields(res.json(), route_meta)


def _build_consolidated() -> Dict[str, Any]:
    t0 = time.monotonic()
    routes = _fetch_routes_list()
    valid_routes = [r for r in routes if r.get("version_id")]

    consolidated: List[Dict] = []
    errors: List[str] = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_fetch_route_version, r): r for r in valid_routes}
        for future in as_completed(futures):
            try:
                consolidated.append(future.result())
            except Exception as exc:
                errors.append(str(exc))

    consolidated.sort(key=lambda x: x["route_id"])
    return {
        "data": consolidated,
        "errors": errors,
        "build_seconds": round(time.monotonic() - t0, 1),
    }


def _run_build_locked() -> None:
    """Corre un build completo. Si ya hay uno en curso, no hace nada (no-op)."""
    if not _build_lock.acquire(blocking=False):
        return
    try:
        _cache["building"] = True
        result = _build_consolidated()
        _cache["data"] = result["data"]
        _cache["errors"] = result["errors"]
        _cache["build_seconds"] = result["build_seconds"]
        _cache["ts"] = time.monotonic()
    finally:
        _cache["building"] = False
        _build_lock.release()


def trigger_background_build() -> bool:
    """Lanza un build en un hilo aparte. Devuelve False si ya había uno corriendo."""
    if _cache["building"]:
        return False
    threading.Thread(target=_run_build_locked, daemon=True).start()
    return True


@app.on_event("startup")
def _warm_cache_on_startup():
    # Al arrancar (deploy o al despertar tras dormir) no hay datos en memoria:
    # dispara un build inmediatamente para que el servicio se "auto-caliente".
    trigger_background_build()


@app.get(
    "/api/v1/recorridos_online_consolidado",
    summary="Todos los recorridos consolidados (paradas + puntos)",
)
def recorridos_online_consolidado():
    if _cache["data"] is None:
        raise HTTPException(
            status_code=503,
            detail="Todavia no hay datos cacheados (primer build en curso o pendiente). "
                   "Reintentar en unos minutos, o consultar /health para ver el progreso.",
        )
    return _cache["data"]


def _check_refresh_auth(provided_key: Optional[str]) -> None:
    if REFRESH_API_KEY and provided_key != REFRESH_API_KEY:
        raise HTTPException(status_code=401, detail="API key inválida o faltante (header X-API-Key).")


@app.post(
    "/api/v1/recorridos_online_consolidado/refresh",
    summary="Dispara un rebuild manual en background (no bloquea la respuesta)",
)
def refresh(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    _check_refresh_auth(x_api_key)
    started = trigger_background_build()
    return {
        "status": "started" if started else "already_building",
        "note": "Consultar /health para ver cuándo termina (cache_age_seconds vuelve a 0 al finalizar).",
    }


@app.get("/health", include_in_schema=False)
def health():
    age = round(time.monotonic() - _cache["ts"], 1) if _cache["ts"] else None
    return {
        "status": "ok",
        "building": _cache["building"],
        "cache_age_seconds": age,
        "total_recorridos": len(_cache["data"]) if _cache["data"] is not None else 0,
        "last_build_seconds": _cache["build_seconds"],
        "last_build_errors": len(_cache["errors"]),
    }
