#!/usr/bin/env python3
"""Licitaciones deportivas abiertas en PLACE (toda España) a partir de la
sindicación ATOM que Hacienda publica como datos abiertos.

Se ejecuta fuera del agente (cron, Programador de tareas o el Bash de un agente
de código) y deja un JSON y un Markdown que el agente lee. Solo usa la
biblioteca estándar de Python 3.9+.

    python3 place_deporte.py --dias 7 --salida ./place

Buenas maneras con el origen: una sola conexión, pausa entre páginas,
User-Agent que te identifica y tope de páginas. No lo lances más de una vez al día.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import re
import sys
import time
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

FEEDS = {
    # Perfiles del contratante alojados en PLACE (AGE, muchos ayuntamientos...)
    "perfiles": "https://contrataciondelsectorpublico.gob.es/sindicacion/sindicacion_643/licitacionesPerfilesContratanteCompleto3.atom",
    # Plataformas autonómicas agregadas (Cataluña, Madrid, Andalucía, Euskadi, Galicia...), sin menores
    "agregadas": "https://contrataciondelsectorpublico.gob.es/sindicacion/sindicacion_1044/PlataformasAgregadasSinMenores.atom",
}
USER_AGENT = "SE360-vigilancia (lectura diaria de datos abiertos con pausa; contacto: info@sportevents360.com)"
PAUSA_S = 2.0
ESTADOS = {"PRE": "Anuncio previo", "PUB": "En plazo", "EV": "Pendiente de adjudicación",
           "ADJ": "Adjudicada", "RES": "Resuelta", "ANUL": "Anulada"}
TIPOS = {"1": "Suministros", "2": "Servicios", "3": "Obras", "21": "Gestión de servicios públicos",
         "22": "Concesión de servicios", "31": "Concesión de obras públicas", "32": "Concesión de obras",
         "40": "Colaboración público-privada", "7": "Administrativo especial", "8": "Privado", "50": "Patrimonial"}
TIPOS_FUERA = {"1", "3", "31", "32"}  # suministros y obras
CPV_DEPORTE = ("926", "7995")
PALABRAS = [
    "deport", "esport", "carrera", "cursa", "trail", "marato", "maraton", "btt", "bicicleta", "ciclis",
    "atletis", "triatl", "duatl", "marcha", "marxa", "caminata", "caminada", "senderis", "orientacio",
    "escalada", "campus", "cronometr", "torneo", "torneig", "campeonato", "campionat", "competicio",
    "natacio", "natacion", "padel", "pilates", "yoga", "zumba", "spinning", "actividad fisica",
    "activitat fisica", "juegos escolares", "jocs escolars", "liga escolar", "lliga escolar",
    "multideporte", "multiesport", "dinamitzacio esportiva", "dinamizacion deportiva",
    "san silvestre", "sant silvestre", "media maraton", "mitja marato",
]
# Expresiones que contienen una palabra clave pero no son deporte. Se borran del
# título antes de buscar (en minúsculas y sin tildes).
EXCLUSIONES = [
    "puesta en marcha", "posada en marxa", "posta en marcha", "en marcha",
    "carrera profesional", "carrera administrativa", "carrera horizontal", "carrera judicial",
    "orientacion laboral", "orientacio laboral", "orientacion profesional", "orientacio professional",
    "orientacion educativa", "orientacio educativa", "orientacion juridica", "orientacio juridica",
    "orientacion sociolaboral", "orientacio sociolaboral", "orientacion a las familias",
    "via ciclista", "vias ciclistas", "carril bici", "carril bicicleta", "carril ciclable",
    "aparcamiento de bicicletas", "aparcabicicletas", "aparcament de bicicletes", "bicicleta publica",
    "bicicleta compartida", "bicing", "bicicletas electricas de servicio",
    "campus universitario", "campus universitari", "campus de salud", "campus de salut",
    "administracion concursal", "concursal",
    "torneo de debate", "torneig de debat",
]
# Si el título tiene una de estas y no lleva CPV 926/7995, no es organización de
# eventos ni de actividades (mantenimiento, limpieza, obras, consultoría...).
NEGATIVAS = [
    "mantenimiento", "manteniment", "limpieza", "neteja", "conservacion", "conservacio",
    "obras", "obres", "urbaniza", "redaccion de proyecto", "redaccio de projecte",
    "suministro", "subministrament", "alojamiento", "allotjament", "retransmision", "retransmissio",
    "consultoria", "assessorament", "asesoramiento", "auditoria", "vigilancia", "seguridad privada",
    "seguretat", "transporte", "transport", "catering", "restauracion", "cafeteria", "bar ",
]


def norm(texto: str) -> str:
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", t.lower())


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def hijos(el, nombre):
    return [h for h in el if local(h.tag) == nombre]


def primero(el, *ruta):
    """Primer descendiente siguiendo la ruta de nombres locales (ignora namespaces)."""
    actual = [el]
    for nombre in ruta:
        siguiente = []
        for a in actual:
            siguiente.extend(d for d in a.iter() if d is not a and local(d.tag) == nombre)
        if not siguiente:
            return None
        actual = siguiente
    return actual[0]


def texto(el, *ruta):
    n = primero(el, *ruta)
    return (n.text or "").strip() if n is not None and n.text else ""


def descargar(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/atom+xml,*/*"})
    with urllib.request.urlopen(req, timeout=120) as r:
        cuerpo = r.read()
    if not cuerpo.lstrip().startswith(b"<?xml") and b"<feed" not in cuerpo[:2000]:
        raise RuntimeError(f"respuesta que no es ATOM (¿página de bloqueo?) en {url}")
    return cuerpo


def fecha(s: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def leer_entrada(e) -> dict:
    cfs = primero(e, "ContractFolderStatus")
    enlace = next((l.get("href") for l in hijos(e, "link") if l.get("href")), "")
    cpvs = [n.text.strip() for n in (cfs.iter() if cfs is not None else [])
            if local(n.tag) == "ItemClassificationCode" and n.text]
    plazo_f = texto(cfs, "TenderSubmissionDeadlinePeriod", "EndDate") if cfs is not None else ""
    plazo_h = texto(cfs, "TenderSubmissionDeadlinePeriod", "EndTime") if cfs is not None else ""
    g = lambda *r: texto(cfs, *r) if cfs is not None else ""
    return {
        "expediente": g("ContractFolderID"),
        "titulo": texto(e, "title") or g("ProcurementProject", "Name"),
        "resumen": texto(e, "summary"),
        "organo": g("LocatedContractingParty", "PartyName", "Name"),
        "estado_codigo": g("ContractFolderStatusCode"),
        "estado": ESTADOS.get(g("ContractFolderStatusCode"), g("ContractFolderStatusCode")),
        "tipo": TIPOS.get(g("ProcurementProject", "TypeCode"), g("ProcurementProject", "TypeCode")),
        "tipo_codigo": g("ProcurementProject", "TypeCode"),
        "presupuesto_sin_iva": g("ProcurementProject", "BudgetAmount", "TaxExclusiveAmount") or "no indicado",
        "valor_estimado": g("ProcurementProject", "BudgetAmount", "EstimatedOverallContractAmount") or "no indicado",
        "cpv": sorted(set(cpvs)),
        "lugar": g("ProcurementProject", "RealizedLocation", "CountrySubentity") or "no indicado",
        "procedimiento_codigo": g("TenderingProcess", "ProcedureCode"),
        "fecha_limite": (plazo_f + (" " + plazo_h if plazo_h else "")).strip() or "no indicado",
        "actualizado": texto(e, "updated"),
        "enlace": enlace,
    }


def por_que(l: dict) -> list[str]:
    """Motivos por los que la licitación es deportiva; lista vacía = se descarta.

    - CPV 926* (servicios deportivos): pasa siempre.
    - CPV 7995* (eventos en general: fiestas, cabalgatas, congresos...): solo pasa si
      además el título tiene una palabra de deporte.
    - Sin esos CPV: pasa si el título tiene una palabra de deporte y no es un contrato
      de mantenimiento, limpieza, obras, consultoría, etc.
    Se mira solo el título: el resumen incluye el nombre del órgano ("Consejería de
    Cultura y Deporte", "Patronat d'Esports"...) y metía contratos sin relación.
    """
    cpv_926 = [f"CPV {c}" for c in l["cpv"] if c.startswith("926")]
    cpv_7995 = [f"CPV {c}" for c in l["cpv"] if c.startswith("7995")]
    t = norm(l["titulo"])
    for frase in EXCLUSIONES:
        t = t.replace(frase, " ")
    palabras = [f"palabra «{p}»" for p in PALABRAS if p in t]
    if cpv_926:
        return cpv_926 + cpv_7995 + palabras
    if not palabras:
        return []
    if not cpv_7995 and any(n in t for n in NEGATIVAS):
        return []
    return cpv_7995 + palabras


def abierta(l: dict, hoy: dt.date) -> bool:
    if l["estado_codigo"] not in ("PUB", "PRE"):
        return False
    f = l["fecha_limite"][:10]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", f):
        return dt.date.fromisoformat(f) >= hoy
    return True  # sin plazo publicado: se deja para revisar


def recorrer(nombre: str, url: str, desde: dt.datetime, max_paginas: int, informe: dict):
    vistos, paginas = {}, 0
    while url and paginas < max_paginas:
        cuerpo = descargar(url)
        paginas += 1
        raiz = ET.parse(io.BytesIO(cuerpo)).getroot()
        entradas = hijos(raiz, "entry")
        mas_reciente_vieja = True
        for e in entradas:
            l = leer_entrada(e)
            act = fecha(l["actualizado"])
            if act and act < desde:
                continue
            mas_reciente_vieja = False
            clave = l["enlace"] or l["expediente"]
            # el fichero vivo va de lo más nuevo a lo más viejo: la primera versión vista es la última
            vistos.setdefault(clave, l)
        siguiente = next((x.get("href") for x in hijos(raiz, "link") if x.get("rel") == "next"), None)
        if mas_reciente_vieja and entradas:
            break  # toda la página es anterior a la ventana
        url = siguiente
        if url:
            time.sleep(PAUSA_S)
    informe["fuentes"][nombre] = {
        "paginas": paginas,
        "estado": "OK" if paginas < max_paginas else "PARCIAL (tope de páginas alcanzado)",
    }
    return vistos


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=int, default=4)
    ap.add_argument("--max-paginas", type=int, default=120, help="por fuente; 500 entradas por página")
    ap.add_argument("--salida", default="place_salida")
    ap.add_argument("--incluir-obras-y-suministros", action="store_true")
    a = ap.parse_args()

    ahora = dt.datetime.now(dt.timezone.utc)
    desde = ahora - dt.timedelta(days=a.dias)
    informe = {"generado": ahora.isoformat(timespec="seconds"), "desde": desde.date().isoformat(),
               "fuentes": {}, "licitaciones": []}
    todas = {}
    for nombre, url in FEEDS.items():
        try:
            todas.update(recorrer(nombre, url, desde, a.max_paginas, informe))
        except Exception as exc:  # noqa: BLE001 - se informa y se sigue con la otra fuente
            informe["fuentes"][nombre] = {"estado": f"KO: {exc}"}

    for l in todas.values():
        motivos = por_que(l)
        if not motivos or not abierta(l, ahora.date()):
            continue
        if l["tipo_codigo"] in TIPOS_FUERA and not a.incluir_obras_y_suministros:
            continue
        l["detectado_por"] = motivos
        informe["licitaciones"].append(l)
    informe["licitaciones"].sort(key=lambda l: l["fecha_limite"])

    out = Path(a.salida)
    out.mkdir(parents=True, exist_ok=True)
    (out / "place_deporte.json").write_text(json.dumps(informe, ensure_ascii=False, indent=2), encoding="utf-8")
    filas = ["| Límite | Título | Órgano | Lugar | Presupuesto s/IVA | Estado | Detectado por | Enlace |",
             "|---|---|---|---|---|---|---|---|"]
    for l in informe["licitaciones"]:
        filas.append("| {fecha_limite} | {titulo} | {organo} | {lugar} | {presupuesto_sin_iva} | {estado} | {m} | {enlace} |"
                     .format(m=", ".join(l["detectado_por"][:3]), **{k: str(v).replace("|", "/") for k, v in l.items()}))
    fuentes = "\n".join(f"- {k}: {v}" for k, v in informe["fuentes"].items())
    (out / "place_deporte.md").write_text(
        f"# PLACE · deporte · desde {informe['desde']}\n\nGenerado: {informe['generado']} (UTC)\n\nFuentes:\n{fuentes}\n\n" + "\n".join(filas) + "\n",
        encoding="utf-8")
    print(f"{len(informe['licitaciones'])} candidatas · {informe['fuentes']}")
    return 0 if all(v["estado"] == "OK" for v in informe["fuentes"].values()) else 2


if __name__ == "__main__":
    sys.exit(main())
