# Guía de Consumo — Recorridos Online Consolidado API

Para cualquier sistema que vaya a integrar esta API: flujo recomendado, patrones de error, y trampas a evitar.

## Antes de empezar

**Esta API NO es un endpoint REST tradicional que devuelve datos "al instante"** como un CRUD normal. Es un **caché con rebuild asíncrono**. El patrón es similar a un job de ETL expuesto vía HTTP.

### Expectativas realistas

- **Primer request post-deploy/despertar:** 503 + esperar 4-8 minutos
- **GET con cache válido:** <1s (instantáneo)
- **POST refresh:** <1s (solo dispara, no hace el trabajo)
- **Tamaño del payload:** ~34 MB (gzipeado: ~2-3 MB)

---

## Flujo correcto paso a paso

### Escenario 1: Refresh programado diario (ej. 03:00 AM)

```
[03:00] Tu sistema (cron/scheduler):
    └─→ POST /api/v1/recorridos_online_consolidado/refresh
            Headers: X-API-Key: xxxxx
            Response: {"status": "started"}
            ↓ (no esperes más, la respuesta es instantánea)
        
[03:00] El servidor (en un thread aparte):
        └─→ Comienza a descargar 1455 rutas (~4-8 min)

[03:05] Tu sistema (5 min después, mientras esperas):
    └─→ GET /api/v1/recorridos_online_consolidado/health
        Response: {"building": true, "cache_age_seconds": 300, ...}
        → still building, vuelve a preguntar en 30s

[03:08] Tu sistema (ahora sí):
    └─→ GET /api/v1/recorridos_online_consolidado/health
        Response: {"building": false, "cache_age_seconds": 12, "total_recorridos": 1455}
        → ¡Terminó! Los datos están frescos

[03:09] Tu sistema:
    └─→ GET /api/v1/recorridos_online_consolidado
        Response: [...1455 recorridos...]
        ↓
        Procesar/importar a tu BD local, actualizar tu UI, etc.
```

**Pseudocódigo Python:**

```python
import requests
import time

API_BASE = "https://recorridos-online-consolidado.onrender.com"
API_KEY = "tu_api_key"
MAX_WAIT_SECONDS = 600  # 10 minutos timeout

def refresh_recorridos():
    # 1. Disparar refresh
    print("[*] Disparando refresh...")
    res = requests.post(
        f"{API_BASE}/api/v1/recorridos_online_consolidado/refresh",
        headers={"X-API-Key": API_KEY},
        timeout=5
    )
    res.raise_for_status()
    print(f"    Status: {res.json()['status']}")
    
    # 2. Pollear /health hasta que building=False
    print("[*] Esperando a que termine el build...")
    start = time.time()
    while True:
        elapsed = time.time() - start
        if elapsed > MAX_WAIT_SECONDS:
            raise TimeoutError(f"Build tardó más de {MAX_WAIT_SECONDS}s")
        
        health = requests.get(f"{API_BASE}/health", timeout=5).json()
        
        if not health["building"]:
            print(f"    ✓ Completado en {health['last_build_seconds']}s")
            print(f"    ✓ Total recorridos: {health['total_recorridos']}")
            if health["last_build_errors"] > 0:
                print(f"    ⚠ {health['last_build_errors']} rutas con error")
            break
        
        print(f"    ... {elapsed:.0f}s (building={health['building']})")
        time.sleep(30)  # pollear cada 30s
    
    # 3. Descargar datos
    print("[*] Descargando recorridos...")
    res = requests.get(
        f"{API_BASE}/api/v1/recorridos_online_consolidado",
        timeout=60
    )
    res.raise_for_status()
    data = res.json()
    
    print(f"    ✓ {len(data)} recorridos descargados")
    return data

# Uso
if __name__ == "__main__":
    try:
        recorridos = refresh_recorridos()
        # Aquí: procesar, importar a BD, etc.
        print(f"Procesando {len(recorridos)} recorridos...")
    except Exception as e:
        print(f"ERROR: {e}")
        # Notificar al admin, reintentar, etc.
```

---

### Escenario 2: Refresh manual (usuario hace clic en "Actualizar")

Mismo patrón, pero desde tu UI (puede ser larga espera para el usuario).

**Recomendación:** Mostrar un diálogo "Actualizando recorridos... por favor espera" que polle `/health` y cierre cuando termine.

```javascript
// JavaScript en tu frontend
async function refreshRecorridos() {
    const dialog = showLoadingDialog("Actualizando recorridos...");
    
    try {
        // 1. Disparar refresh
        await fetch('https://api.tuurl.com/api/v1/refresh', {
            method: 'POST',
            headers: { 'X-API-Key': API_KEY }
        });
        
        // 2. Pollear /health
        while (true) {
            const health = await fetch('https://api.tuurl.com/health').then(r => r.json());
            dialog.setText(`Actualizando... ${health.last_build_seconds ?? '0'}s`);
            
            if (!health.building) {
                dialog.setText(`Actualizado: ${health.total_recorridos} recorridos`);
                setTimeout(() => dialog.close(), 1000);
                break;
            }
            
            await sleep(2000);  // pollear cada 2s en UI
        }
    } catch (err) {
        dialog.showError(`Error: ${err.message}`);
    }
}
```

---

## Errores comunes y cómo evitarlos

### ❌ Error: Llamar a GET esperando data, pero server devuelve 503

```python
# MAL:
res = requests.get(f"{API_BASE}/api/v1/recorridos_online_consolidado")
data = res.json()  # crashes si es 503!
```

**Causa:** Primer request, el build todavía no terminó.

**Solución:**
```python
# BIEN:
res = requests.get(f"{API_BASE}/api/v1/recorridos_online_consolidado")
if res.status_code == 503:
    print("Datos no disponibles aún. Esperar y reintentar.")
    time.sleep(10)
    res = requests.get(f"{API_BASE}/api/v1/recorridos_online_consolidado")
res.raise_for_status()
data = res.json()
```

O mejor: **siempre polear `/health` primero** antes de pedir datos.

### ❌ Error: Disparar refresh cada vez que necesitas datos "frescos"

```python
# MAL:
while True:
    requests.post(f"{API_BASE}/refresh", ...)  # ¡cada request!
    time.sleep(5)
    data = requests.get(f"{API_BASE}/...").json()
    # Procesar...
```

**Causa:** Sobrecargas el servicio, gastas horas gratis innecesariamente, y probablemente obtengas 503 porque nunca termina un build.

**Solución:**
```python
# BIEN: refresh una vez al día
if needs_refresh():  # ej. if hora == 3:00 AM
    refresh_recorridos()
else:
    # GET normal, usa el cache
    data = requests.get(f"{API_BASE}/...").json()
```

### ❌ Error: Timeout insuficiente en requests

```python
# MAL:
requests.get(f"{API_BASE}/...", timeout=5)
```

**Causa:** El servidor podría estar descomprimiendo gzip (34 MB) o la red es lenta.

**Solución:**
```python
# BIEN:
requests.get(f"{API_BASE}/...", timeout=60)
# timeout > tiempo_build + margen
```

### ❌ Error: No manejar 401 en el refresh

```python
# MAL:
res = requests.post(f"{API_BASE}/refresh", headers={"X-API-Key": api_key})
if res.status_code != 200:
    # ignorar o asumir que funciona
```

**Causa:** `api_key` cambió en Render, el header no se envía, o está mal formado.

**Solución:**
```python
# BIEN:
res = requests.post(f"{API_BASE}/refresh", headers={"X-API-Key": api_key})
if res.status_code == 401:
    raise ValueError("API_KEY inválida o no configurada en Render. Revisar.")
res.raise_for_status()
```

### ❌ Error: Asumir que los datos están completos si hay errores

```python
health = requests.get(f"{API_BASE}/health").json()
print(f"Total: {health['total_recorridos']}")  # quizá sean 1450, no 1455
# Procesar sin notar que faltan 5 rutas
```

**Causa:** Algunas rutas fallaron durante el build (network, PaxTracker offline, etc.).

**Solución:**
```python
# BIEN:
if health["last_build_errors"] > 0:
    print(f"⚠ Advertencia: {health['last_build_errors']} rutas no se descargaron")
    print("  Revisar logs del servidor. El resultado está incompleto.")
    # Decidir si continuar o reintentar el refresh
```

---

## Consideraciones de performance

### Gzip debe estar soportado

El servidor envía `Content-Encoding: gzip`. Verifica que tu cliente lo soporta:

```python
import requests
res = requests.get(f"{API_BASE}/...")
print(res.headers.get("Content-Encoding"))  # debe ser "gzip"
```

Si tu cliente no lo soporta automáticamente, pedir al servidor que lo desactive (comentar línea en `main.py`).

### Cacheá localmente después de descargar

```python
# No:
recorridos = requests.get(f"{API_BASE}/...").json()
# usar recorridos...
# otro request
recorridos = requests.get(f"{API_BASE}/...").json()  # ¡mismos datos!

# Sí:
recorridos = requests.get(f"{API_BASE}/...").json()
guardar_en_archivo("recorridos_cache.json", recorridos)
# Usar desde archivo
# Si necesitas datos frescos, check /health y decide si refrescar
```

### Filtrá en tu BD, no en el JSON en memoria

```python
# No: cargar 34 MB, filtrar en Python
data = requests.get(f"{API_BASE}/...").json()
empresa_5 = [r for r in data if r["company"]["id"] == 5]

# Sí: importa una sola vez a BD, luego query
# (si el servidor tuviera un parámetro ?company_id=5, mejor)
for recorrido in data:
    db.insert(recorrido)
# Luego:
empresa_5 = db.query("SELECT * FROM recorridos WHERE company_id = 5")
```

---

## Monitoreo y alertas

### Sugerencias para tu sistema

1. **Log de cada refresh:**
   ```python
   import logging
   logging.info(f"Refresh iniciado a {datetime.now()}")
   logging.info(f"Build completado en {health['last_build_seconds']}s")
   logging.info(f"Total recorridos: {health['total_recorridos']}")
   if health["last_build_errors"] > 0:
       logging.warning(f"Errores: {health['last_build_errors']}")
   ```

2. **Alertas si el build falla:**
   ```python
   if health["total_recorridos"] == 0:
       send_alert("Recorridos API: build sin datos. Revisar PaxTracker.")
   if health["last_build_errors"] > 50:
       send_alert(f"Recorridos API: {health['last_build_errors']} rutas fallaron.")
   ```

3. **Alertas si el servicio duerme (Render free tier):**
   ```python
   # Si POST /refresh tarda > 60s, probablemente está durmiendo
   health = requests.get(f"{API_BASE}/health", timeout=60).json()
   if health["total_recorridos"] == 0:
       print("Servicio estaba dormido. Esperando warm-up...")
       # Reintentar luego
   ```

---

## Plan B: Si la API falla

**El servicio en Render puede fallar por:**
- Render reinicia el contenedor (deploys, actualizaciones)
- PaxTracker está offline
- Credenciales vencieron
- Límite de rate de PaxTracker alcanzado

**Qué hacer:**

1. **Mantener un backup local** de los últimos datos conocidos buenos:
   ```python
   def safe_refresh():
       try:
           return refresh_recorridos()
       except Exception as e:
           logging.error(f"Refresh falló: {e}")
           # Cargar último backup local
           with open("recorridos_backup.json") as f:
               return json.load(f)
   ```

2. **Reintentar con backoff exponencial:**
   ```python
   import backoff
   
   @backoff.on_exception(
       backoff.expo,
       requests.exceptions.RequestException,
       max_tries=5,
       max_time=600  # 10 min total
   )
   def fetch_with_retry():
       return requests.get(f"{API_BASE}/...", timeout=30)
   ```

3. **Notificar al admin si el issue persiste:**
   ```python
   if retries_exhausted:
       send_email("admin@tudominio.com", "Recorridos API: falló tras 5 reintentos")
   ```

---

## Preguntas de diseño: ¿Cuándo refrescar?

### Opción 1: Una vez al día (recomendado)

- **Ventaja:** Predecible, consume <2 horas/mes, bajo costo
- **Desventaja:** Si cambian rutas en PaxTracker a las 14:00, no lo veras hasta el próximo refresh a las 03:00
- **Uso:** Power BI reports, dashboards de gestión, análisis históricos

```python
# Cada día a las 03:00 AM
schedule.every().day.at("03:00").do(refresh_recorridos)
```

### Opción 2: Cada vez que lo solicitas (refresh manual)

- **Ventaja:** Actualización bajo demanda, flexible
- **Desventaja:** Usuario espera 4-8 minutos, consume más horas si es frecuente
- **Uso:** Botón "Actualizar ahora" en una UI admin

```python
# Endpoint en tu API que dispara POST /refresh
@app.post("/admin/refrescar_recorridos")
def admin_refresh():
    # Solo si el usuario es admin
    return refresh_recorridos()
```

### Opción 3: Híbrido (diario + manual)

- **Ventaja:** Actualización de fondo + capacidad de forzar
- **Desventaja:** Un poco más complejo
- **Uso:** La mayoría de sistemas en producción

```python
# Cron diario a las 03:00
schedule.every().day.at("03:00").do(refresh_recorridos)

# Y un endpoint manual para admins
@app.post("/admin/refrescar_recorridos")
def admin_refresh(current_user: User = Depends(get_current_admin)):
    return refresh_recorridos()  # mismo código que el cron
```

---

## Testing

### Test sin conectar a la API real

```python
# Mockear los responses
from unittest.mock import patch, MagicMock

def test_refresh_flow():
    mock_health = {"building": False, "total_recorridos": 100, ...}
    mock_data = [{"route_id": 1, ...}]
    
    with patch('requests.post') as mock_post, \
         patch('requests.get') as mock_get:
        
        mock_post.return_value.json.return_value = {"status": "started"}
        mock_get.side_effect = [
            MagicMock(json=lambda: {**mock_health, "building": True}),
            MagicMock(json=lambda: {**mock_health, "building": False}),
            MagicMock(json=lambda: mock_data)
        ]
        
        result = refresh_recorridos()
        assert len(result) == 100
```

### Test con datos reales (antes de producción)

```bash
# En un ambiente de test:
curl https://recorridos-online-consolidado.onrender.com/health
curl https://recorridos-online-consolidado.onrender.com/api/v1/recorridos_online_consolidado | jq 'length'
```

---

## Resumen: Checklist antes de producción

- [ ] Testé el flujo completo: POST /refresh → poleo /health → GET /data
- [ ] Tengo timeout ≥ 10 minutos para el polling
- [ ] Manejo 503 responses correctamente
- [ ] Tengo backup local de datos por si la API falla
- [ ] Logeo cada refresh y cualquier error
- [ ] Alertas configuradas si el build falló o tardó demasiado
- [ ] API_KEY está guardada segura, no hardcodeada
- [ ] Testé qué pasa si Render reinicia el servicio
- [ ] Documenté en mi repo dónde se integra esta API

---

Cualquier pregunta, revisar el README.md principal o los logs en Render Dashboard.
