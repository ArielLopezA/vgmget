# vgmget ♬

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org)
[![Platform](https://img.shields.io/badge/platform-macOS-lightgrey.svg)](https://www.apple.com/macos)


> Designed for macOS. Some features (`aac_at`, `pbpaste`) are not available on other platforms.

[Features](#features--características) · [Demo](#demo) · [Requirements](#requirements--requisitos) · [Installation](#installation--instalación) · [Usage](#usage--uso) · [VGMDB Tagging](#vgmdb-tagging--etiquetado-con-vgmdb) · [Configuration](#configuration--configuración)

Download, convert and tag video game soundtracks from KHInsider in a single command.

**vgmget** is a command-line tool for downloading video game soundtracks from [KHInsider](https://downloads.khinsider.com), converting them to M4A (AAC), embedding cover art, and tagging them with detailed metadata from [VGMDB](https://vgmdb.net) — all from a single command.

---

**vgmget** es una herramienta de línea de comandos para descargar soundtracks de videojuegos desde [KHInsider](https://downloads.khinsider.com), convertirlos a M4A (AAC), incrustar la portada del álbum y etiquetarlos con metadatos detallados desde [VGMDB](https://vgmdb.net) — todo desde un solo comando.

## Why?

Existing downloaders only download.

**vgmget** downloads, converts, embeds artwork and writes accurate VGMDB metadata in one workflow — while preserving album structure and never touching original files until every conversion succeeds.

---

Los descargadores existentes solo descargan.

**vgmget** descarga, convierte, incrusta la portada y escribe metadatos precisos de VGMDB en un solo flujo — preservando la estructura del álbum y sin tocar los archivos originales hasta que todas las conversiones sean exitosas.

## Features / Características

- Downloads albums from KHInsider in FLAC (preferred) or MP3
- Converts FLAC → M4A using the best available AAC codec (`aac_at` → `libfdk_aac` → `aac`)
- Converts local folders of FLAC files, including multi-disc albums with subfolders
- Downloads and optimizes cover art (max 500 KB, 1400×1400 px) using ffmpeg
- Injects cover art into M4A files via AtomicParsley (Apple `covr` atom)
- Tags files with VGMDB data: title, artist, composer, album, year, catalog number, genre
- Parallel scraping with per-thread HTTP sessions (no repeated TLS handshakes)
- Clean progress bar during download — no verbose aria2c output
- Safely converts FLAC files — originals are removed only after every conversion succeeds
- Quiet CLI following the Unix rule of silence

---

- Descarga álbumes desde KHInsider en FLAC (preferido) o MP3
- Convierte FLAC → M4A usando el mejor codec AAC disponible (`aac_at` → `libfdk_aac` → `aac`)
- Convierte carpetas locales de archivos FLAC, incluyendo álbumes multidisco con subcarpetas
- Descarga y optimiza la portada del álbum (máx 500 KB, 1400×1400 px) usando ffmpeg
- Inyecta la portada en los archivos M4A via AtomicParsley (átomo `covr` de Apple)
- Etiqueta los archivos con datos de VGMDB: título, artista, compositor, álbum, año, número de catálogo, género
- Scraping paralelo con sesiones HTTP por hilo (sin handshakes TLS repetidos)
- Barra de progreso limpia durante la descarga — sin output verbose de aria2c
- Convierte FLAC de forma segura — los originales se eliminan solo si todas las conversiones son exitosas
- CLI silencioso siguiendo la regla Unix del silencio

## Design Goals

- Prefer the best available AAC encoder, prioritizing Apple's AudioToolbox (`aac_at`)
- Keep dependencies minimal — no pip packages beyond `requests` and `beautifulsoup4`
- Never delete original FLAC files unless every conversion succeeds
- Preserve album structure, including multi-disc subfolder layouts
- Keep the interface quiet and readable — output only what matters

---

- Usar el mejor encoder AAC disponible, priorizando Apple AudioToolbox (`aac_at`)
- Dependencias mínimas — sin paquetes pip más allá de `requests` y `beautifulsoup4`
- Nunca eliminar los FLAC originales a menos que todas las conversiones sean exitosas
- Preservar la estructura del álbum, incluyendo subcarpetas de múltiples discos
- Interfaz silenciosa y legible — mostrar solo lo que importa

## Demo

```
♬ vgmget
─────────────────────────────────────────────
  → https://downloads.khinsider.com/game-soundtracks/album/chrono-cross

  Leyendo álbum...  8 pistas encontradas.
  ✓  Portada optimizada (503 KB → 178 KB)

  Extrayendo enlaces en paralelo (8 hilos)...
  ·  01. Chrono Cross -Scars of Time-.flac  [FLAC]
  ·  02. Arni (Home World).flac  [FLAC]
  ...

  ████████████████████  100%  8.8 MB/s   [8/8]

─────────────────────────────────────────────
  ✓  Descarga completa

  ¿Convertir a M4A ahora? [S/n]:

  Convirtiendo 8 archivo(s)  FLAC → M4A  [aac_at  256k  portada incluida]

  [1.01]  01. Chrono Cross -Scars of Time-.flac
          01. Chrono Cross -Scars of Time-.m4a ✓

─────────────────────────────────────────────
  ✓  8 archivo(s) convertidos

  ¿Etiquetar con VGMDB? [S/n]:
  ✓  Album     CHRONO CROSS Orchestral Arrangement
  ✓  Artist    Yasunori Mitsuda  /  Año  2019  /  Catalog  SQEX-10725

─────────────────────────────────────────────
  ✓  8 pista(s) etiquetadas
```

## Requirements / Requisitos

### External dependencies / Dependencias externas

```bash
brew install aria2 ffmpeg atomicparsley
```

### Python

```bash
pip install requests beautifulsoup4
```

## Installation / Instalación

```bash
# Clone and enter directory
git clone https://github.com/ArielLopezA/vgmget.git && cd vgmget

# Install scripts to ~/.local/bin
chmod +x vgmget
mkdir -p ~/.local/bin
cp vgmget vgmdb_tag.py ~/.local/bin/

# Add ~/.local/bin to PATH if needed
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc
```

## Usage / Uso

Just run `vgmget` and paste a KHInsider URL or a local folder path when prompted.

Ejecuta `vgmget` y pega una URL de KHInsider o una ruta de carpeta local cuando se solicite.

### Mode 1 — Download from KHInsider / Modo 1 — Descarga desde KHInsider

Paste a KHInsider album URL. The script will:

1. Find cover art and optimize it
2. Extract all track download links in parallel
3. Download files with aria2c (progress bar included)
4. Offer to convert FLAC → M4A
5. Offer to tag with VGMDB data

Pega una URL de álbum de KHInsider. El script:

1. Busca y optimiza la portada
2. Extrae todos los enlaces de descarga en paralelo
3. Descarga los archivos con aria2c (con barra de progreso)
4. Ofrece convertir FLAC → M4A
5. Ofrece etiquetar con datos de VGMDB

### Mode 2 — Convert local folder / Modo 2 — Convertir carpeta local

Paste a local folder path containing FLAC files. The script will:

1. Detect cover art in the folder (cover.jpg, folder.jpg, etc.)
2. Convert all FLAC files to M4A, including subfolders
3. Offer to tag with VGMDB data

Pega la ruta de una carpeta local con archivos FLAC. El script:

1. Detecta la portada en la carpeta (cover.jpg, folder.jpg, etc.)
2. Convierte todos los archivos FLAC a M4A, incluyendo subcarpetas
3. Ofrece etiquetar con datos de VGMDB

## VGMDB Tagging / Etiquetado con VGMDB

To tag files with VGMDB metadata, copy the full text of an album page from [vgmdb.net](https://vgmdb.net) to your clipboard before confirming the tagging step. The script reads the clipboard automatically.

Para etiquetar con datos de VGMDB, copia el texto completo de una página de álbum en [vgmdb.net](https://vgmdb.net) al portapapeles antes de confirmar el paso de etiquetado. El script lee el portapapeles automáticamente.

Tags written / Tags escritos:

| Tag | Source / Fuente |
|---|---|
| Title / Título | VGMDB tracklist |
| Artist / Artista | VGMDB composer per track |
| Composer / Compositor | VGMDB composer per track |
| Album | VGMDB album title |
| Year / Año | VGMDB Release Date |
| Comment / Comentario | VGMDB Catalog Number |
| Genre / Género | "Soundtrack" (fixed / fijo) |
| Cover / Portada | KHInsider or VGMDB URL |

## Configuration / Configuración

Edit the constants at the top of `vgmget` to change default behavior:

Edita las constantes al inicio de `vgmget` para cambiar el comportamiento por defecto:

```python
CARPETA_DESTINO = os.path.expanduser("~/Downloads/")  # Download destination
MAX_WORKERS     = 8      # Parallel scraping threads
RETRY_DELAY     = 2      # Seconds between retries
AAC_BITRATE     = "256k" # Conversion bitrate
```

## AAC Codec Priority / Prioridad de Codec AAC

The script automatically detects and uses the best available AAC encoder:

El script detecta y usa automáticamente el mejor encoder AAC disponible:

1. `aac_at` — Apple AudioToolbox (macOS native, best for Apple devices)
2. `libfdk_aac` — Fraunhofer FDK (technically superior in objective metrics)
3. `aac` — ffmpeg native (universal fallback)

## License / Licencia

MIT
