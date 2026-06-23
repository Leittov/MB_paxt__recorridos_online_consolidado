# Índice de Documentación

Guía rápida para encontrar lo que necesitas según tu rol.

---

## 🎯 Por rol

### 👤 Soy un **desarrollador/analista que consume la API**
*Necesito entender cómo usarla desde otro sistema/script.*

**Lee en este orden:**
1. [README.md](README.md) — **Sección "Endpoints"** (ver qué endpoints hay, qué devuelven)
2. [CONSUMER_GUIDE.md](CONSUMER_GUIDE.md) — **Todo** (flujos correctos, errores comunes, ejemplos)
3. [README.md](README.md) — **Sección "Troubleshooting"** (qué hacer si algo falla)

**Archivos de referencia:**
- `requirements.txt` — dependencias (si necesitas montar algo similar)
- `render.yaml` — variables de entorno que debo configurar para que funcione

---

### 🔧 Soy un **DevOps / SRE que despliega / monitorea**
*Necesito saber cómo deployar, configurar, y mantener el servicio en Render.*

**Lee en este orden:**
1. [README.md](README.md) — **Secciones "Variables de entorno" + "Deployment en Render"**
2. [README.md](README.md) — **Sección "Monitoreo en Render"**
3. [TECHNICAL.md](TECHNICAL.md) — **Sección "Debugging"** (resolver problemas de deploy)

**Comandos clave:**
```bash
# Deploy en Render
git push origin main  # Trigger automático si está conectado

# Verificar health
curl https://tu_url/health | jq .

# Ver logs en Render
# Dashboard → Logs
```

**Variables a configurar (antes del primer deploy):**
- `PAXTRACKER_EMAIL` (secret)
- `PAXTRACKER_PASSWORD` (secret)
- `REFRESH_API_KEY` (secret, genera con `openssl rand -hex 32`)

---

### 👨‍💻 Soy un **desarrollador del código mismo**
*Necesito entender la arquitectura, modificar features, o debuggear bugs.*

**Lee en este orden:**
1. [TECHNICAL.md](TECHNICAL.md) — **Secciones "Arquitectura" + "Flujo interno paso a paso"**
2. [README.md](README.md) — **Sección "Endpoints"** (para entender qué hace cada ruta)
3. [TECHNICAL.md](TECHNICAL.md) — **Secciones "Sincronización" + "Debugging"**
4. [TECHNICAL.md](TECHNICAL.md) — **Sección "Modificaciones típicas"** (si vas a cambiar algo)

**Para agregar features:**
- Nuevo campo en recorridos → [TECHNICAL.md](TECHNICAL.md) sección "Agregar un nuevo campo"
- Rate limiting → [TECHNICAL.md](TECHNICAL.md) sección "Agregar rate limiting"
- Autenticación en GET → [TECHNICAL.md](TECHNICAL.md) sección "Agregar autenticación"

**Para debuggear:**
1. Runear localmente: `uvicorn main:app --log-level debug`
2. Seguir flujo en [TECHNICAL.md](TECHNICAL.md) sección "Debugging"
3. Si es en producción, revisar Render logs

---

### 📊 Soy un **data engineer / analista que voy a integrar la API en un pipeline**
*Necesito entender qué datos hay, cómo refrescar, y tolerancia a fallos.*

**Lee en este orden:**
1. [README.md](README.md) — **Sección "Endpoints"** (qué devuelve el GET)
2. [CONSUMER_GUIDE.md](CONSUMER_GUIDE.md) — **Sección "Flujo correcto paso a paso"** (scenario 1: refresh programado)
3. [CONSUMER_GUIDE.md](CONSUMER_GUIDE.md) — **Sección "Consideraciones de performance"** (caché local, gzip)
4. [CONSUMER_GUIDE.md](CONSUMER_GUIDE.md) — **Sección "Plan B: Si la API falla"** (backup local, reintentos)

**Pseudocódigo para tu pipeline:**
```python
from my_consumer_lib import refresh_recorridos

# En tu scheduler (Airflow, etc.)
@dag(schedule="0 3 * * *")  # 03:00 AM diario
def update_recorridos():
    try:
        recorridos = refresh_recorridos(timeout=600)  # 10 min
        cargar_a_bd(recorridos)
    except Exception as e:
        cargar_desde_backup()  # Plan B
        alertar(f"Refresh falló: {e}")
```

---

### 📚 Soy un **PM / stakeholder que necesita entender el scope**
*Necesito saber qué es esto, por qué existe, y cuáles son las limitaciones.*

**Lee:**
1. [README.md](README.md) — **Secciones "Descripción general" + "Arquitectura"** (qué es y por qué)
2. [README.md](README.md) — **Sección "Costos en Render"** (cuánto cuesta)
3. [CONSUMER_GUIDE.md](CONSUMER_GUIDE.md) — **Sección "Plan B: Si la API falla"** (qué pasa si se cae)

**Puntos clave para presentar:**
- ✅ Consolidación de ~1455 recorridos en un único JSON (~34 MB)
- ⏱️ Tarda 4-8 minutos en actualizar (no es real-time)
- 💰 Muy bajo costo (free tier de Render, <2 USD/mes si escalamos)
- 🔄 Refresh manual + programado (flexible)
- 🛡️ Protegido por API key

---

## 📋 Referencia rápida: archivos

| Archivo | Para qué | Tamaño | Actualización |
|---------|----------|--------|---------------|
| [README.md](README.md) | Guía principal: qué es, endpoints, variables, deploy, troubleshooting | ~20 KB | Cada cambio importante |
| [CONSUMER_GUIDE.md](CONSUMER_GUIDE.md) | Cómo consumir la API correctamente, errores comunes, ejemplos | ~14 KB | Cuando el API cambia de comportamiento |
| [TECHNICAL.md](TECHNICAL.md) | Internals: arquitectura, flujo, debugging, modificaciones | ~16 KB | Cuando el código cambia |
| main.py | Código fuente único | ~8 KB | El producto |
| requirements.txt | Dependencias Python | <1 KB | Cuando agregamos librerías |
| render.yaml | Configuración de Render | <1 KB | Cuando cambian env vars o comandos |
| Dockerfile | Build image para Render | <1 KB | Rara vez |
| .gitignore | Archivos a no versionar | <1 KB | Si hay nuevos temp files |

---

## 🔄 Flujo de actualización de documentación

Cuando algo cambia en el código/diseño:

1. **Cambio en el endpoint GET/POST** → Actualizar **README.md sección Endpoints** + **CONSUMER_GUIDE.md**
2. **Cambio en variables de entorno** → Actualizar **README.md sección Variables** + **render.yaml**
3. **Cambio en lógica interna (locks, threads, etc.)** → Actualizar **TECHNICAL.md**
4. **Nuevo error conocido** → Agregar a **README.md Troubleshooting** + **CONSUMER_GUIDE.md Errores comunes**
5. **Mejora en performance/costo** → Actualizar **README.md sección Costos**

---

## ❓ FAQ por rol

### "¿Dónde veo qué campos tiene cada recorrido?"
→ [README.md](README.md) sección "Endpoints → Response (200 OK)"

### "¿Cómo protejo el endpoint /refresh para que no lo usen otros?"
→ [README.md](README.md) sección "Variables de entorno" + [TECHNICAL.md](TECHNICAL.md) sección "Autenticación en PaxTracker"

### "¿Cómo debuggeo si el build falla?"
→ [TECHNICAL.md](TECHNICAL.md) sección "Debugging" + [README.md](README.md) sección "Troubleshooting"

### "¿Cuánto tiempo tarda en actualizar?"
→ [README.md](README.md) sección "Estimación de duración"

### "¿Qué pasa si Render reinicia el servicio?"
→ [CONSUMER_GUIDE.md](CONSUMER_GUIDE.md) sección "Plan B: Si la API falla" + [README.md](README.md) sección "Render sleep"

### "¿Puedo agregar un nuevo campo?"
→ [TECHNICAL.md](TECHNICAL.md) sección "Agregar un nuevo campo a cada recorrido"

### "¿Cómo testeo localmente?"
→ [README.md](README.md) sección "Desarrollo local → Testing local"

### "¿Cuál es el costo mensual?"
→ [README.md](README.md) sección "Costos en Render (free tier)"

---

## 🚀 Primeros pasos por rol

### Si eres **consumidor** (dev que usa la API):
```bash
# 1. Lee CONSUMER_GUIDE.md
# 2. Copia el pseudocódigo Python
# 3. Integra en tu sistema
# 4. Test local contra servicio de staging (si existe)
# 5. Deploy a prod
```

### Si eres **DevOps** (deploy/monitoreo):
```bash
# 1. Lee README.md secciones Variables + Deployment
# 2. Genera REFRESH_API_KEY: openssl rand -hex 32
# 3. Configura en Render Dashboard
# 4. Push código a GitHub
# 5. Monitorea en Render Logs
```

### Si eres **desarrollador del código**:
```bash
# 1. Lee TECHNICAL.md Arquitectura
# 2. Clone el repo
# 3. Crea .env local con credenciales
# 4. uvicorn main:app --reload
# 5. Testa en http://localhost:8000/docs
# 6. Modifica + push
```

### Si eres **data engineer**:
```bash
# 1. Lee CONSUMER_GUIDE.md flujo 1
# 2. Diseña schedule (Airflow, Cron, etc.)
# 3. Implementa retry logic + backup local
# 4. Test end-to-end
# 5. Deploy
```

---

## 📞 Soporte

- **Problemas técnicos:** Revisar [README.md](README.md) Troubleshooting + [TECHNICAL.md](TECHNICAL.md) Debugging
- **Cómo consumir:** [CONSUMER_GUIDE.md](CONSUMER_GUIDE.md)
- **Errores al integrar:** [CONSUMER_GUIDE.md](CONSUMER_GUIDE.md) sección "Errores comunes"
- **Deploy / configuración:** [README.md](README.md) sección "Deployment en Render"
- **Cambios futuros:** Revisar [TECHNICAL.md](TECHNICAL.md) sección "Roadmap"

---

**Última actualización:** 2026-06-23
**Versión API:** 1.0.0
