# Venezuela te Busca

Sistema humanitario SAR/DVI para búsqueda de personas desaparecidas y monitoreo de edificios dañados tras el terremoto en Venezuela (2026).

**Repositorio:** [github.com/jomstudiovzla/VENEZUELATEBUSCA](https://github.com/jomstudiovzla/VENEZUELATEBUSCA)

## Fuentes de datos

- **Desaparecidos:** [desaparecidosterremotovenezuela.com](https://desaparecidosterremotovenezuela.com/)
- **Edificios dañados:** [terremotovenezuela.com](https://terremotovenezuela.com/)

## Funciones

- Visor unificado: desaparecidos, edificios, mapa Leaflet, cámaras SAR 24h y emergencias
- Tiempo real vía WebSocket (~20 s) y dashboard instantáneo
- Búsqueda por nombre o cédula (~40k registros en BD local)
- **Reportar persona o edificio** con foto obligatoria (botones en visor)
- Emergencias por operadora: **171** CANTV · **\*1** Movilnet · **112** Digitel · **911** Movistar
- Descarga automática de fotos de desaparecidos y edificios
- Cámaras con video en vivo (RTSP/MJPEG) y detección de personas
- Perfil forense por persona

## Requisitos

- **Python 3.11+** (3.12 recomendado)
- macOS / Linux (Windows con WSL también funciona)
- ~1 GB de espacio libre (repo + fotos + BD)
- Conexión a internet (para actualizar fuentes en vivo)

## Instalación rápida

```bash
git clone https://github.com/jomstudiovzla/VENEZUELATEBUSCA.git
cd VENEZUELATEBUSCA

chmod +x instalar.sh arrancar.sh
./instalar.sh
./arrancar.sh
```

Abrir en el navegador: **http://127.0.0.1:8000/**

### Instalación manual

```bash
git clone https://github.com/jomstudiovzla/VENEZUELATEBUSCA.git
cd VENEZUELATEBUSCA

python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-core.txt
cp .env.example .env
uvicorn main:app --host 0.0.0.0 --port 8000
```

### ML opcional (feeds SAR con YOLO)

Solo necesario si usas detección avanzada en feeds RTSP:

```bash
pip install torch torchvision
pip install -r requirements.txt
```

## Datos incluidos en el repositorio

| Archivo / carpeta | Contenido |
|-------------------|-----------|
| `ojo_de_dios.db` | ~40k desaparecidos sincronizados |
| `reference_photos/` | ~4.800 fotos locales de personas |
| `building_photos/` | ~200 fotos de edificios dañados |
| `config/` | Cámaras, emergencias por zona |

Al arrancar, el sistema sigue actualizando en vivo desde las APIs oficiales (con fallback a BD local si la API cae).

## Reportar persona o edificio

En el visor (**http://127.0.0.1:8000/**):

- Botón **+ Reportar persona** (pestaña Desaparecidos o botón flotante 👤)
- Botón **+ Reportar edificio** (pestaña Edificios o botón flotante 🏢)

**Foto obligatoria** (JPG/PNG/WebP, máx. 10 MB) + datos de contacto del reportante.

API:

```bash
# Persona
curl -X POST http://localhost:8000/api/reports/persona \
  -F "full_name=Nombre Apellido" \
  -F "last_known_location=Caracas, zona…" \
  -F "reporter_contact=0412-0000000" \
  -F "photo=@foto.jpg"

# Edificio
curl -X POST http://localhost:8000/api/reports/edificio \
  -F "name=Edificio X" \
  -F "address=Dirección" \
  -F "city=Caracas" \
  -F "damage_level=severo" \
  -F "reporter_contact=0412-0000000" \
  -F "photo=@foto.jpg"
```

## Cámaras en vivo

Editar `config/camaras_venezuela.json`:

```json
{
  "http_mjpeg": "http://IP-CAMARA/mjpg/video.mjpg",
  "video_en_vivo": ["rtsp://usuario:clave@IP:554/Streaming/Channels/101"]
}
```

Recargar:

```bash
curl -X POST http://localhost:8000/api/cameras/reload
```

## API principal

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/api/stats/dashboard` | Stats instantáneas (personas, fotos, emergencias) |
| GET | `/api/stats/live` | Stats en vivo (desaparecidos + edificios) |
| GET | `/missing?q=` | Buscar desaparecidos |
| POST | `/api/reports/persona` | Reportar persona (multipart + foto) |
| POST | `/api/reports/edificio` | Reportar edificio (multipart + foto) |
| GET | `/api/terremoto/buildings` | Edificios dañados |
| GET | `/api/emergencias` | Números de emergencia por zona |
| GET | `/api/cameras` | Red de cámaras SAR |
| GET | `/api/cameras/{id}/live.mjpg` | Stream MJPEG |
| GET | `/health` | Estado del sistema |

Documentación interactiva: **http://127.0.0.1:8000/docs**

## Estructura

| Ruta | Descripción |
|------|-------------|
| `main.py` | API FastAPI y workers |
| `static/visor.html` | Visor principal |
| `reports.py` | Reportes comunitarios con foto |
| `stats_dashboard.py` | Dashboard en tiempo real |
| `config/camaras_venezuela.json` | Cámaras SAR |
| `config/emergencias_venezuela.json` | 171 / \*1 / 112 / 911 |
| `data_ingestor.py` | Ingesta de desaparecidos |
| `terremoto_*.py` | Edificios y fotos |
| `camera_service.py` | Video en vivo |

## Solución de problemas (GitHub / clone / arranque)

### El `git clone` tarda mucho o falla

El repositorio pesa **~450 MB** (miles de fotos + base de datos). Es normal que tarde **5–15 minutos**.

```bash
# Clone superficial (más rápido, suficiente para usar el sistema)
git clone --depth 1 https://github.com/jomstudiovzla/VENEZUELATEBUSCA.git
cd VENEZUELATEBUSCA
./instalar.sh
```

Si falla por timeout, reintenta o usa una red más estable.

### `pip install` falla o tarda horas

Usa las dependencias mínimas (sin PyTorch):

```bash
pip install -r requirements-core.txt
```

PyTorch (`torch`) solo hace falta para ML avanzado en feeds SAR.

### Puerto 8000 ocupado

```bash
lsof -ti:8000 | xargs kill -9
./arrancar.sh
```

### La página carga pero sin datos

1. Verifica que exista `ojo_de_dios.db` en la carpeta del proyecto.
2. Revisa: `curl http://127.0.0.1:8000/health`
3. Si la API externa de desaparecidos está caída (502), el sistema usa la BD local automáticamente.

### GitHub muestra "Uh oh! There was an error while loading"

Es un fallo temporal de la interfaz de GitHub al listar repos grandes. El código y el README están bien — usa **Code → Download ZIP** o `git clone` desde terminal.

## Licencia

Proyecto humanitario — uso responsable de datos personales conforme a las fuentes públicas citadas.