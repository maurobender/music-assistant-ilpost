# Il Post para Music Assistant

Provider de [Music Assistant](https://music-assistant.io) (MA) para escuchar los podcasts de
[Il Post](https://www.ilpost.it/podcasts/) con tu cuenta.

- **Biblioteca:** los ~80 podcasts de Il Post, con todos sus episodios.
- **Reproducción:** cada episodio es un MP3 directo (no necesita token), con avance y retroceso.
- **Progreso sincronizado:** MA lee de Il Post hasta dónde escuchaste cada episodio y le
  manda la posición mientras escuchás, así que podés seguir en la app o en ilpost.it.
- **Búsqueda** de podcasts por título, autor o descripción.
- **Recomendaciones:** los bloques de la home de podcasts de Il Post ("Nuovi episodi",
  "In evidenza", "Archivio", ...).

Usa una API no oficial (la misma que usan la web y las apps de Il Post), así que puede dejar
de funcionar si Il Post la cambia.

## Estructura

```
ilpost/           el provider (se copia o monta en music_assistant/providers/ilpost)
  __init__.py     provider: biblioteca, episodios, stream, búsqueda, home, progreso
  api_client.py   cliente HTTP de la API de Il Post (login, reintentos, paginado)
  setup_flow.py   formulario de alta (email y contraseña)
  manifest.json, strings.json, icon.svg, icon_monochrome.svg
scripts/
  probe_api.py      prueba la API: login, podcasts, episodios, URL de audio
  probe_state.py    prueba la API de progreso (solo lectura salvo con --write)
  test_provider.py  corre el provider contra la API real, sin levantar MA
```

## Instalación en MA con Docker

El provider usa la API de setup flows de MA (`setup_flow.py`), que está en el `main` actual
del servidor. Necesitás una imagen de MA de esa época o más nueva: con una versión vieja el
provider no aparece o falla al cargarse.

1. Copiá la carpeta `ilpost/` al host donde corre el contenedor, por ejemplo a
   `/opt/music-assistant/custom_providers/ilpost`.

2. Averiguá dónde están los providers dentro del contenedor (cambiá `music-assistant` por el
   nombre de tu contenedor):

   ```bash
   docker exec music-assistant python3 -c "import music_assistant.providers as p; print(p.__path__[0])"
   ```

   Va a imprimir algo como `/app/venv/lib/python3.14/site-packages/music_assistant/providers`.

3. Montá la carpeta en ese directorio como `ilpost`. Con docker compose:

   ```yaml
   services:
     music-assistant:
       image: ghcr.io/music-assistant/server:latest
       # ... lo que ya tengas ...
       volumes:
         - ./data:/data
         - /opt/music-assistant/custom_providers/ilpost:/app/venv/lib/python3.14/site-packages/music_assistant/providers/ilpost:ro
   ```

   Con `docker run`, el equivalente es
   `-v /opt/music-assistant/custom_providers/ilpost:<ruta del paso 2>/ilpost:ro`.

4. Recreá el contenedor para que tome el volumen:

   ```bash
   docker compose up -d --force-recreate music-assistant
   ```

5. En MA: **Settings → Providers → Add provider → Il Post**, cargá tu email y contraseña de
   Il Post. Después de la primera sincronización de la biblioteca aparecen los podcasts.

Al actualizar la imagen de MA, el montaje se mantiene. Si la ruta del paso 2 cambia (por
ejemplo con una versión nueva de Python), hay que ajustarla.

Para ver errores del provider:

```bash
docker logs -f music-assistant 2>&1 | grep -i ilpost
```

Para más detalle, poné el log level del provider en **Debug** desde su configuración en MA.

## Cómo probarlo localmente

Credenciales: copiá `.env.example` a `.env` y completá `ILPOST_EMAIL` e `ILPOST_PASSWORD`.
`.env` está en `.gitignore`.

### Contra la API, sin MA (solo necesita [uv](https://docs.astral.sh/uv/))

```bash
uv run scripts/probe_api.py
```

```bash
uv run scripts/probe_state.py
```

Las respuestas crudas quedan en `scripts/output/` (ignorado por git).
`probe_state.py --write EPISODE_ID SEGUNDOS` guarda una posición en tu cuenta.

### El provider, con el código de MA

1. Cloná el servidor de MA y creá su entorno. `aiolibdatachannel` no compila en macOS y el
   provider no la usa, así que se instala todo menos esa dependencia:

   ```bash
   git clone https://github.com/music-assistant/server.git ../music-assistant-server
   ```

   ```bash
   cd ../music-assistant-server && uv venv -p 3.14 .venv && python3 -c 'import tomllib; print("\n".join(d for d in tomllib.load(open("pyproject.toml","rb"))["project"]["dependencies"] if not d.startswith("aiolibdatachannel")))' > /tmp/ma-reqs.txt && uv pip install -p .venv -r /tmp/ma-reqs.txt mypy==2.3.0 ruff==0.16.5 && uv pip install -p .venv --no-deps -e .
   ```

2. Enlazá el provider dentro de MA:

   ```bash
   ln -sfn "$PWD/../music-assistant-ilpost/ilpost" music_assistant/providers/ilpost
   ```

3. Corré el provider contra la API real (no escribe nada en tu cuenta: las escrituras de
   progreso se capturan):

   ```bash
   cd ../music-assistant-ilpost && ../music-assistant-server/.venv/bin/python scripts/test_provider.py
   ```

4. Lint con la configuración de MA (desde `music-assistant-server`):

   ```bash
   .venv/bin/ruff check music_assistant/providers/ilpost/ && .venv/bin/ruff format --check music_assistant/providers/ilpost/
   ```

   ```bash
   .venv/bin/mypy music_assistant/providers/ilpost/ 2>&1 | grep -E "ilpost|^Success"
   ```

   mypy revisa todo el paquete de MA; los errores de otros providers (por dependencias no
   instaladas) se pueden ignorar. No tiene que aparecer ninguna línea con `ilpost`.

## Detalles de la API

- Login: `POST https://api-prod.ilpost.it/user/v1/auth/login` (form `username`, `password`).
  Todas las llamadas llevan `Apikey: testapikey` y `Token: <token>`. El token se renueva cada
  ~2 h y ante un 401.
- Podcasts y episodios: `https://api-prod.ilpost.it/podcast/v1/podcast/...`, paginados con
  `pg` y `hits`. Home: `/podcast/v1/bff/hp`.
- Progreso: `POST /user/v1/state/episode` con `{"items": [ids]}` lee posiciones;
  `POST /user/v1/state/episode/{id}` con `{"data": {"position": segundos}}` las guarda. La
  lectura tarda unos segundos en reflejar una escritura. No hay marca de "escuchado": el
  provider considera escuchado un episodio al 95 % y, al terminarlo, guarda la posición
  final.
- El audio (`episode_raw_url`) es un MP3 público en `www.ilpost.it/wp-content/uploads/...`.
