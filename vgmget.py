#!/usr/bin/env -S python3 -W ignore
"""
vgmget — Descarga álbumes de KHInsider y/o convierte FLAC a M4A.
Opcionalmente etiqueta los archivos con datos de VGMDB.

Modos de uso:
  · URL de KHInsider  → descarga, convierte a M4A, inyecta portada y etiqueta.
  · Ruta de carpeta   → convierte FLAC a M4A, inyecta portada y etiqueta.

Uso:
    vgmget

Requisitos:
    pip install requests beautifulsoup4
    brew install aria2 ffmpeg atomicparsley
"""

import sys
import os
import re
import time
import shutil
import threading
import warnings
import requests
import subprocess
from pathlib import Path
from bs4 import BeautifulSoup
from urllib.parse import urljoin, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed

# Suprimir warning de urllib3 sobre LibreSSL en macOS (inofensivo)
try:
    from urllib3.exceptions import NotOpenSSLWarning
    warnings.filterwarnings("ignore", category=NotOpenSSLWarning)
except ImportError:
    pass

# ─────────────────────────────────────────────
# CONFIGURACIÓN — edita estas líneas si lo deseas:
CARPETA_DESTINO = os.path.expanduser("~/Downloads/")
MAX_WORKERS     = 8      # Hilos paralelos para scraping de pistas
RETRY_DELAY     = 2      # Segundos entre reintentos (protección ante bloqueos)
AAC_BITRATE     = "256k" # Bitrate para la conversión FLAC → M4A
# ─────────────────────────────────────────────

# Jerarquía de codecs AAC por calidad en dispositivos Apple.
# aac_at (AudioToolbox nativo de Apple) siempre tiene prioridad:
# está optimizado para el hardware y el stack de audio de Apple,
# y a 256k su salida es la más compatible y mejor calibrada para
# Mac, iPhone, iPad y AirPods. libfdk_aac es técnicamente más
# preciso en métricas objetivas pero su ventaja a 256k es
# inaudible; aac es el fallback universal de ffmpeg.
CODECS_AAC_PREFERIDOS = ["aac_at", "libfdk_aac", "aac"]

# Nombres de archivo candidatos para portada en carpetas locales,
# en orden de prioridad.
PORTADA_CANDIDATOS = [
    "cover.jpg", "cover.png", "cover.jpeg",
    "folder.jpg", "folder.png",
    "artwork.jpg", "artwork.png",
    "front.jpg", "front.png",
]

BASE = "https://downloads.khinsider.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

LINEA = "─" * 45

# Sesión HTTP por hilo — reutiliza conexiones TLS evitando handshakes repetidos
_local = threading.local()

def _get_session() -> requests.Session:
    """
    Retorna una sesión requests ligada al hilo actual.
    Cada hilo crea su propia sesión la primera vez y la reutiliza en adelante,
    eliminando el overhead de negociación TLS por cada petición.
    """
    if not hasattr(_local, "session"):
        _local.session = requests.Session()
        _local.session.headers.update(HEADERS)
    return _local.session


# ══════════════════════════════════════════════════════════════
# EXCEPCIÓN PERSONALIZADA
# ══════════════════════════════════════════════════════════════

class VgmgetError(Exception):
    """Error controlado de vgmget. main() lo captura y muestra el mensaje."""
    pass


# ══════════════════════════════════════════════════════════════
# VERIFICACIÓN DE DEPENDENCIAS
# ══════════════════════════════════════════════════════════════

def verificar_dependencias():
    """
    Verifica que las herramientas externas requeridas estén disponibles
    en el PATH antes de comenzar cualquier operación.
    Si falta alguna, muestra un informe claro y lanza VgmgetError.
    """
    dependencias = {
        "ffmpeg":        "brew install ffmpeg",
        "aria2c":        "brew install aria2",
        "atomicparsley": "brew install atomicparsley",
    }

    faltantes = {
        cmd: instruccion
        for cmd, instruccion in dependencias.items()
        if not shutil.which(cmd)
    }

    if faltantes:
        print(f"\n  ✘  Dependencias faltantes:")
        for cmd, instruccion in faltantes.items():
            print(f"     {cmd:<16} →  {instruccion}")
        raise VgmgetError("Dependencias faltantes. Instálalas y vuelve a correr vgmget.")


def cabecera():
    print("\n♬ vgmget")
    print(LINEA)


# ══════════════════════════════════════════════════════════════
# UTILIDADES COMPARTIDAS
# ══════════════════════════════════════════════════════════════

def detectar_mejor_codec_aac():
    """
    Prueba los codecs de CODECS_AAC_PREFERIDOS en orden y retorna el primero
    que ffmpeg acepte. La jerarquía refleja la calidad óptima para
    dispositivos Apple: aac_at > libfdk_aac > aac.
    Lanza VgmgetError si ffmpeg no está instalado.
    """
    for codec in CODECS_AAC_PREFERIDOS:
        resultado = subprocess.run(
            [
                "ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-t", "0.1", "-c:a", codec, "-f", "null", "-",
            ],
            capture_output=True,
        )
        if resultado.returncode == 0:
            return codec

    raise VgmgetError("No se encontró ningún codec AAC compatible. Reinstala con: brew reinstall ffmpeg")


def recopilar_audios(carpeta_raiz, extension):
    """
    Recorre recursivamente carpeta_raiz con os.walk() y retorna una lista
    de rutas absolutas a todos los archivos con la extensión indicada,
    ordenados por subcarpeta y nombre para respetar el orden de discos y pistas.
    """
    audios = []
    for raiz, _, archivos in os.walk(carpeta_raiz):
        for archivo in sorted(archivos):
            if archivo.lower().endswith(extension):
                audios.append(os.path.join(raiz, archivo))
    return audios


def inyectar_artwork(ruta_m4a, ruta_portada):
    """
    Inyecta una imagen como portada (átomo covr) en un archivo .m4a
    usando AtomicParsley. El archivo se sobreescribe en su lugar.
    Retorna True si tuvo éxito, False si falló (sin detener el proceso).
    """
    try:
        resultado = subprocess.run(
            [
                "atomicparsley", ruta_m4a,
                "--artwork", ruta_portada,
                "--overWrite",
            ],
            capture_output=True,
            text=True,
        )
        return resultado.returncode == 0
    except FileNotFoundError:
        return False


def buscar_portada_local(carpeta):
    """
    Busca una imagen de portada en la carpeta según PORTADA_CANDIDATOS.
    Si no encuentra ningún candidato conocido, busca cualquier imagen
    jpg/png en la raíz de la carpeta como último recurso.
    Retorna la ruta absoluta si encuentra algo, None si no.
    """
    for nombre in PORTADA_CANDIDATOS:
        ruta = os.path.join(carpeta, nombre)
        if os.path.isfile(ruta):
            return ruta

    # Fallback: cualquier imagen jpg/png en la raíz (excluyendo temporales)
    try:
        for archivo in sorted(os.listdir(carpeta)):
            if archivo.lower().endswith((".jpg", ".jpeg", ".png")) and not archivo.startswith("_"):
                return os.path.join(carpeta, archivo)
    except OSError:
        pass

    return None


def optimizar_portada(ruta_imagen, limite_kb=500):
    """
    Verifica el peso de la imagen y la optimiza solo si supera el límite.
    Si el archivo pesa menos de limite_kb, se usa directamente sin modificar.
    Si supera el límite, ffmpeg la redimensiona a 1400x1400 máximo y la
    recomprime como JPG al 85% de calidad, sobreescribiendo el archivo original.
    """
    try:
        peso_kb = os.path.getsize(ruta_imagen) / 1024
    except OSError:
        return False

    if peso_kb <= limite_kb:
        return True

    print(f"  ⚠  Portada pesada ({peso_kb:.0f} KB) — optimizando...")

    ruta_temp = ruta_imagen + ".tmp.jpg"

    cmd = [
        "ffmpeg",
        "-i", ruta_imagen,
        "-vf", "scale='min(1400,iw)':'min(1400,ih)':force_original_aspect_ratio=decrease",
        "-q:v", "4",
        "-y",
        "-loglevel", "error",
        ruta_temp,
    ]

    resultado = subprocess.run(cmd, capture_output=True, text=True)

    if resultado.returncode != 0:
        print("      ⚠  No se pudo optimizar la portada — se usará el original.")
        if os.path.exists(ruta_temp):
            os.remove(ruta_temp)
        return True

    os.replace(ruta_temp, ruta_imagen)
    peso_final_kb = os.path.getsize(ruta_imagen) / 1024
    print(f"  ✓  Portada optimizada ({peso_kb:.0f} KB → {peso_final_kb:.0f} KB)")
    return True


def convertir_flac_a_m4a(dest_dir, codec_audio, ruta_portada=None):
    """
    Convierte todos los archivos .flac de dest_dir (y subcarpetas) a .m4a.
    Los FLAC originales se acumulan y eliminan TODOS al final, solo si
    todas las conversiones fueron exitosas. Si alguna falla, lanza VgmgetError
    y ningún FLAC es eliminado.
    """
    flacs = recopilar_audios(dest_dir, ".flac")

    if not flacs:
        print("\n  Sin archivos FLAC en la carpeta.")
        return

    total = len(flacs)
    artwork_info = "  portada incluida" if ruta_portada else "  sin portada"
    print(f"\n  Convirtiendo {total} archivo(s)  FLAC → M4A  [{codec_audio}  {AAC_BITRATE}{artwork_info}]\n")

    flacs_convertidos = []  # Acumula los FLAC convertidos exitosamente

    for i, ruta_flac in enumerate(flacs, 1):
        nombre_flac  = os.path.basename(ruta_flac)
        subcarpeta   = os.path.relpath(os.path.dirname(ruta_flac), dest_dir)
        base_name, _ = os.path.splitext(nombre_flac)
        nombre_m4a   = base_name + ".m4a"
        ruta_m4a     = os.path.join(os.path.dirname(ruta_flac), nombre_m4a)

        etiqueta = f"{subcarpeta}/{nombre_flac}" if subcarpeta != "." else nombre_flac

        # Detectar disco/pista para el label usando la misma función que vgmdb_tag
        try:
            import vgmdb_tag as _vt
            disco, pista = _vt.detectar_disco_pista(base_name)
            label_pista = f"{disco}.{pista:02d}" if (disco and pista) else str(i)
        except Exception:
            label_pista = str(i)
        print(f"  [{label_pista}]  {etiqueta}")

        cmd = [
            "ffmpeg",
            "-i", ruta_flac,
            "-vn",
            "-c:a", codec_audio,
            "-b:a", AAC_BITRATE,
            "-map_metadata", "0",
            ruta_m4a,
            "-y",
            "-loglevel", "error",
        ]

        resultado = subprocess.run(cmd, capture_output=True, text=True)

        if resultado.returncode != 0:
            detalle = resultado.stderr.strip() if resultado.stderr else ""
            raise VgmgetError(
                f"Falló la conversión de: {etiqueta}\n"
                f"      {detalle}\n"
                f"      Ningún FLAC fue eliminado."
            )

        flacs_convertidos.append(ruta_flac)

        # Inyección de artwork con AtomicParsley
        if ruta_portada:
            ok = inyectar_artwork(ruta_m4a, ruta_portada)
            if not ok:
                print(f"         {nombre_m4a}  ⚠ sin portada")
            else:
                print(f"         {nombre_m4a} ✓")
        else:
            print(f"         {nombre_m4a} ✓")

    # Todas las conversiones exitosas — eliminar FLACs originales
    for ruta_flac in flacs_convertidos:
        try:
            os.remove(ruta_flac)
        except OSError:
            pass

    print(f"\n{LINEA}")
    print(f"  ✓  {total} archivo(s) convertidos")
    print(f"     {dest_dir}")


# ══════════════════════════════════════════════════════════════
# MODO DESCARGA — KHInsider
# ══════════════════════════════════════════════════════════════

def sanitize_folder(name):
    return re.sub(r'[\\/*?:"<>|]', "_", name)


def get_soup(url, retries=3, delay=RETRY_DELAY):
    """Descarga y parsea una página HTML con reintentos y pausa entre intentos."""
    for attempt in range(retries):
        try:
            r = _get_session().get(url, timeout=15)
            r.raise_for_status()
            return BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            if attempt < retries - 1:
                print(f"  ⚠  Reintento {attempt + 1}/{retries}: {e}")
                time.sleep(delay)
            else:
                print(f"  ✘  Error definitivo en {url}: {e}")
    return None


def obtener_portada_khinsider(album_url, dest_dir):
    """
    Extrae la URL de la portada del álbum desde la página de KHInsider.
    Usa regex sobre el HTML crudo filtrando las imágenes de vgmtreasurechest.
    Retorna la ruta del archivo temporal descargado, o None si no la encuentra.
    """
    soup = get_soup(album_url)
    if not soup:
        return None

    raw = str(soup)
    patron = r'https://[^\s\'"<>]+\.(?:jpg|jpeg|png)'
    matches = re.findall(patron, raw, re.IGNORECASE)
    candidatos = [m for m in matches if "vgmtreasurechest.com" in m]

    if not candidatos:
        return None

    img_url = candidatos[0]

    ext = ".jpg"
    for candidate_ext in [".png", ".jpeg", ".jpg"]:
        if candidate_ext in img_url.lower():
            ext = candidate_ext
            break

    try:
        r = _get_session().get(img_url, timeout=15)
        r.raise_for_status()
        ruta_temp = os.path.join(dest_dir, f"_cover_temp{ext}")
        with open(ruta_temp, "wb") as f:
            f.write(r.content)
        return ruta_temp
    except Exception:
        return None


def get_track_page_links(album_url):
    """Extrae los paths de cada pista desde la tabla principal del álbum."""
    print("\n  Leyendo álbum...")
    soup = get_soup(album_url)
    if not soup:
        raise VgmgetError("No se pudo cargar la página del álbum.")

    table = soup.find("table", id="songlist")
    if not table:
        raise VgmgetError("No se encontró la tabla de canciones. ¿URL correcta?")

    vistos = set()
    links = []
    for a in table.find_all("a", href=True):
        href = a["href"]
        if "/game-soundtracks/album/" in href and href not in vistos:
            vistos.add(href)
            links.append(href)

    print(f"  {len(links)} pistas encontradas.")
    return links


def get_best_audio_url(track_path):
    """
    Visita la página de una pista y devuelve la mejor URL de audio disponible.
    Orden de preferencia: FLAC > MP3.
    Busca en: tag <audio>, enlaces <a>, y regex sobre el HTML completo.
    Retorna un dict {url, filename, format} o None si no encuentra nada.
    """
    url = urljoin(BASE, track_path)
    soup = get_soup(url)
    if not soup:
        return None

    found = {"flac": None, "mp3": None}

    audio_tag = soup.find("audio")
    if audio_tag:
        src = audio_tag.get("src", "")
        if src.lower().endswith(".flac"):
            found["flac"] = src
        elif src.lower().endswith(".mp3"):
            found["mp3"] = src

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().endswith(".flac") and not found["flac"]:
            found["flac"] = href
        elif href.lower().endswith(".mp3") and not found["mp3"]:
            found["mp3"] = href

    raw = str(soup)
    if not found["flac"]:
        m = re.search(r'https://[^"\']+\.flac', raw)
        if m:
            found["flac"] = m.group(0)
    if not found["mp3"]:
        m = re.search(r'https://[^"\']+\.mp3', raw)
        if m:
            found["mp3"] = m.group(0)

    chosen_url = found["flac"] or found["mp3"]
    if not chosen_url:
        return None

    fmt = "flac" if chosen_url == found["flac"] else "mp3"
    filename = unquote(chosen_url.split("/")[-1])

    return {"url": chosen_url, "filename": filename, "format": fmt}


def descargar_con_progreso(aria_cmd, total_archivos, input_file_path):
    """
    Ejecuta aria2c capturando su output en tiempo real y dibuja una barra
    de progreso con bloque sólido, porcentaje, velocidad y archivos completados.
    Maneja Ctrl+C limpiamente. Limpia el input_file_path al terminar.
    Retorna True si aria2c terminó sin errores, False si hubo alguno.
    """
    ANCHO_BARRA = 20

    proceso = subprocess.Popen(
        aria_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )

    pct_actual  = 0
    vel_actual  = "0 KB/s"
    completados = 0
    _cancelado  = False

    barra_vacia = "░" * ANCHO_BARRA
    print(f"\r  {barra_vacia}    0%  {'0 KB/s':<12}  [0/{total_archivos}]  ", end="", flush=True)

    try:
        while True:
            linea = proceso.stdout.readline()
            if not linea and proceso.poll() is not None:
                break

            linea = linea.strip()
            if not linea:
                continue

            if "Download complete:" in linea:
                completados += 1
                pct_actual = int(completados / total_archivos * 100)

            m = re.search(r"\[DL:([^\]]+)\]", linea)
            if m:
                vel_raw    = m.group(1).replace("MiB", " MB").replace("KiB", " KB").replace("GiB", " GB")
                vel_actual = vel_raw + "/s" if not vel_raw.endswith("/s") else vel_raw

            llenos = int(ANCHO_BARRA * pct_actual // 100)
            barra  = "█" * llenos + "░" * (ANCHO_BARRA - llenos)
            print(
                f"\r  {barra}  {pct_actual:3d}%  {vel_actual:<12}  [{completados}/{total_archivos}]  ",
                end="", flush=True
            )

        proceso.wait()

    except KeyboardInterrupt:
        _cancelado = True
        proceso.terminate()
        print("\n\n  Descarga cancelada.")
        if os.path.exists(input_file_path):
            os.remove(input_file_path)
        raise KeyboardInterrupt

    finally:
        if not _cancelado:
            barra = "█" * ANCHO_BARRA
            print(
                f"\r  {barra}  100%  {vel_actual:<12}  [{total_archivos}/{total_archivos}]  "
            )
        if os.path.exists(input_file_path):
            os.remove(input_file_path)

    return proceso.returncode == 0


def modo_descarga(album_url):
    """Descarga un álbum de KHInsider y ofrece convertir los FLAC a M4A."""
    album_slug = sanitize_folder(album_url.split("/")[-1])
    dest_dir   = os.path.join(CARPETA_DESTINO, album_slug)
    os.makedirs(dest_dir, exist_ok=True)

    track_paths = get_track_page_links(album_url)

    print("  Buscando portada del álbum...")
    ruta_portada = obtener_portada_khinsider(album_url, dest_dir)
    if ruta_portada:
        print("  ✓  Portada encontrada")
        optimizar_portada(ruta_portada)
    else:
        print("  ⚠  No se encontró portada")

    audio_tracks    = []
    formatos_usados = {"flac": 0, "mp3": 0}
    fallidos        = []

    print(f"\n  Extrayendo enlaces en paralelo ({MAX_WORKERS} hilos)...")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_info = {
            executor.submit(get_best_audio_url, path): (idx, path)
            for idx, path in enumerate(track_paths)
        }

        resultados = {}
        for future in as_completed(future_to_info):
            idx, path = future_to_info[future]
            try:
                resultados[idx] = future.result()
            except Exception as e:
                print(f"  ⚠  Error en {path}: {e}")
                resultados[idx] = None

    for idx in sorted(resultados.keys()):
        track_info = resultados[idx]
        if track_info:
            audio_tracks.append(track_info)
            formatos_usados[track_info["format"]] += 1
            print(f"  ·  {track_info['filename']}  [{track_info['format'].upper()}]")
        else:
            fallidos.append(track_paths[idx])

    print(f"\n  {len(audio_tracks)} enlaces  —  FLAC: {formatos_usados['flac']}  MP3: {formatos_usados['mp3']}")

    if fallidos:
        print(f"\n  ⚠  {len(fallidos)} pista(s) sin enlace:")
        for p in fallidos:
            print(f"     {p}")

    if not audio_tracks:
        raise VgmgetError("No se pudieron extraer enlaces de descarga.")

    input_file_path = os.path.join(dest_dir, "aria_input.txt")
    with open(input_file_path, "w") as f:
        for track in audio_tracks:
            f.write(f"{track['url']}\n")
            f.write(f"  out={track['filename']}\n")

    print("\n  Descargando...")
    print(f"  {os.path.abspath(dest_dir)}\n")

    aria_cmd = [
        "aria2c",
        "-i", input_file_path,
        "-d", dest_dir,
        "-j", "5",
        "-x", "8",
        "--auto-file-renaming=false",
        "--allow-overwrite=true",
        "--console-log-level=notice",
        "--summary-interval=1",
        "--quiet=false",
    ]

    descarga_ok = False
    try:
        ok = descargar_con_progreso(aria_cmd, len(audio_tracks), input_file_path)
        if not ok:
            raise VgmgetError("aria2c terminó con algunos errores.")
        print(f"\n{LINEA}")
        print("  ✓  Descarga completa")
        print(f"     {dest_dir}")
        descarga_ok = True
    except FileNotFoundError:
        raise VgmgetError("aria2c no está instalado. Instálalo con: brew install aria2")

    if descarga_ok and formatos_usados["flac"] > 0:
        print(f"\n{LINEA}")
        print(f"  {formatos_usados['flac']} archivo(s) FLAC descargados.")
        print("  Los FLAC se eliminarán al finalizar todas las conversiones.")

        try:
            respuesta = input("\n  ¿Convertir a M4A ahora? [S/n]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\n\n  Conversión cancelada.")
            respuesta = "n"

        if respuesta in ("s", "si", "sí", ""):
            convertir_flac_a_m4a(dest_dir, detectar_mejor_codec_aac(), ruta_portada)

            print(f"\n{LINEA}")
            try:
                resp_vgmdb = input("  ¿Etiquetar con VGMDB? [S/n]: ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                resp_vgmdb = "n"

            if resp_vgmdb in ("s", "si", "sí", ""):
                paso_etiquetado_vgmdb(dest_dir, ruta_portada)
        else:
            print("  Los FLAC quedan intactos en la carpeta.")

    if ruta_portada and os.path.exists(ruta_portada):
        os.remove(ruta_portada)


# ══════════════════════════════════════════════════════════════
# MODO CONVERSIÓN — carpeta local
# ══════════════════════════════════════════════════════════════

def modo_conversion(carpeta):
    """Convierte los FLAC de una carpeta local (y subcarpetas) a M4A."""
    codec_audio  = detectar_mejor_codec_aac()
    ruta_portada = buscar_portada_local(carpeta)
    if ruta_portada:
        optimizar_portada(ruta_portada)

    print(f"\n  Carpeta   {carpeta}")
    print(f"  Codec     {codec_audio}")
    print(f"  Bitrate   {AAC_BITRATE}")
    if ruta_portada:
        print(f"  Portada   {os.path.basename(ruta_portada)}")
    else:
        print("  Portada   no encontrada")
    print("  Los FLAC se eliminarán al finalizar todas las conversiones.")

    try:
        respuesta = input("\n  ¿Comenzar conversión? [S/n]: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print("\n\n  Operación cancelada.")
        return

    if respuesta in ("s", "si", "sí", ""):
        convertir_flac_a_m4a(carpeta, codec_audio, ruta_portada)

        print(f"\n{LINEA}")
        try:
            resp_vgmdb = input("  ¿Etiquetar con VGMDB? [S/n]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            resp_vgmdb = "n"

        if resp_vgmdb in ("s", "si", "sí", ""):
            paso_etiquetado_vgmdb(carpeta, ruta_portada)
    else:
        print("  Conversión cancelada. Los FLAC quedan intactos.")


# ══════════════════════════════════════════════════════════════
# ETIQUETADO VGMDB — paso opcional al terminar la conversión
# ══════════════════════════════════════════════════════════════

def paso_etiquetado_vgmdb(carpeta, ruta_portada_existente=None):
    """
    Paso opcional de etiquetado con datos de VGMDB.
    Importa vgmdb_tag en tiempo de ejecución para no romper vgmget
    si el módulo no está disponible.
    """
    try:
        import vgmdb_tag
    except ImportError:
        print("  ✘  Módulo vgmdb_tag no encontrado.")
        print("      Asegúrate de que vgmdb_tag.py esté en ~/.local/bin/")
        return

    # 1. Leer datos de VGMDB desde clipboard o archivo
    print("\n  Leyendo clipboard...")
    texto = vgmdb_tag.leer_clipboard().strip()

    if not texto or not vgmdb_tag.es_pagina_vgmdb(texto):
        print("  ⚠  El clipboard no contiene datos válidos de VGMDB.")
        print()
        try:
            ruta_txt = input("  Ruta al archivo .txt con los datos (Enter para cancelar): ").strip().strip("'\"")
        except (KeyboardInterrupt, EOFError):
            print("\n\n  Etiquetado cancelado.")
            return

        if not ruta_txt:
            print("  Etiquetado cancelado.")
            return

        ruta_txt = os.path.expanduser(ruta_txt.replace("\\ ", " "))
        if not os.path.isfile(ruta_txt):
            print("  ✘  Archivo no encontrado. Etiquetado cancelado.")
            return

        with open(ruta_txt, encoding="utf-8") as f:
            texto = f.read()

        if not vgmdb_tag.es_pagina_vgmdb(texto):
            print("  ✘  El archivo no contiene datos válidos de VGMDB. Etiquetado cancelado.")
            return

    # 2. Extraer datos y parsear composición
    datos     = vgmdb_tag.extraer_datos_vgmdb(texto)
    track_map = vgmdb_tag.parsear_composicion(datos["composicion_texto"])

    if datos["album"]:
        print(f"  ✓  Album     {datos['album']}")
    if datos["artist"]:
        print(f"  ✓  Artist    {datos['artist']}")
    if datos.get("anio"):
        print(f"  ✓  Año       {datos['anio']}")
    if datos.get("catalog"):
        print(f"  ✓  Catalog   {datos['catalog']}")
    if datos["tracklist"]:
        print(f"  ✓  Tracklist {len(datos['tracklist'])} pista(s)  —  {datos['disc_total']} disco(s)")

    # 3. Portada: URL de VGMDB opcional, fallback a la ya descargada
    portada_para_tags = None
    print()
    try:
        url_portada = input("  URL de portada de VGMDB (Enter para usar la existente): ").strip()
    except (KeyboardInterrupt, EOFError):
        url_portada = ""

    if url_portada:
        try:
            r = requests.get(url_portada, timeout=15)
            r.raise_for_status()
            ext = ".png" if url_portada.lower().endswith(".png") else ".jpg"
            ruta_vgmdb = os.path.join(carpeta, f"_cover_vgmdb{ext}")
            with open(ruta_vgmdb, "wb") as f:
                f.write(r.content)
            portada_para_tags = ruta_vgmdb
            print(f"  ✓  Portada VGMDB descargada ({len(r.content) // 1024} KB)")
        except Exception as e:
            print(f"  ⚠  No se pudo descargar la portada de VGMDB: {e}")
    elif ruta_portada_existente and os.path.isfile(ruta_portada_existente):
        portada_para_tags = ruta_portada_existente
        peso_kb = os.path.getsize(ruta_portada_existente) // 1024
        print(f"  ✓  Usando portada existente ({peso_kb} KB)")

    # 4. Tabla previa y escritura sin segunda confirmación
    archivos = vgmdb_tag.recopilar_m4a(Path(carpeta))
    if not archivos:
        print(f"\n  ✘  No se encontraron archivos .m4a en: {carpeta}")
        return

    print(f"\n{LINEA}")
    resultado = vgmdb_tag.etiquetar_carpeta(
        Path(carpeta), datos, track_map, portada_para_tags, skip_confirmation=True
    )
    if resultado is None:
        return
    n_ok, n_fallback, n_error = resultado

    # Limpiar portada VGMDB temporal si se descargó
    if url_portada and portada_para_tags and os.path.exists(portada_para_tags):
        os.remove(portada_para_tags)

    # 5. Resumen
    print(f"\n{LINEA}")
    if n_ok:
        print(f"  ✓  {n_ok} pista(s) con compositor específico")
    if n_fallback:
        print(f"  ~  {n_fallback} pista(s) con artist global")
    if n_error:
        print(f"  ✘  {n_error} error(es)")


# ══════════════════════════════════════════════════════════════
# ENTRADA INTERACTIVA Y DETECCIÓN DE MODO
# ══════════════════════════════════════════════════════════════

def pedir_entrada():
    """
    Solicita una URL de KHInsider o una ruta de carpeta.
    Detecta automáticamente qué modo activar según lo ingresado.
    Lanza VgmgetError si la entrada no es válida.
    """
    cabecera()
    print("  Pega una URL para descargar,")
    print("  o una ruta de carpeta para convertir FLAC a M4A.")
    print()
    try:
        entrada = input("  → ").strip().strip("'\"")
    except (KeyboardInterrupt, EOFError):
        print("\n\n  Operación cancelada.")
        return None, None

    if not entrada:
        raise VgmgetError("No se ingresó nada.")

    if entrada.startswith("http"):
        if "khinsider.com" not in entrada:
            raise VgmgetError("La URL no parece ser de KHInsider.")
        return "descarga", entrada.strip().rstrip("/")

    entrada_limpia = entrada.replace("\\ ", " ")
    ruta = os.path.expanduser(entrada_limpia)
    if not os.path.isdir(ruta):
        raise VgmgetError(f"La ruta no existe o no es una carpeta: {ruta}")
    return "conversion", ruta


def main():
    try:
        verificar_dependencias()
        modo, valor = pedir_entrada()

        if modo is None:  # Usuario canceló en pedir_entrada
            sys.exit(0)

        if modo == "descarga":
            modo_descarga(valor)
        else:
            modo_conversion(valor)

    except KeyboardInterrupt:
        print("\n\n  Operación cancelada.")
        sys.exit(0)
    except VgmgetError as e:
        print(f"\n  ✘  {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
