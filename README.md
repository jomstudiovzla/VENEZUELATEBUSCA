# Venezuela te Busca

Sistema humanitario SAR/DVI para búsqueda de personas desaparecidas y monitoreo de edificios dañados tras el terremoto en Venezuela (2026).

## Fuentes de datos

- **Desaparecidos:** [desaparecidosterremotovenezuela.com](https://desaparecidosterremotovenezuela.com/)
- **Edificios dañados:** [terremotovenezuela.com](https://terremotovenezuela.com/)

## Funciones

- Visor unificado con desaparecidos, edificios, mapa y cámaras SAR 24h
- Sincronización en vivo vía API y WebSocket
- Búsqueda por nombre o cédula (~37k registros)
- Descarga automática de fotos de desaparecidos y edificios
- Red de cámaras con video en vivo real (RTSP/MJPEG) y detección de personas
- Perfil forense por persona

## Requisitos

- Python 3.11+
- macOS / Linux (recomendado)

## Instalación

```bash
git clone https://github.com/jomstudiovzla/VENEZUELATEBUSCA.git
cd VENEZUELATEBUSCA

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
```

## Arranque

```bash
source .venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8000
```

Abrir en el navegador: **http://127.0.0.1:8000/**

La primera ejecución sincroniza desaparecidos y edificios; las fotos se descargan en segundo plano.

## Cámaras en vivo

Editar `config/camaras_venezuela.json` con las URLs reales de tus cámaras:

```json
{
  "http_mjpeg": "http://IP-CAMARA/mjpg/video.mjpg",
  "video_en_vivo": ["rtsp://usuario:clave@IP:554/Streaming/Channels/101"]
}
```

Recargar la red de cámaras:

```bash
curl -X POST http://localhost:8000/api/cameras/reload
```

## Estructura

| Ruta | Descripción |
|------|-------------|
| `main.py` | API FastAPI y workers |
| `static/visor.html` | Visor principal |
| `config/camaras_venezuela.json` | Configuración de cámaras SAR |
| `data_ingestor.py` | Ingesta de desaparecidos |
| `terremoto_*.py` | Edificios dañados y fotos |
| `camera_service.py` | Video en vivo y detecciones |

## API principal

- `GET /api/stats/live` — estadísticas en vivo
- `GET /missing?q=` — desaparecidos (búsqueda)
- `GET /api/terremoto/buildings` — edificios
- `GET /api/cameras` — red de cámaras
- `GET /api/cameras/{id}/live.mjpg` — stream MJPEG

## Licencia

Proyecto humanitario — uso responsable de datos personales conforme a las fuentes públicas citadas.