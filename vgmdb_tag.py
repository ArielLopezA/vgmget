"""
vgmdb_tag.py — Módulo de etiquetado de archivos .m4a con datos de VGMDB.
Puede importarse desde bgmget o ejecutarse directamente como herramienta standalone.

Tags que escribe (via ffmpeg -c:a copy):
    title     Título de la pista
    artist    Compositor de la pista (o global como fallback)
    composer  Compositor de la pista
    album     Título del álbum
    track     Número de pista / total
    disc      Número de disco / total

La portada se inyecta por separado con atomicparsley.

Uso standalone:
    python3 vgmdb_tag.py [/ruta/al/album/]

Requisitos:
    brew install ffmpeg atomicparsley
"""

import sys
import os
import re
import subprocess
import argparse
from pathlib import Path


# ── Colores ANSI (públicos para que bgmget pueda importarlos) ─────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"

LINEA  = "─" * 45

# Headers para peticiones HTTP — evita bloqueos 403 en servidores que filtran bots
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


# ══════════════════════════════════════════════════════════════
# CLIPBOARD
# ══════════════════════════════════════════════════════════════

def leer_clipboard() -> str:
    """
    Lee el contenido del clipboard via pbpaste (macOS).
    Retorna cadena vacía si pbpaste no está disponible.
    """
    try:
        resultado = subprocess.run(
            ["pbpaste"], capture_output=True, text=True, timeout=3
        )
        texto = resultado.stdout
        # Limpiar espacios, saltos de línea extraños y retornos de carro
        texto = texto.strip().replace("\r\n", "\n").replace("\r", "\n")
        return texto
    except Exception:
        return ""


# ══════════════════════════════════════════════════════════════
# DETECCIÓN Y EXTRACCIÓN DE DATOS VGMDB
# ══════════════════════════════════════════════════════════════

def es_pagina_vgmdb(texto: str) -> bool:
    """
    Detecta si el texto corresponde a una página completa de VGMDB
    buscando marcadores característicos del formato.
    """
    return bool(re.search(
        r"(^Composer\t|^Notes\s*$|^Tracklist\s*$)",
        texto,
        re.MULTILINE
    ))


def _extraer_titulo_album(texto: str) -> str:
    """Extrae el título del álbum desde la primera línea significativa."""
    for linea in texto.splitlines():
        linea = linea.strip()
        if linea and not re.match(
            r"^(Catalog|Barcode|Release|Publish|Media|Classification|Label|Publisher|Phonographic)",
            linea
        ):
            return linea
    return ""


def _extraer_artist_global(texto: str) -> str:
    """Extrae el compositor global del campo Composer."""
    m = re.search(r"^Composer\t(.+)$", texto, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _extraer_tracklist(texto: str) -> dict:
    """
    Extrae {(disco, pista): título} del tracklist de VGMDB.
    Maneja títulos con tabs internos tomando todo entre el primer
    número+tab y el último tab+duración.
    """
    tracks = {}
    disco_actual = 1
    en_tracklist = False

    for linea in texto.splitlines():
        stripped = linea.strip()

        if re.match(r"^Tracklist", stripped, re.IGNORECASE):
            en_tracklist = True
            continue
        if re.match(r"^Notes", stripped, re.IGNORECASE):
            break
        if not en_tracklist or not stripped:
            continue

        m = re.match(r"^Disc\s+(\d+)", stripped, re.IGNORECASE)
        if m:
            disco_actual = int(m.group(1))
            continue

        m = re.match(r"^(\d+)\t(.+)\t(\d+:\d+)$", stripped)
        if m:
            num_pista = int(m.group(1))
            titulo    = m.group(2).strip()
            titulo    = re.sub(r'^"(.+)"$', r'\1', titulo)
            tracks[(disco_actual, num_pista)] = titulo

    return tracks


def _extraer_anio(texto: str) -> str:
    """Extrae el año de lanzamiento desde el campo Release Date."""
    m = re.search(r"^Release Date	.+(\d{4})", texto, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _extraer_catalog(texto: str) -> str:
    """Extrae el número de catálogo desde el campo Catalog Number."""
    m = re.search(r"^Catalog Number	(.+)$", texto, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _extraer_bloque_notas(texto: str) -> str:
    """Extrae el bloque Notes de la página VGMDB (contiene la composición)."""
    m = re.search(r"^Notes\s*\n(.*)", texto, re.DOTALL | re.MULTILINE)
    return m.group(1).strip() if m else ""


def _contar_discos(texto: str) -> int:
    discos = re.findall(r"^Disc\s+(\d+)", texto, re.MULTILINE | re.IGNORECASE)
    return max((int(d) for d in discos), default=1)


def extraer_datos_vgmdb(texto: str) -> dict:
    """
    Extrae todos los campos relevantes de una página VGMDB.
    Retorna dict con: album, artist, tracklist, composicion_texto, disc_total.
    """
    return {
        "album":             _extraer_titulo_album(texto),
        "artist":            _extraer_artist_global(texto),
        "tracklist":         _extraer_tracklist(texto),
        "composicion_texto": _extraer_bloque_notas(texto),
        "disc_total":        _contar_discos(texto),
        "anio":              _extraer_anio(texto),
        "catalog":           _extraer_catalog(texto),
    }


# ══════════════════════════════════════════════════════════════
# PARSEO DE COMPOSICIÓN
# ══════════════════════════════════════════════════════════════

def _detectar_formato(texto: str) -> str:
    if re.search(r"^M\d+", texto, re.MULTILINE | re.IGNORECASE):
        return "mxx"
    return "standard"


def _parsear_formato_mxx(texto: str) -> dict:
    """Parsea formato M01/M02 — un compositor por pista."""
    track_map = {}
    pista_actual = None

    for linea in texto.strip().splitlines():
        linea = linea.strip()
        if not linea:
            continue
        m = re.match(r"^(?:D(\d+))?M(\d+)", linea, re.IGNORECASE)
        if m:
            disco        = int(m.group(1)) if m.group(1) else 1
            pista_actual = (disco, int(m.group(2)))
            continue
        m = re.match(r"^Composition:\s*(.+)", linea, re.IGNORECASE)
        if m and pista_actual is not None:
            raw          = re.sub(r"\s*\(.*?\)", "", m.group(1)).strip()
            compositores = [c.strip() for c in re.split(r"[,&]", raw) if c.strip()]
            if pista_actual not in track_map:
                track_map[pista_actual] = []
            for c in compositores:
                if c and c not in track_map[pista_actual]:
                    track_map[pista_actual].append(c)

    return track_map


def _parsear_formato_standard(texto: str) -> dict:
    """
    Parsea formato estándar — rangos de pistas por compositor.
    Soporta dos variantes:
      · Disco.Pista  → '1.01~05'  (álbumes multidisco)
      · Solo pista   → '1, 2~5'   (álbumes de un disco, sin prefijo de disco)
    Si el token no contiene punto, asume disco 1.
    """
    track_map = {}
    m = re.search(
        r"Composition:\s*\n?(.*?)(?:\n\s*\n|\Z)",
        texto, re.DOTALL | re.IGNORECASE,
    )
    bloque = m.group(1) if m else texto

    for linea in bloque.strip().splitlines():
        linea = linea.strip()
        if not linea or ":" not in linea:
            continue
        parte_compositor, parte_rangos = linea.split(":", 1)
        compositores = [c.strip() for c in re.split(r"[&,]", parte_compositor) if c.strip()]

        for token in parte_rangos.split(","):
            token = token.strip()
            if not token:
                continue

            # Formato con disco explícito: 1.01 o 1.01~05
            m = re.match(r"(\d+)\.(\d+)(?:~(\d+))?", token)
            if m:
                disco  = int(m.group(1))
                inicio = int(m.group(2))
                fin    = int(m.group(3)) if m.group(3) else inicio
            else:
                # Formato sin disco: 1 o 1~5 — asume disco 1
                m = re.match(r"(\d+)(?:~(\d+))?", token)
                if not m:
                    continue
                disco  = 1
                inicio = int(m.group(1))
                fin    = int(m.group(2)) if m.group(2) else inicio

            for pista in range(inicio, fin + 1):
                clave = (disco, pista)
                if clave not in track_map:
                    track_map[clave] = []
                for c in compositores:
                    if c not in track_map[clave]:
                        track_map[clave].append(c)

    return track_map


def parsear_composicion(texto: str) -> dict:
    """
    Detecta el formato del bloque de composición y lo parsea.
    Retorna {(disco, pista): [compositores]}.
    """
    fmt = _detectar_formato(texto)
    if fmt == "mxx":
        return _parsear_formato_mxx(texto)
    return _parsear_formato_standard(texto)


# ══════════════════════════════════════════════════════════════
# ARCHIVOS M4A
# ══════════════════════════════════════════════════════════════

def detectar_disco_pista(nombre: str, disco_fallback: int = 1):
    """
    Intenta detectar disco y pista desde el nombre del archivo.
    Soporta formatos: 1-01, 101, 01, etc.
    Función pública para que bgmget pueda usarla al recopilar archivos.
    """
    stem = re.sub(r"\.m4a$", "", nombre, flags=re.IGNORECASE)
    m = re.match(r"^(\d)[-._](\d{1,2})", stem)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r"^(\d)(\d{2})(?:\D|$)", stem)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r"^(\d{1,2})(?:\D|$)", stem)
    if m:
        return disco_fallback, int(m.group(1))
    return None, None


def recopilar_m4a(carpeta: Path) -> list:
    """
    Recopila archivos .m4a de la carpeta, detectando disco y pista.
    Soporta subcarpetas (álbumes multidisco).
    Filtra carpetas ocultas y carpetas sin M4A para evitar falsos positivos.
    Ignora archivos temporales (.tmp.m4a) que pudieran haber quedado huérfanos.
    Retorna lista de (Path, disco, pista).
    """
    resultados = []
    subdirs = sorted([
        d for d in carpeta.iterdir()
        if d.is_dir()
        and not d.name.startswith(".")   # excluye .AppleDouble y similares
        and any(d.glob("*.m4a"))         # solo carpetas con M4A reales
    ])

    if subdirs:
        for subdir in subdirs:
            m = re.search(r"(\d+)", subdir.name)
            num_disco = int(m.group(1)) if m else (subdirs.index(subdir) + 1)
            # Excluir archivos temporales huérfanos
            archivos = sorted([
                f for f in subdir.glob("*.m4a")
                if not f.name.endswith(".tmp.m4a")
            ])
            for i, ruta in enumerate(archivos):
                _, pista = detectar_disco_pista(ruta.name, disco_fallback=num_disco)
                if pista is None:
                    pista = i + 1
                resultados.append((ruta, num_disco, pista))
    else:
        # Excluir archivos temporales huérfanos
        for ruta in sorted([
            f for f in carpeta.glob("*.m4a")
            if not f.name.endswith(".tmp.m4a")
        ]):
            disco, pista = detectar_disco_pista(ruta.name)
            resultados.append((ruta, disco, pista))

    return resultados


def _total_pistas_por_disco(tracklist: dict, archivos: list) -> dict:
    """Calcula el total de pistas por disco para el tag track."""
    por_disco: dict = {}
    fuente = list(tracklist.keys()) if tracklist else [(d, t) for _, d, t in archivos]
    for (d, _) in fuente:
        por_disco[d] = por_disco.get(d, 0) + 1
    return por_disco


# ══════════════════════════════════════════════════════════════
# CONSTRUCCIÓN DE FILAS Y TABLA
# ══════════════════════════════════════════════════════════════

def construir_filas(archivos: list, datos: dict, track_map: dict) -> list:
    """
    Construye las filas para la tabla previa y el proceso de escritura.
    Retorna lista de (etiqueta, titulo, compositor, nombre_archivo, tiene_compositor_especifico).
    """
    tracklist     = datos.get("tracklist", {})
    artist_global = datos.get("artist", "")
    filas = []

    for ruta, disco, pista in archivos:
        clave          = (disco, pista) if (disco and pista) else None
        compositores   = track_map.get(clave, []) if clave else []
        compositor_str = ", ".join(compositores) if compositores else artist_global
        titulo         = tracklist.get(clave, "") if clave else ""
        etiqueta       = f"{disco}.{pista:02d}" if (disco and pista) else "?"
        filas.append((etiqueta, titulo, compositor_str, ruta.name, bool(compositores)))

    return filas


def imprimir_tabla(filas: list) -> None:
    """
    Imprime la tabla previa con el mapeo disco/pista/compositor.
    Usa colores ANSI solo en la tabla.
    """
    W = {"label": 7, "titulo": 28, "compositor": 24, "archivo": 32}

    print(f"\n  {DIM}{'#':<{W['label']}} {'Título':<{W['titulo']}} {'Compositor':<{W['compositor']}} Archivo{RESET}")
    print(f"  {DIM}{'─' * 94}{RESET}")

    for etiqueta, titulo, compositor, nombre, tiene_especifico in filas:
        estado = f"{GREEN}✓{RESET}" if tiene_especifico else f"{YELLOW}~{RESET}"
        lab    = f"{DIM}{etiqueta:<{W['label']}}{RESET}"
        tit    = titulo[:W["titulo"] - 1] if titulo else f"{DIM}(sin título){RESET}"
        com    = f"{DIM}{compositor[:W['compositor'] - 1]}{RESET}"
        arc    = f"{DIM}{nombre[:W['archivo'] - 1]}{RESET}"
        print(f"  {estado} {lab} {tit:<{W['titulo']}} {com:<{W['compositor']}} {arc}")


# ══════════════════════════════════════════════════════════════
# ESCRITURA DE TAGS — ffmpeg -c:a copy + atomicparsley
# ══════════════════════════════════════════════════════════════

def escribir_tags(
    filepath: Path,
    artist: str,
    composer: str,
    album: str,
    title: str,
    track_num: int,
    track_total: int,
    disc_num: int,
    disc_total: int,
    anio: str = "",
    catalog: str = "",
    genero: str = "Soundtrack",
) -> None:
    """
    Escribe los tags de texto en un archivo .m4a usando ffmpeg -c:a copy.
    No recodifica el audio — solo reescribe el contenedor con los nuevos tags.
    La portada se inyecta por separado con atomicparsley.
    Usa un archivo temporal para evitar colisiones y garantizar atomicidad.
    """
    ruta_temp = str(filepath) + ".tmp.m4a"

    cmd = [
        "ffmpeg",
        "-i", str(filepath),
        "-c:a", "copy",          # Sin recodificación — copia el stream de audio
        "-vn",                   # Ignora streams de video/portada incrustada
        "-map_metadata", "-1",   # Limpia todos los tags previos
        "-metadata", f"title={title}",
        "-metadata", f"artist={artist}",
        "-metadata", f"composer={composer}",
        "-metadata", f"album={album}",
        "-metadata", f"track={track_num}/{track_total}",
        "-metadata", f"disc={disc_num}/{disc_total}",
        "-metadata", f"genre={genero}",
    ] + (
        ["-metadata", f"date={anio}"] if anio else []
    ) + (
        ["-metadata", f"comment={catalog}"] if catalog else []
    ) + [
        "-y",
        "-loglevel", "error",
        ruta_temp,
    ]

    resultado = subprocess.run(cmd, capture_output=True, text=True)

    if resultado.returncode != 0:
        if os.path.exists(ruta_temp):
            os.remove(ruta_temp)
        raise RuntimeError(resultado.stderr.strip())

    os.replace(ruta_temp, str(filepath))


def inyectar_portada(filepath: Path, ruta_portada: str) -> bool:
    """
    Inyecta la portada en un archivo .m4a usando atomicparsley.
    Retorna True si tuvo éxito, False si falló.
    """
    try:
        resultado = subprocess.run(
            [
                "atomicparsley", str(filepath),
                "--artwork", ruta_portada,
                "--overWrite",
            ],
            capture_output=True,
            text=True,
        )
        return resultado.returncode == 0
    except FileNotFoundError:
        return False


def etiquetar_carpeta(
    carpeta: Path,
    datos: dict,
    track_map: dict,
    ruta_portada: str = None,
    skip_confirmation: bool = False,
) -> tuple:
    """
    Escribe los tags en todos los .m4a de la carpeta usando ffmpeg + atomicparsley.
    Muestra la tabla previa siempre. Si skip_confirmation=True, procede directamente
    sin pedir confirmación — útil cuando se invoca desde bgmget, donde el usuario
    ya confirmó el etiquetado en el paso anterior.
    Retorna (n_ok, n_fallback, n_error) o None si el usuario cancela.
    """
    archivos   = recopilar_m4a(carpeta)
    filas      = construir_filas(archivos, datos, track_map)
    tracklist  = datos.get("tracklist", {})
    totales    = _total_pistas_por_disco(tracklist, archivos)
    disc_total = datos.get("disc_total", 1)

    # Tabla previa — siempre visible
    imprimir_tabla(filas)

    # Confirmación — solo si no viene de bgmget
    if not skip_confirmation:
        print()
        try:
            confirmar = input("  ¿Escribir tags? [S/n]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\n\n  Etiquetado cancelado.")
            return None
        if confirmar not in ("s", "si", "sí", ""):
            print("  Etiquetado cancelado. Archivos sin modificar.")
            return None

    n_ok = n_fallback = n_error = 0

    for (ruta, disco, pista), (etiqueta, titulo, compositor, _, tiene_especifico) in zip(archivos, filas):
        t_total = totales.get(disco, 0)
        try:
            escribir_tags(
                filepath    = ruta,
                artist      = compositor,
                composer    = compositor,
                album       = datos.get("album", ""),
                title       = titulo,
                track_num   = pista or 0,
                track_total = t_total,
                disc_num    = disco or 1,
                disc_total  = disc_total,
                anio        = datos.get("anio", ""),
                catalog     = datos.get("catalog", ""),
                genero      = "Soundtrack",
            )
            if ruta_portada:
                inyectar_portada(ruta, ruta_portada)

            print(f"     {etiqueta}  {ruta.name} ✓")
            if tiene_especifico:
                n_ok += 1
            else:
                n_fallback += 1
        except Exception as e:
            print(f"     {etiqueta}  {ruta.name}  ✘  {e}")
            n_error += 1

    return n_ok, n_fallback, n_error


# ══════════════════════════════════════════════════════════════
# PUNTO DE ENTRADA STANDALONE
# ══════════════════════════════════════════════════════════════

def _pedir_carpeta() -> Path:
    """Solicita la ruta de la carpeta interactivamente si no se pasó como argumento."""
    print()
    try:
        ruta = input("  Carpeta con los .m4a: ").strip().strip("'\"").replace("\\ ", " ")
    except (KeyboardInterrupt, EOFError):
        print("\n\n  Operación cancelada.")
        sys.exit(0)

    ruta = os.path.expanduser(ruta)
    if not ruta:
        print("  ✘  No se ingresó ninguna ruta.")
        sys.exit(1)
    if not os.path.isdir(ruta):
        print(f"  ✘  La ruta no existe o no es una carpeta: {ruta}")
        sys.exit(1)
    return Path(ruta)


def _leer_datos_vgmdb() -> str:
    """
    Lee datos VGMDB desde clipboard o archivo .txt.
    Aplica strip() al clipboard para limpiar espacios y saltos de línea extraños
    que pueden colarse al copiar desde el navegador o la terminal.
    """
    print("\n  Leyendo clipboard...")
    texto = leer_clipboard().strip()  # strip() antes de validar

    if texto and es_pagina_vgmdb(texto):
        print("  ✓  Datos de VGMDB encontrados en clipboard")
        return texto

    print("  ⚠  El clipboard no contiene datos válidos de VGMDB.")
    print()
    try:
        ruta_txt = input("  Ruta al archivo .txt con los datos (Enter para cancelar): ").strip().strip("'\"")
    except (KeyboardInterrupt, EOFError):
        print("\n\n  Operación cancelada.")
        sys.exit(0)

    if not ruta_txt:
        print("  Operación cancelada.")
        sys.exit(0)

    ruta_txt = os.path.expanduser(ruta_txt.replace("\\ ", " "))
    if not os.path.isfile(ruta_txt):
        print("  ✘  Archivo no encontrado.")
        sys.exit(1)

    with open(ruta_txt, encoding="utf-8") as f:
        texto = f.read().strip()  # strip() también al leer desde archivo

    if not es_pagina_vgmdb(texto):
        print("  ✘  El archivo no contiene datos válidos de VGMDB.")
        sys.exit(1)

    return texto


def main():
    parser = argparse.ArgumentParser(description="Tagger de .m4a con datos de VGMDB.", add_help=False)
    parser.add_argument("folder", nargs="?")
    args, _ = parser.parse_known_args()

    print("\n♬ vgmdb-tag")
    print(LINEA)

    # 1. Carpeta
    if args.folder:
        carpeta = Path(args.folder).expanduser().resolve()
        if not carpeta.is_dir():
            print(f"  ✘  Carpeta no encontrada: {carpeta}")
            sys.exit(1)
    else:
        carpeta = _pedir_carpeta()

    archivos = recopilar_m4a(carpeta)
    if not archivos:
        print(f"  ✘  No se encontraron archivos .m4a en: {carpeta}")
        sys.exit(1)

    print(f"  ✓  {len(archivos)} archivo(s) en {carpeta.name}")

    # 2. Datos VGMDB
    texto     = _leer_datos_vgmdb()
    datos     = extraer_datos_vgmdb(texto)
    track_map = parsear_composicion(datos["composicion_texto"])

    if datos["album"]:
        print(f"  ✓  Album     {datos['album']}")
    if datos["artist"]:
        print(f"  ✓  Artist    {datos['artist']}")
    if datos["anio"]:
        print(f"  ✓  Año       {datos['anio']}")
    if datos["catalog"]:
        print(f"  ✓  Catalog   {datos['catalog']}")
    if datos["tracklist"]:
        print(f"  ✓  Tracklist {len(datos['tracklist'])} pista(s)  —  {datos['disc_total']} disco(s)")

    # 3. Portada opcional
    print()
    try:
        url_portada = input("  URL de portada (Enter para omitir): ").strip()
    except (KeyboardInterrupt, EOFError):
        url_portada = ""

    ruta_portada = None
    if url_portada:
        try:
            import requests as _requests
            r = _requests.get(url_portada, headers=HEADERS, timeout=15)
            r.raise_for_status()
            ext          = ".png" if url_portada.lower().endswith(".png") else ".jpg"
            ruta_portada = str(carpeta / f"_cover_vgmdb{ext}")
            with open(ruta_portada, "wb") as f:
                f.write(r.content)
            print(f"  ✓  Portada descargada ({len(r.content) // 1024} KB)")
        except Exception as e:
            print(f"  ⚠  No se pudo descargar la portada: {e}")

    # 4. Tabla previa, confirmación y escritura de tags
    print(f"\n{LINEA}")
    resultado = etiquetar_carpeta(carpeta, datos, track_map, ruta_portada, skip_confirmation=False)
    if resultado is None:
        sys.exit(0)
    n_ok, n_fallback, n_error = resultado

    # Limpiar portada temporal si se descargó
    if ruta_portada and os.path.exists(ruta_portada):
        os.remove(ruta_portada)

    # 6. Resumen
    print(f"\n{LINEA}")
    if n_ok:
        print(f"  ✓  {n_ok} pista(s) con compositor específico")
    if n_fallback:
        print(f"  ~  {n_fallback} pista(s) con artist global")
    if n_error:
        print(f"  ✘  {n_error} error(es)")


if __name__ == "__main__":
    main()
