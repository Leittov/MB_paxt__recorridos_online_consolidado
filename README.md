# Recorridos Online Consolidado API

Middleware FastAPI que consulta la API de PaxTracker, descarga todos los recorridos con sus paradas y puntos geográficos, los consolida en un único array JSON, y los expone mediante caché en memoria con refresh programable.

## Descripción general

**Problema original:** PaxTracker almacena cada recorrido en su propia versión dentro de la API. Para obtener todos los ~1455 recorridos, hay que hacer ~1455 requests GET secuenciales (muy lento) o en paralelo (requiere coordinación).

**Solución:** Este servicio:
1. Descarga todo el listado de rutas (1 request).
2. Para cada ruta con `version_id` válido, descarga su versión completa en paralelo (20 workers concurrentes, ~4-8 minutos total).
3. Extrae campos relevantes (ID, nombre, dirección, empresa, paradas con nombres/coords, puntos GPS de la ruta).
4. Consolida todo en un array ordenado por `route_id`.
5. **Cachea en memoria** dentro del contenedor — la primera consulta post-deploy/despertar dispara un build automático.
6. Permite **refresh manual en background** vía POST con API key.

## Stack

- **FastAPI** 0.111.0 — framework web async con validación automática
- **Uvicorn** 0.29.0 — ASGI server
- **Requests** 2.31.0 — HTTP client
- **Docker** + **Render.com** free tier — deployment

Tamaño de la respuesta JSON: **~34 MB** (comprimido con gzip en tránsito).

---

## Arquitectura

### Flujo de datos

```
┌─────────────────────────────────────────────────────────────────┐
│ Cliente externo (tu sistema que consume esta API)               │
└──────────────────────┬──────────────────────────────────────────┘
                       │
          ┌────────────┴──────────────┐
          │                           │
    GET /api/v1/                POST /api/v1/
    recorridos_online_          recorridos_online_
    consolidado                 consolidado/refresh
          │                           │
          ▼                           ▼
    ┌─────────────────────────────────────────┐
    │ FastAPI App (recorridos_online_api)     │
    │                                         │
    │ ┌─────────────────────────────────────┐ │
    │ │ Cache en memoria (_cache dict)      │ │
    │ │ - data: [] recorridos              │ │
    │ │ - ts: último timestamp              │ │
    │ │ - building: bool (rebuild en curso) │ │
    │ │ - errors: excepciones               │ │
    │ │ - build_seconds: tiempo del build   │ │
    │ └─────────────────────────────────────┘ │
    │                                         │
    │ ┌─────────────────────────────────────┐ │
    │ │ Build process (background thread)   │ │
    │ │ 1. Autenticarse en PaxTracker       │ │
    │ │ 2. Obtener listado de rutas         │ │
    │ │ 3. Descargar versiones en paralelo  │ │
    │ │ 4. Extraer y consolidar             │ │
    │ │ 5. Actualizar cache                 │ │
    │ └─────────────────────────────────────┘ │
    └──────────────────┬──────────────────────┘
                       │
                       ▼
         ┌──────────────────────────┐
         │ PaxTracker API (origen)  │
         │ https://paxtracker...    │
         └──────────────────────────┘
```

### Estados del cache

1. **Vacío (primera vez):** `data=None`, `building=True` → GET devuelve 503, POST /refresh devuelve `"started"`
2. **Construyéndose:** `building=True`, `data=<stale o null>` → GET espera o devuelve stale, POST devuelve `"already_building"`
3. **Válido:** `building=False`, `data=<fresh>`, `cache_age_seconds < 6h` → GET devuelve datos frescos
4. **Expirado pero disponible:** `building=False`, `data=<old>`, `cache_age_seconds >= 6h` → GET devuelve datos viejos (no refresh automático), POST /refresh inicia novo build

### Concurrencia

- **Lock `_build_lock`:** solo un thread hace build a la vez. Si dos requests llegan con cache vacío/expirado, el segundo espera.
- **Lock `_token_lock`:** protege el token global de re-login concurrente.
- **Thread daemon:** el build corre en background sin bloquear la response HTTP.

---

## Endpoints

### `GET /api/v1/recorridos_online_consolidado`

Devuelve el array consolidado de todos los recorridos.

**Request:**
```bash
curl -X GET https://recorridos-online-consolidado.onrender.com/api/v1/recorridos_online_consolidado \
  -H "Accept: application/json"
```

**Response (200 OK):**
```json
[
  {
    "route_id": 3817,
    "route_name": "Línea A - Centro",
    "company": {
      "id": 1,
      "name": "Masterbus",
      "cuit": "20-12345678-9",
      "email": "info@masterbus.net"
    },
    "route_direction": "Ida",
    "revision": 5,
    "index": 1,
    "status": "published",
    "message": null,
    "distance": 45.2,
    "route_stops": [
      {
        "index": 0,
        "name": "Estación Central",
        "lat": -33.4569,
        "lon": -70.6673,
        "waypoint": false,
        "times": ["08:00", "09:00"],
        "changed": false,
        "way": "A",
        "sort": 0
      },
      {
        "index": 1,
        "name": "Plaza de Armas",
        "lat": -33.4376,
        "lon": -70.6654,
        "waypoint": false,
        "times": ["08:05", "09:05"],
        "changed": false,
        "way": "A",
        "sort": 1
      }
    ],
    "route_points": [
      {"lat": -33.4569, "lon": -70.6673},
      {"lat": -33.4485, "lon": -70.6663},
      {"lat": -33.4376, "lon": -70.6654}
    ]
  },
  ...
]
```

**Response (503 Service Unavailable):**
```json
{
  "detail": "Todavia no hay datos cacheados (primer build en curso o pendiente). Reintentar en unos minutos, o consultar /health para ver el progreso."
}
```

**Características:**
- Nunca bloquea la respuesta HTTP.
- Devuelve datos en caché (potencialmente antiguos hasta 6 horas).
- Gzip activado automáticamente (el cliente debe aceptar `Content-Encoding: gzip`).
- Si no hay datos todavía, devuelve 503 (flujo de "esperar a que el build inicial termine").

---

### `POST /api/v1/recorridos_online_consolidado/refresh`

Dispara un rebuild completo en background. Responde inmediatamente; el build real tarda 4-8 minutos.

**Request:**
```bash
curl -X POST https://recorridos-online-consolidado.onrender.com/api/v1/recorridos_online_consolidado/refresh \
  -H "X-API-Key: tu_api_key_aqui" \
  -H "Content-Type: application/json"
```

**Response (200 OK) - Build iniciado:**
```json
{
  "status": "started",
  "note": "Consultar /health para ver cuándo termina (cache_age_seconds vuelve a 0 al finalizar)."
}
```

**Response (200 OK) - Ya hay uno en curso:**
```json
{
  "status": "already_building",
  "note": "Consultar /health para ver cuándo termina (cache_age_seconds vuelve a 0 al finalizar)."
}
```

**Response (401 Unauthorized):**
```json
{
  "detail": "API key inválida o faltante (header X-API-Key)."
}
```

**Características:**
- Header `X-API-Key` es **obligatorio** si `REFRESH_API_KEY` está configurado en Render.
- La respuesta es instantánea; el build ocurre en un thread aparte.
- No se pueden disparar múltiples builds en paralelo — el lock las serializa.

---

### `GET /health`

Estado del servicio, útil para monitoreo y polling post-refresh.

**Request:**
```bash
curl -X GET https://recorridos-online-consolidado.onrender.com/health
```

**Response (200 OK):**
```json
{
  "status": "ok",
  "building": false,
  "cache_age_seconds": 234,
  "total_recorridos": 1455,
  "last_build_seconds": 342.5,
  "last_build_errors": 0
}
```

**Campos:**
- `building` → `true` si hay un build en curso, `false` si terminó o no hay.
- `cache_age_seconds` → segundos desde que se cachearon los datos. Pasa a ~0 cuando termina un build.
- `total_recorridos` → cantidad de recorridos consolidados (0 si no hay datos todavía).
- `last_build_seconds` → cuánto tardó el último build en completarse.
- `last_build_errors` → cantidad de rutas que fallaron durante el build (se omiten del resultado final).

---

## Variables de entorno

Todas se configuran en **Render** como env vars (no en archivos `.env` locales en Render, sino en la UI Dashboard).

| Variable | Tipo | Requerida | Descripción | Ejemplo |
|----------|------|-----------|-------------|---------|
| `PAXTRACKER_EMAIL` | String | ✅ Sí | Email para login en PaxTracker API | `htoffoli@masterbus.net` |
| `PAXTRACKER_PASSWORD` | String | ✅ Sí | Password para login en PaxTracker API | (secreto) |
| `REFRESH_API_KEY` | String | ✅ Sí | Key para proteger el endpoint `/refresh` | Genera uno fuerte, p.ej. con `openssl rand -hex 32` |
| `MAX_WORKERS` | Integer | ❌ No | Cantidad de threads concurrentes para descargas (default: 20) | `20` |
| `PYTHON_VERSION` | String | ❌ No | Versión de Python (recomendado: 3.11) | `3.11.0` |

**En desarrollo local:**
Crear un archivo `.env` (no versionado) con las variables — FastAPI no lo lee automáticamente, debes cargarlas vos. O usar `python-dotenv`:

```python
# En main.py o antes de ejecutar
from dotenv import load_dotenv
load_dotenv()
```

**En Render:**
- Ir a **Dashboard → tu servicio → Environment**.
- Añadir cada variable como `sync: false` en `render.yaml` (no hardcodear valores).
- Render te pedirá los valores antes del primer deploy.

---

## Flujo típico de consumo desde tu sistema

Supongamos que tu sistema necesita actualizar datos diariamente a las 03:00 AM y permitir refresh manual también.

### Refresh programado (diario)

```bash
# 1. Disparar refresh (no bloquea)
POST /api/v1/recorridos_online_consolidado/refresh
X-API-Key: tu_api_key
→ 200 {"status": "started"}

# 2. Pollear /health cada 30 segundos hasta que building = false
GET /health
→ 200 {"building": true, "cache_age_seconds": 5, ...}
→ 200 {"building": true, "cache_age_seconds": 35, ...}
→ 200 {"building": false, "cache_age_seconds": 1, "total_recorridos": 1455, ...}

# 3. Una vez que building = false y cache_age_seconds < 10, pedir los datos
GET /api/v1/recorridos_online_consolidado
→ 200 [{ route_id: 3817, ... }, ...]

# 4. Procesar el array localmente (import a BD, etc.)
```

### Refresh manual

Mismo flujo, pero disparado por un usuario en tu UI (botón "Actualizar ahora").

### Timeout recomendado

- **POST /refresh → respuesta:** < 1s (el servidor solo inicia el thread)
- **Pollear /health:** configura timeout de 10 minutos; si no termina en ese tiempo, falló algo
- **GET /recorridos_online_consolidado:** esperar a que `/health` diga `building=false`, luego <5s (gzip descomprime localmente)

---

## Desarrollo local

### Setup

```bash
# Clonar/descargar en tu máquina
cd recorridos_online_api

# Crear venv
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Instalar deps
pip install -r requirements.txt

# Crear .env local
cat > .env << 'EOF'
PAXTRACKER_EMAIL=htoffoli@masterbus.net
PAXTRACKER_PASSWORD=hernan1991
REFRESH_API_KEY=mi_api_key_local_123
MAX_WORKERS=20
EOF
```

### Correr localmente

```bash
uvicorn main:app --reload --port 8000
```

- API estará en `http://localhost:8000`
- Docs interactivos en `http://localhost:8000/docs`
- ReloadChange automático si editas `main.py`

### Testing local

```bash
# 1. Esperar ~30s a que startup completa el primer build
curl http://localhost:8000/health

# 2. Si building=true, esperar más
# 3. Una vez building=false:
curl http://localhost:8000/api/v1/recorridos_online_consolidado | jq 'length'

# 4. Probar refresh
curl -X POST http://localhost:8000/api/v1/recorridos_online_consolidado/refresh \
  -H "X-API-Key: mi_api_key_local_123"

# 5. Pollear
watch -n 5 'curl -s http://localhost:8000/health | jq .building'
```

---

## Deployment en Render

### Prerequisitos

1. Cuenta en **Render.com** (free tier)
2. Repositorio Git con la carpeta `recorridos_online_api` (mismo patrón que Excesos2)
3. Variables de entorno preparadas

### Pasos

1. **Git setup** (si no lo hiciste)
   ```bash
   cd recorridos_online_api
   git init
   git add -A
   git commit -m "Initial commit: recorridos_online_consolidado API"
   git remote add origin https://github.com/tu_usuario/recorridos_online_api.git
   git push -u origin main
   ```

2. **Render Dashboard**
   - Ir a **https://dashboard.render.com**
   - Click **"+ New" → Web Service**
   - Seleccionar tu repo (conectar GitHub si no lo hiciste)
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Instance Type:** Free (recomendado para arrancar)
   - **Environment variables:**
     ```
     PYTHON_VERSION = 3.11.0
     PAXTRACKER_EMAIL = htoffoli@masterbus.net  (secreto)
     PAXTRACKER_PASSWORD = xxxxxx               (secreto)
     REFRESH_API_KEY = xxxxxxxx                 (secreto)
     MAX_WORKERS = 20
     ```
   - Click **"Create Web Service"**

3. **Esperar deploy** (~3-5 minutos)
   - Render construirá la imagen Docker, la subirá, y la ejecutará
   - El servicio tendrá una URL como `https://recorridos-online-consolidado.onrender.com`
   - El hook de `startup` dispara el primer build automáticamente

4. **Verificar**
   ```bash
   curl https://recorridos-online-consolidado.onrender.com/health
   ```

### Monitoreo en Render

- **Logs:** Dashboard → tu servicio → **Logs** (ver errores de build, login, network)
- **Metrics:** CPU, memoria, tiempo de respuesta
- **Health checks:** Render pinga `/health` cada 30s para verificar que sigue vivo

---

## Modificaciones comunes

### Agregar más campos a cada recorrido

En `_extract_fields()`:

```python
def _extract_fields(version_data: Dict, route_meta: Dict) -> Dict:
    v = version_data.get("data", {}).get("version", {})
    # ... código existente ...
    
    return {
        # ... campos existentes ...
        "nuevo_campo": v.get("nuevo_campo"),  # ← agregar aquí
    }
```

### Cambiar el TTL del cache

En `render.yaml`, cambiar la variable (actualmente no la hay, pero podés agregar):

```yaml
- key: CACHE_TTL_SECONDS
  value: "21600"  # 6 horas. Cambiar a 3600 = 1 hora, 43200 = 12 horas, etc.
```

En `main.py`, agregar la lectura (ya está en el código anterior, pero no en render.yaml):

```python
CACHE_TTL = int(os.environ.get("CACHE_TTL_SECONDS", str(6 * 3600)))
```

Luego usarla donde decía "nunca bloqueante" — si querés que GET bloqueé hasta que haya datos frescos, esto requiere redesign.

### Agregar filtros al GET

Ej., filtrar por `company_id` o rango de `route_id`:

```python
from fastapi import Query

@app.get("/api/v1/recorridos_online_consolidado")
def recorridos_online_consolidado(
    company_id: Optional[int] = Query(None, description="Filtrar por ID de empresa")
):
    data = get_consolidated_data()
    if company_id is not None:
        data = [r for r in data if r["company"]["id"] == company_id]
    return data
```

### Cambiar el refresh a programado (cron interno)

Actualmente es manual (vos disparas POST). Para hacerlo automático cada 6h desde el mismo servicio:

```python
from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler()

@app.on_event("startup")
def start_scheduler():
    trigger_background_build()  # ya existe
    scheduler.add_job(trigger_background_build, 'interval', hours=6)
    scheduler.start()

@app.on_event("shutdown")
def shutdown_scheduler():
    scheduler.shutdown()
```

Requiere `pip install apscheduler` en `requirements.txt`.

---

## Troubleshooting

### GET `/api/v1/recorridos_online_consolidado` devuelve 503

**Causa probable:** Primer build aún en curso o falló.

**Diagnóstico:**
```bash
curl https://tu_url/health
```

- Si `building: true` → esperar 4-8 minutos
- Si `building: false` y `total_recorridos: 0` → falló el build, revisar logs

**Revisar logs en Render:**
Dashboard → Logs → buscar `ERROR` o `Exception`

### POST `/refresh` devuelve 401

**Causa:** `X-API-Key` no coincide o falta.

```bash
# Verificar que el header esté bien
curl -X POST https://tu_url/refresh \
  -H "X-API-Key: el_valor_correcto_de_REFRESH_API_KEY"
```

### Build tarda más de 10 minutos

**Causa probable:** 
1. Red lenta (Render en us-east, PaxTracker en Argentina = latencia)
2. PaxTracker API respondiendo lentamente ese día
3. MAX_WORKERS muy bajo (por defecto 20)

**Solución:**
1. Aumentar `MAX_WORKERS` a 30 en `render.yaml` (cuidado: más CPU/memoria)
2. Revisar logs de Render para ver tiempos de request individual
3. Si es sistemático, contactar soporte de PaxTracker

### Render sleep (free tier se duerme tras 15 min sin tráfico)

Cuando el servicio despierta:
1. Cold start (~30-60s)
2. Hook `startup` dispara build automático (~4-8 min)
3. Primer GET espera a que termine

Si necesitas respuesta rápida siempre, considera un plan pago o hacer ping cada 10 min.

### El json descargado tiene 34 MB sin gzip — ¿es problema?

**No.** Con gzip son ~2-3 MB en tránsito. La mayoría de clientes HTTP lo soportan natively. Si el tuyo no:

```python
# Desactivar gzip (en main.py)
# app.add_middleware(GZipMiddleware, minimum_size=1000)  # ← comentar
```

---

## Costos en Render (free tier)

- **750 horas/mes** compartidas entre todos tus servicios free (Excesos2 + este)
- **Duración del build:** ~4-8 min = ~0.067 horas por build
- **Refresh diario:** 24 builds/mes = ~1.6 horas/mes
- **Inactividad (dormir):** no consume
- **Total estimado:** <2 horas/mes si solo refrescas 1x diario, sobra mucho

Si planeas consumir el endpoint constantemente o agregar más servicios, evalúa Starter ($7/mes) que no duerme.

---

## Preguntas frecuentes

**¿Puedo limitar el resultado a X recorridos?**
Sí, agregar paginación en el GET (ver sección "Modificaciones comunes").

**¿Qué pasa si falla una ruta individual durante el build?**
Se registra en `last_build_errors` pero no detiene el build. Otros 1454 se descarga normalmente. Revisar logs para ver cuáles fallaron.

**¿Puedo usar el mismo endpoint desde múltiples clientes?**
Sí, está diseñado para eso. El caché es compartido.

**¿Es seguro dejar el email/password en Render?**
Sí si usas `sync: false` en `render.yaml` (las variables se cargan en UI Dashboard, no en el archivo versionado). Mejor aún: cambiar la credencial en PaxTracker a un user de servicio dedicado.

**¿Qué pasa si el token de PaxTracker expira durante el build?**
Está manejado — `_authed_get()` reintenta login automáticamente si recibe 401.

---

## Contacto / Issues

Cualquier problema durante desarrollo/deployment, revisar:
1. Logs en Render Dashboard
2. `/health` endpoint para estado actual
3. Variables de entorno están configuradas correctamente
4. Credenciales de PaxTracker siguen válidas (cambió password recientemente?)
