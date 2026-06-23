# Documentación Técnica — Recorridos Online Consolidado API

Detalles de implementación, decisiones de diseño, y cómo debuggear problemas.

## Estructura del código

```
recorridos_online_api/
├── main.py              # Aplicación FastAPI (único archivo de lógica)
├── requirements.txt     # Dependencias
├── Dockerfile           # Build para Render
├── render.yaml          # Configuración de deploy en Render
├── .gitignore           # Archivos a no versionar
├── README.md            # Guía principal
├── CONSUMER_GUIDE.md    # Para quién consume la API
└── TECHNICAL.md         # Este archivo
```

---

## Flujo interno paso a paso

### 1. Startup

```python
@app.on_event("startup")
def _warm_cache_on_startup():
    trigger_background_build()
```

Se ejecuta cuando Uvicorn inicia. Dispara un build inmediatamente (en un thread aparte) para que si llega un request rápidamente, haya algo en cache.

### 2. Request GET `/api/v1/recorridos_online_consolidado`

```python
@app.get("/api/v1/recorridos_online_consolidado", ...)
def recorridos_online_consolidado():
    if _cache["data"] is None:
        raise HTTPException(status_code=503, ...)
    return _cache["data"]
```

**Garantías:**
- Nunca bloquea (no llama a `get_consolidated_data()` que esperaría)
- Devuelve inmediatamente: datos viejos, frescos, o 503 si no hay
- No hay race conditions: si otro thread está actualizando `_cache["data"]`, Python GIL lo protege (asignación atómica de referencias)

### 3. Request POST `/api/v1/recorridos_online_consolidado/refresh`

```python
def refresh(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    _check_refresh_auth(x_api_key)
    started = trigger_background_build()
    return {
        "status": "started" if started else "already_building",
        ...
    }

def trigger_background_build() -> bool:
    """Devuelve False si ya hay uno en curso."""
    if _cache["building"]:
        return False
    threading.Thread(target=_run_build_locked, daemon=True).start()
    return True
```

**Flujo:**
1. Valida API key (401 si falla)
2. Chequea si ya hay un build corriendo (`_cache["building"]`)
3. Si no → crea un thread daemon y devuelve `"started"` **inmediatamente**
4. Si sí → devuelve `"already_building"` (no inicia otro)

**El thread daemon hace:**
```python
def _run_build_locked() -> None:
    if not _build_lock.acquire(blocking=False):  # ← try-lock sin esperar
        return  # otro thread ya lo tiene, nos vamos
    try:
        _cache["building"] = True
        result = _build_consolidated()  # ← el trabajo de verdad (4-8 min)
        _cache["data"] = result["data"]
        _cache["errors"] = result["errors"]
        _cache["build_seconds"] = result["build_seconds"]
        _cache["ts"] = time.monotonic()
    finally:
        _cache["building"] = False
        _build_lock.release()
```

**Punto clave:** `_build_lock.acquire(blocking=False)` es un try-lock. Si otro thread ya está en la sección crítica, este retorna `False` y se va. **No espera**. Esto evita que dos threads intenten hacer build simultáneamente.

### 4. El build en sí (`_build_consolidated()`)

```python
def _build_consolidated() -> Dict[str, Any]:
    t0 = time.monotonic()
    
    # 1. Obtener listado de rutas (1 request)
    routes = _fetch_routes_list()  # GET /api/routes
    valid_routes = [r for r in routes if r.get("version_id")]
    
    # 2. Descargar cada versión en paralelo
    consolidated: List[Dict] = []
    errors: List[str] = []
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # Crear futures para cada ruta
        futures = {executor.submit(_fetch_route_version, r): r for r in valid_routes}
        
        # Procesar conforme terminen (no esperar a que terminen todas)
        for future in as_completed(futures):
            try:
                consolidated.append(future.result())
            except Exception as exc:
                errors.append(str(exc))
    
    # 3. Ordenar y devolver
    consolidated.sort(key=lambda x: x["route_id"])
    return {
        "data": consolidated,
        "errors": errors,
        "build_seconds": round(time.monotonic() - t0, 1),
    }
```

**Detalles:**
- `_fetch_routes_list()` → `GET /api/routes` con reintento de login si falla
- `ThreadPoolExecutor(max_workers=20)` → 20 threads concurrentes
- `as_completed()` → itera conforme termina cada future (no espera a que todas terminen)
- Errores individuales no detienen el build; se registran y continúa
- Ordenamiento final por `route_id` asegura determinismo

**Tiempo estimado:**
- 1 request del listado: ~1s
- 1455 requests de versiones con 20 workers: ~1455 × 0.96s / 20 workers ≈ 70s (secuencial promedio), pero paralelo es mucho más rápido
- En la práctica: ~240-480s (4-8 min) observado desde Render

### 5. Request GET `/health`

```python
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
```

Devuelve estado actual sin hacer nada (no bloquea, no dispara builds).

---

## Variables globales y sincronización

```python
_cache: Dict[str, Any] = {
    "data": None,  # array de recorridos
    "ts": 0.0,  # timestamp (monotonic) del cache
    "errors": [],  # excepciones durante el build
    "build_seconds": None,  # duración del último build
    "building": False,  # si hay un build en curso
}

_build_lock = threading.Lock()  # sincroniza _run_build_locked()
_token_lock = threading.Lock()  # sincroniza _get_token() / _login()
_token: Optional[str] = None  # token JWT actual de PaxTracker
```

**Invariantes:**
- `_cache` es accesible desde múltiples threads, pero solo el thread del build actualiza sus valores. Los readers (GET) nunca modifican, solo leen.
- `_build_lock` serializa builds: solo un thread en `_run_build_locked()` a la vez
- `_token_lock` protege el token global: si dos threads necesitan login, solo uno se ejecuta

### ¿Por qué no deadlock?

```
Thread A (GET /refresh)         Thread B (GET /health)
    └─ acquire(_build_lock)          └─ (no toca locks)
    └─ _run_build_locked()               └─ lee _cache (sin lock!)
    └─ _build_consolidated()
       └─ acquire(_token_lock)
       └─ _login()
       └─ release(_token_lock)
    └─ actualiza _cache
    └─ release(_build_lock)
```

No hay deadlock porque:
1. GET nunca intenta adquirir locks (es read-only)
2. POST /refresh no espera dentro de un lock antes de adquirir otro lock
3. Los locks están separados por propósito (build vs token)

### ¿Qué pasa con las "lecturas sucias" (dirty reads)?

Si Thread A está actualizando `_cache["data"]` mientras Thread B hace GET:
```python
# Thread A
_cache["data"] = new_data  # asignación de referencia, atómica en CPython

# Thread B (simultáneamente)
data = _cache["data"]  # Lee referencia, podría ser old o new
return data
```

En CPython, las asignaciones de referencias a nivel de bytecode son atómicas (GIL). En el peor caso, devuelves datos viejos un momento antes de que se actualicen — es tolerable (el cliente ve un refresh atrasado pero no corrupto).

Si necesitaras garantía 100% de que el GET espere a que termine el build, necesitarías:
```python
with _build_lock:
    if _cache["data"] is not None:
        return _cache["data"]  # Esperar bloqueante
```

Pero eso ralentizaría mucho; el diseño actual prioriza latencia baja (GET rápido) sobre consistencia estricta.

---

## Autenticación en PaxTracker

### Login y caché de token

```python
def _login() -> str:
    global _token
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
```

**Decisiones:**
- El token se cachea en memoria globalmente (`_token`)
- Si es `None` (primer startup), llama a `_login()` automáticamente
- Múltiples threads usan el mismo token (sin re-login por cada uno)

### Reintento de login en 401

```python
def _authed_get(url: str, **kwargs) -> requests.Response:
    token = _get_token()
    res = requests.get(url, headers={"Authorization": f"Bearer {token}"}, **kwargs)
    if res.status_code == 401:  # Token expiró
        token = _login()  # Reautenticar
        res = requests.get(url, headers={"Authorization": f"Bearer {token}"}, **kwargs)
    return res
```

Si el servidor devuelve 401, reintentas una sola vez con login nuevo. Si sigue siendo 401, el error se propaga (el build falla).

---

## Manejo de errores durante el build

```python
for future in as_completed(futures):
    try:
        consolidated.append(future.result())
    except Exception as exc:
        errors.append(str(exc))  # ← registra pero continúa
```

**Implicaciones:**
- Si 5 de 1455 rutas fallan → el resultado tiene 1450 recorridos, `errors` tiene 5 entradas
- El build NO falla completamente; `total_recorridos` baja pero no sale cero
- El consumidor debe chequear `last_build_errors` para saber si hay gaps

**Tipos de error común:**
```
"route 3817 version 5: HTTP 404"  # versión no existe
"route 3818 version 6: [Errno 110] Connection timed out"  # network error
"route 3819 version 7: 401"  # token inválido (debería haber reautenticado, pero falló)
```

---

## Serialización JSON

```python
def _extract_fields(version_data: Dict, route_meta: Dict) -> Dict:
    v = version_data.get("data", {}).get("version", {})
    
    # Shallow copy + mapping de campos
    return {
        "route_id": v.get("route_id") or route_meta.get("id"),
        ...
    }
```

**Por qué esta estructura:**
- `version_data` es la respuesta cruda de `GET /api/routes/{id}/versions/{vid}`
- Tiene anidación profunda (`response.data.version.route_stops[].times`)
- `_extract_fields()` toma solo lo que importa, ignorando campos no necesarios
- Reduce tamaño del JSON final y evita serializar objetos problemáticos (Dates se convierten a strings, etc.)

---

## Performance y límites

### Memoria

- **JSON cacheado en memoria:** ~34 MB (un array de dicts Python es algo más)
- **ThreadPoolExecutor (20 workers):** ~10-20 MB (stacks de threads)
- **Total:** <100 MB en uso pico — cabe fácilmente en Render free (512 MB available)

### Network

- **Requests simultáneos:** 20 (MAX_WORKERS) → rate limit de PaxTracker?
- **Probabilidad de 429 (too many requests):** Baja, pero posible
- **Mitigation:** Si PaxTracker devuelve 429, el build falla (error registrado). Solución: bajar MAX_WORKERS a 10 o 15.

### CPU

- **Build time:** I/O bound (network), no CPU bound
- **Render free:** CPU compartida, suficiente para este uso

---

## Debugging

### Logs en desarrollo local

```bash
uvicorn main:app --log-level debug
```

Verás:
```
INFO:     Uvicorn running on http://127.0.0.1:8000
INFO:     Application startup complete
[*] Disparando refresh...
```

### Logs en Render

Dashboard → tu servicio → **Logs** → filtra por:
- `ERROR` → excepciones
- `route` → errores de rutas específicas
- `building` → estado del build

### Debugging del flujo completo

```bash
# Terminal 1: Servidor local
uvicorn main:app

# Terminal 2: Tests
curl http://localhost:8000/health
# {"building": true, ...}  ← espera a que pase a false

curl http://localhost:8000/api/v1/recorridos_online_consolidado
# {"detail": "...503..."} o array si building=false

curl -X POST http://localhost:8000/api/v1/recorridos_online_consolidado/refresh \
  -H "X-API-Key: test"
# {"status": "started"}

# Pollear
watch -n 2 'curl -s http://localhost:8000/health | jq "{building, cache_age: .cache_age_seconds, total: .total_recorridos}"'
```

### Si el build falla permanentemente

**Pasos:**
1. Revisar logs en Render
2. Chequear si PaxTracker API está disponible (curl desde tu máquina)
3. Verificar credenciales en Render env vars (¿cambió password en PaxTracker?)
4. Si es 429 (rate limit), bajar MAX_WORKERS
5. Si es timeout (>30s), PaxTracker está muy lento — reducir MAX_WORKERS para evitar sobrecargar

---

## Modificaciones tipicas

### Agregar un nuevo campo a cada recorrido

1. Identifica dónde viene en el response de PaxTracker:
   ```bash
   curl -H "Authorization: Bearer TOKEN" \
     https://paxtracker.masterbus.net/api/routes/3817/versions/5 | jq '.data.version.nuevo_campo'
   ```

2. Agrégalo a `_extract_fields()`:
   ```python
   return {
       ...,
       "nuevo_campo": v.get("nuevo_campo"),
   }
   ```

3. Test local: `curl http://localhost:8000/api/v1/...` y verifica que aparezca

### Cambiar MAX_WORKERS

En `render.yaml`:
```yaml
envVars:
  - key: MAX_WORKERS
    value: "10"  # más bajo = menos carga, pero más lento
```

O en desarrollo:
```bash
MAX_WORKERS=10 uvicorn main:app
```

### Agregar rate limiting

```python
from slowapi import Limiter
limiter = Limiter(key_func=lambda: "global")

@app.get("/api/v1/recorridos_online_consolidado")
@limiter.limit("10/minute")
def recorridos_online_consolidado():
    ...
```

Requiere `pip install slowapi`.

### Agregar autenticación en GET (además de POST)

```python
def verify_auth(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    if x_api_key != REFRESH_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True

@app.get("/api/v1/recorridos_online_consolidado")
def recorridos_online_consolidado(auth: bool = Depends(verify_auth)):
    if _cache["data"] is None:
        raise HTTPException(status_code=503, ...)
    return _cache["data"]
```

---

## Testing

### Unit test: Verificar que _extract_fields maneja campos faltantes

```python
def test_extract_fields_missing_fields():
    version_data = {"data": {"version": {}}}  # vacío
    route_meta = {"id": 1, "name": "Test"}
    result = _extract_fields(version_data, route_meta)
    
    assert result["route_id"] == 1
    assert result["route_name"] == "Test"
    assert result["route_stops"] == []
    assert result["route_points"] == []
```

### Integration test: Mock de API PaxTracker

```python
from unittest.mock import patch

def test_build_consolidated_success(monkeypatch):
    mock_routes = [
        {"id": 1, "version_id": 1, "name": "R1"},
        {"id": 2, "version_id": 2, "name": "R2"},
    ]
    mock_version = {
        "data": {"version": {
            "route_id": 1,
            "route_name": "R1",
            "route_stops": [],
            "route_points": []
        }}
    }
    
    def mock_authed_get(url, **kwargs):
        resp = MagicMock()
        if "/routes" in url and "versions" not in url:
            resp.status_code = 200
            resp.json.return_value = {"data": {"routes": mock_routes}}
        else:
            resp.status_code = 200
            resp.json.return_value = mock_version
        return resp
    
    monkeypatch.setattr("main._authed_get", mock_authed_get)
    
    result = _build_consolidated()
    assert len(result["data"]) == 2
    assert result["errors"] == []
```

---

## Roadmap de mejoras posibles

- [ ] Persistencia en disco (para no re-descargar tras restart)
- [ ] Webhook POST a una URL cuando termina el build (en lugar de que el cliente polee)
- [ ] Paginación en GET (ej. `?limit=100&offset=0`)
- [ ] Filtros en GET (ej. `?company_id=5`)
- [ ] Compresión de datos (MessagePack en lugar de JSON)
- [ ] Caché distribuida (Redis) si se escala a múltiples instancias
- [ ] Métricas Prometheus (para monitoreo en Render)
- [ ] Health check más robusta (ping a PaxTracker internamente)

---

## Contacto para bugs

Cualquier issue:
1. Revisar logs en Render
2. Reproductir localmente
3. Agregar logs más detallados si es necesario
4. Abrir issue en GitHub con:
   - Pasos para reproducir
   - Logs relevantes
   - Comportamiento esperado vs actual
