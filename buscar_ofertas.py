#!/usr/bin/env python3
"""
Buscador de ofertas de singles de Magic en tiendas chilenas.

Compara el precio de cada carta en stock contra su precio de referencia
(Card Kingdom según condición, en USD, convertido a CLP) y avisa por WhatsApp y correo
cuando encuentra cartas bajo el umbral configurado.

Uso:
  python buscar_ofertas.py                 # corrida normal
  python buscar_ofertas.py --sin-avisos    # solo muestra en pantalla
  python buscar_ofertas.py --probar-avisos # envía un mensaje de prueba
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import smtplib
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
import yaml

RAIZ = Path(__file__).resolve().parent
CACHE = RAIZ / ".cache"
ESTADO = RAIZ / "state" / "avisadas.json"
UA = "MTGOfertasCL/1.0 (+uso personal; contacto via GitHub)"
SESION = requests.Session()
SESION.headers.update({"User-Agent": UA, "Accept": "application/json"})


# ---------------------------------------------------------------- utilidades
def normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    texto = texto.lower().replace("’", "'")
    texto = re.sub(r"[^a-z0-9' /]+", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def clp(n: float) -> str:
    return "$" + f"{round(n):,}".replace(",", ".")


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- referencia
# Referencia: lista de precios pública de Card Kingdom (la que usan las tiendas
# chilenas), con precio por condición: nm, ex (≈LP), vg (≈MP), g (≈HP/DMG).
CK_URL = "https://api.cardkingdom.com/api/v2/pricelist"
CONDICIONES = {"NM": "nm", "LP": "ex", "MP": "vg", "HP": "g", "DMG": "g"}


@dataclass
class Impresion:
    codigos: set[str]          # códigos de edición (sku de CK, con y sin la F de foil)
    set_name: str              # nombre de edición normalizado
    foil: bool
    precios: dict[str, float]  # nm / ex / vg / g en USD
    variacion: str = ""

    def precio(self, cond: str) -> float | None:
        return self.precios.get(CONDICIONES.get(cond, "nm")) or self.precios.get("nm")


def descargar_ck() -> Path:
    """Descarga (1 vez al día) la lista de precios de Card Kingdom, comprimida."""
    CACHE.mkdir(exist_ok=True)
    hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    destino = CACHE / f"ck-{hoy}.json.gz"
    if destino.exists():
        return destino
    for viejo in CACHE.glob("ck-*.json.gz"):
        viejo.unlink()
    log("Descargando lista de precios de Card Kingdom…")
    with SESION.get(CK_URL, stream=True, timeout=600) as r:
        r.raise_for_status()
        with gzip.open(destino, "wb") as f:
            for trozo in r.iter_content(1 << 20):
                f.write(trozo)
    return destino


def cargar_referencias(ruta: Path) -> dict[str, list[Impresion]]:
    """nombre normalizado -> lista de impresiones de CK con sus precios por condición."""
    import ijson

    refs: dict[str, list[Impresion]] = {}
    with gzip.open(ruta, "rb") as f:
        for c in ijson.items(f, "data.item"):
            cv = c.get("condition_values") or {}
            precios = {k: float(cv[f"{k}_price"]) for k in ("nm", "ex", "vg", "g")
                       if cv.get(f"{k}_price") and float(cv[f"{k}_price"]) > 0}
            if not precios and c.get("price_retail") and float(c["price_retail"]) > 0:
                precios = {"nm": float(c["price_retail"])}
            if not precios:
                continue
            variacion = (c.get("variation") or "").lower()
            if "display commander" in variacion or "oversized" in variacion:
                continue  # cartas grandes de exhibición: no son la carta jugable
            pref = c.get("sku", "").split("-")[0].lower()
            foil = str(c.get("is_foil")).lower() == "true"
            codigos = {pref} | ({pref[1:]} if foil and pref.startswith("f") else set())
            imp = Impresion(codigos, normalizar(c.get("edition", "")), foil, precios,
                            normalizar(c.get("variation") or ""))
            nombres = {normalizar(c["name"])}
            if " // " in c["name"]:
                nombres.add(normalizar(c["name"].split(" // ")[0]))
            for n in nombres:
                refs.setdefault(n, []).append(imp)
    log(f"Referencias Card Kingdom cargadas: {len(refs):,} cartas")
    return refs


def dolar_del_dia(cfg) -> float:
    """Dólar para pasar CK a pesos. Por defecto el 'dólar CK' de las tiendas (800)."""
    valor = cfg.get("dolar_ck", cfg.get("dolar", 800))
    if valor != "auto":
        return float(valor)
    try:
        r = SESION.get("https://mindicador.cl/api/dolar", timeout=20).json()
        return float(r["serie"][0]["valor"])
    except Exception as e:  # noqa: BLE001
        log(f"No pude leer el dólar del día ({e}); uso 800")
        return 800.0


# ---------------------------------------------------------------- tiendas
@dataclass
class Oferta:
    tienda: str
    titulo: str
    carta: str
    precio: int
    referencia: float
    edicion: str
    foil: bool | None
    idioma: str
    url: str
    clave: str
    edicion_confirmada: bool = True
    condicion: str = "NM"
    ref_mas_barata: float = 0.0   # impresión más barata de la carta (cualquier edición)

    @property
    def descuento(self) -> float:
        return 1 - self.precio / self.referencia


def permitido_por_robots(base: str, ruta: str) -> bool:
    rp = RobotFileParser()
    try:
        r = SESION.get(urljoin(base, "/robots.txt"), timeout=20)
        if r.status_code >= 400:
            return True  # sin robots.txt = sin restricciones declaradas
        rp.parse(r.text.splitlines())
    except requests.RequestException:
        return True
    return rp.can_fetch(UA, urljoin(base, ruta))


SEPARADORES = re.compile(r"\s*[\[\(\|]|\s+-\s+|\s+–\s+")
PREFIJOS = re.compile(r"^(mtg|magic|single|carta)\s*[:\-]?\s*", re.I)
IDIOMAS = {
    "espanol": "ES", "spanish": "ES", "(es)": "ES", "ingles": "EN", "english": "EN",
    "japones": "JA", "japanese": "JA", "portugues": "PT", "italiano": "IT",
    "aleman": "DE", "frances": "FR", "chino": "ZH", "coreano": "KO", "ruso": "RU",
}


def extraer_nombre(titulo: str) -> str:
    t = PREFIJOS.sub("", titulo.strip())
    t = PREFIJOS.sub("", t)
    return SEPARADORES.split(t, maxsplit=1)[0].strip()


def detectar_idioma(texto: str) -> str:
    n = normalizar(texto)
    for clave, cod in IDIOMAS.items():
        if clave.strip("()") in n.split() or clave in texto.lower():
            return cod
    return "EN?"


def detectar_condicion(texto: str) -> str:
    n = normalizar(texto)
    if re.search(r"near mint|\bnm\b|\bmint\b", n):
        return "NM"
    if re.search(r"lightly|slightly|\blp\b|\bsp\b|\bex\b|excellent|ligeramente", n):
        return "LP"
    if re.search(r"moderately|\bmp\b|\bvg\b|moderadamente|played", n):
        return "MP"
    if re.search(r"heavily|\bhp\b|damaged|\bdmg\b|\bpo\b|muy jugada|danad", n):
        return "HP"
    return "NM"  # sin dato: se asume NM (es lo que suelen publicar las tiendas)


def detectar_foil(texto: str) -> bool | None:
    """True si la tienda dice foil, False si dice explícitamente normal/non-foil,
    None si no lo dice. Ojo: en mazos Commander recientes la versión foil es la
    común (y barata) y la normal es la variante cara, así que sin dato NO se puede
    asumir 'normal'."""
    t = texto.lower()
    if re.search(r"non.?foil|no foil|sin foil|\bnormal\b|\bregular\b", t):
        return False
    if re.search(r"foil|etched|surge|galaxy|textured|confetti", t):
        return True
    return None


def acabado_variante(p: dict, v: dict) -> bool | None:
    """Acabado de una variante Shopify. Muchas tiendas (p.ej. Paytowin) publican
    'Near Mint' y 'Near Mint Foil' como variantes del mismo producto, con SKU
    ...-NF-... (non-foil) y ...-FO-... (foil): si el producto tiene variantes foil,
    la que no dice foil es normal."""
    sku = (v.get("sku") or "").upper()
    if re.search(r"NON-?FOIL|(^|-)NF(-|$)", sku):
        return False
    if re.search(r"FOIL|(^|-)FO(-|$)", sku):   # p.ej. Piedra Bruja: FIC462FoilEN
        return True
    propio = detectar_foil(f"{p['title']} {v.get('title', '')}")
    if propio is not None:
        return propio
    hay_foil = any("foil" in (x.get("title") or "").lower() for x in p.get("variants", []))
    return False if hay_foil else None


def _por_acabado(imps: list[Impresion], foil: bool | None) -> list[Impresion]:
    if foil is None:          # acabado desconocido: se consideran ambos (gana el más barato)
        return imps
    mismas = [i for i in imps if i.foil == foil]
    return mismas or imps


def precio_referencia(
    imps: list[Impresion], texto: str, foil: bool | None, cond: str = "NM"
) -> tuple[float | None, str, bool]:
    """Precio CK (USD) de la edición que calza con el título, en la condición de la
    carta. Si no se reconoce la edición, usa la edición MÁS BARATA (conservador)."""
    n = f" {normalizar(texto)} "
    codigos = {c.lower() for c in re.findall(r"[\[\(]([A-Za-z0-9]{3,5})(?:\s*[-#]\s*[^\]\)]*)?[\]\)]", texto)}
    # 1) primero la edición (en cualquier acabado), 2) después el acabado dentro de ella:
    #    si la edición solo existe en foil (p.ej. Secret Lair), se usa esa versión.
    calzan = [i for i in imps if i.codigos & codigos or (len(i.set_name) > 4 and f" {i.set_name} " in n)]
    candidatas = _por_acabado(calzan, foil) if calzan else _por_acabado(imps, foil)
    precios = [(i.precio(cond), i) for i in candidatas if i.precio(cond)]
    if not precios:
        return None, "", False
    v, imp = min(precios, key=lambda x: x[0])
    return v, sorted(imp.codigos, key=len)[0].upper(), bool(calzan)


def mas_barata(imps: list[Impresion], foil: bool | None, cond: str = "NM") -> float:
    vals = [i.precio(cond) for i in _por_acabado(imps, foil)]
    vals = [v for v in vals if v]
    return min(vals) if vals else 0.0


def leer_shopify(tienda: dict, cfg: dict, refs, dolar: float) -> list[Oferta]:
    base = tienda["url"].rstrip("/")
    if not permitido_por_robots(base, "/products.json"):
        log(f"  ⛔ {tienda['nombre']}: su robots.txt no permite lectura automática, la salto.")
        return []
    filtro = [normalizar(f) for f in tienda.get("filtro", [])]
    excluir = [normalizar(x) for x in cfg.get("excluir_si_contiene", [])]
    ofertas, pagina, vistos = [], 1, 0
    while pagina <= tienda.get("max_paginas", 200):
        r = SESION.get(f"{base}/products.json", params={"limit": 250, "page": pagina}, timeout=30)
        if r.status_code == 429:
            time.sleep(10)
            continue
        if r.status_code != 200:
            log(f"  ⚠️ {tienda['nombre']}: HTTP {r.status_code} en página {pagina}")
            break
        productos = r.json().get("products", [])
        if not productos:
            break
        for p in productos:
            meta = normalizar(" ".join([p.get("product_type", ""), " ".join(p.get("tags", [])), p["title"]]))
            if filtro and not any(f in meta for f in filtro):
                continue
            if OTROS_JUEGOS.search(p.get("product_type", "")):
                continue
            if any(f" {x} " in f" {normalizar(p['title'])} " for x in excluir):
                continue
            vistos += 1
            nombre = extraer_nombre(p["title"])
            imps = refs.get(normalizar(nombre))
            if not imps:
                continue
            for v in p.get("variants", []):
                if not v.get("available"):
                    continue
                texto = " ".join([p["title"], v.get("title", ""), f"[{p.get('product_type', '')}]", " ".join(p.get("tags", []))])
                # Ojo: algunas tiendas ponen "Foil" en los tags de TODAS las cartas,
                # así que el foil se decide solo por título y variante.
                foil = acabado_variante(p, v)
                cond = detectar_condicion(f"{v.get('title', '')} {p['title']}")
                usd, ed, ok = precio_referencia(imps, texto, foil, cond)
                if not usd:
                    continue
                ofertas.append(Oferta(
                    tienda=tienda["nombre"], titulo=p["title"], carta=nombre,
                    precio=round(float(v["price"])), referencia=usd * dolar,
                    edicion=ed, foil=foil, idioma=detectar_idioma(texto),
                    url=f"{base}/products/{p['handle']}?variant={v['id']}",
                    clave=f"{base}|{v['id']}", edicion_confirmada=ok,
                    condicion=cond, ref_mas_barata=mas_barata(imps, foil, cond) * dolar,
                ))
        pagina += 1
        time.sleep(1.5)  # amable con la tienda
    log(f"  ✓ {tienda['nombre']}: {vistos} singles revisados")
    CONTEO["singles"] += vistos
    CONTEO["tiendas"] += 1
    return ofertas


def _limpiar_html(t: str) -> str:
    import html
    return html.unescape(t).replace("–", "-").replace("—", "-")


OTROS_JUEGOS = re.compile(r"pokemon|pok[eé]mon|yu-?gi|one piece|lorcana|flesh|digimon|riftbound|star wars|dragon ball|weiss|union arena", re.I)


def leer_woocommerce(tienda: dict, cfg: dict, refs, dolar: float) -> list[Oferta]:
    """Usa la Store API pública de WooCommerce (/wp-json/wc/store/v1)."""
    base = tienda["url"].rstrip("/")
    api = f"{base}/wp-json/wc/store/v1"
    if not permitido_por_robots(base, "/wp-json/wc/store/v1/products"):
        log(f"  ⛔ {tienda['nombre']}: su robots.txt no permite lectura automática, la salto.")
        return []
    cats = []
    for pg in range(1, 30):
        lote = SESION.get(f"{api}/products/categories", params={"per_page": 100, "page": pg}, timeout=30).json()
        if not isinstance(lote, list) or not lote:
            break
        cats += lote
    filtro = [normalizar(f) for f in tienda.get("filtro", ["single"])]
    raiz = {int(x) for x in tienda.get("categorias", [])} or {
        c["id"] for c in cats
        if any(f in normalizar(c["name"]) for f in filtro) and not OTROS_JUEGOS.search(c["name"])}
    # incluir subcategorías (muchas tiendas ordenan los singles por edición)
    elegidas, cambio = set(raiz), True
    while cambio:
        nuevas = {c["id"] for c in cats if c.get("parent") in elegidas} - elegidas
        elegidas |= nuevas
        cambio = bool(nuevas)
    ids = [str(i) for i in sorted(elegidas)]
    if not ids:
        log(f"  ⚠️ {tienda['nombre']}: no encontré categoría de singles")
        return []
    excluir = [normalizar(x) for x in cfg.get("excluir_si_contiene", [])]
    ofertas, vistos = [], 0
    for pagina in range(1, tienda.get("max_paginas", 400) + 1):
        r = SESION.get(f"{api}/products", timeout=30, params={
            "per_page": 100, "page": pagina, "category": ",".join(ids), "stock_status": "instock"})
        if r.status_code != 200 or not r.json():
            break
        for p in r.json():
            if not p.get("is_in_stock"):
                continue
            titulo = _limpiar_html(p["name"])
            if any(f" {x} " in f" {normalizar(titulo)} " for x in excluir):
                continue
            atrib = {normalizar(a["name"]).replace("pa_", ""): " ".join(t["name"] for t in a.get("terms", []))
                     for a in p.get("attributes", [])}
            nombre = extraer_nombre(titulo)
            vistos += 1
            imps = refs.get(normalizar(nombre))
            if not imps:
                continue
            # "Edicion", "Edición Magic", "Expansion"... cada tienda lo llama distinto
            edicion = next((v for k, v in atrib.items()
                            if k.startswith(("edicion", "edition", "expansion", "set"))), "")
            if not edicion:  # algunas tiendas usan la categoría como edición
                edicion = next((_limpiar_html(c["name"]) for c in p.get("categories", [])
                                if not any(f in normalizar(c["name"]) for f in filtro)), "")
            texto = f"{titulo} [{edicion}] {atrib.get('idioma', '')} {atrib.get('foil', '')}"
            terminos = next((a.get("terms", []) for a in p.get("attributes", [])
                             if normalizar(a["name"]).replace("pa_", "").startswith(("foil", "acabado", "finish"))), [])
            if len(terminos) > 1:      # p.ej. Card Nexus marca "Foil/No Foil" a la vez: no se sabe
                foil = None
            elif terminos:
                foil = detectar_foil(terminos[0]["name"])
            else:
                foil = detectar_foil(titulo)
            cond = detectar_condicion(" ".join(v for k, v in atrib.items()
                                              if k.startswith(("condicion", "estado", "condition"))) + " " + titulo)
            usd, ed, ok = precio_referencia(imps, texto, foil, cond)
            if not usd:
                continue
            pr = p["prices"]
            precio = int(pr["price"]) / (10 ** pr.get("currency_minor_unit", 0))
            ofertas.append(Oferta(
                tienda=tienda["nombre"], titulo=titulo, carta=nombre, precio=round(precio),
                referencia=usd * dolar, edicion=ed, foil=foil, idioma=detectar_idioma(texto),
                url=p["permalink"], clave=f"{base}|{p['id']}", edicion_confirmada=ok,
                condicion=cond, ref_mas_barata=mas_barata(imps, foil, cond) * dolar))
        time.sleep(1.5)
    log(f"  ✓ {tienda['nombre']}: {vistos} singles revisados")
    CONTEO["singles"] += vistos
    CONTEO["tiendas"] += 1
    return ofertas


CONTEO = {"singles": 0, "tiendas": 0}

LECTORES = {"shopify": leer_shopify, "woocommerce": leer_woocommerce}


# ---------------------------------------------------------------- avisos
def formatear(o: Oferta) -> str:
    extra = " ✨Foil" if o.foil else ""
    ed = o.edicion if o.edicion_confirmada else f"{o.edicion}? (ref. más barata)"
    segura = o.ref_mas_barata and o.precio <= o.ref_mas_barata * 0.6
    sello = "✅ segura" if segura else f"⚠️ la edición más barata vale {clp(o.ref_mas_barata)}"
    return (f"🃏 {o.carta}{extra} [{ed}] {o.idioma} {o.condicion}\n"
            f"   {o.tienda}: {clp(o.precio)} vs ref {clp(o.referencia)} "
            f"(-{o.descuento:.0%}) {sello}\n   {o.url}")


def enviar_whatsapp(texto: str):
    tel, key = os.getenv("CALLMEBOT_PHONE"), os.getenv("CALLMEBOT_APIKEY")
    if not (tel and key):
        log("WhatsApp no configurado (faltan CALLMEBOT_PHONE / CALLMEBOT_APIKEY)")
        return
    url = f"https://api.callmebot.com/whatsapp.php?phone={quote(tel)}&text={quote(texto)}&apikey={quote(key)}"
    r = SESION.get(url, timeout=30)
    log(f"WhatsApp: HTTP {r.status_code}")


def enviar_correo(asunto: str, cuerpo: str):
    user, pwd = os.getenv("GMAIL_USER"), os.getenv("GMAIL_APP_PASSWORD")
    para = os.getenv("EMAIL_TO") or user
    if not (user and pwd):
        log("Correo no configurado (faltan GMAIL_USER / GMAIL_APP_PASSWORD)")
        return
    msg = MIMEText(cuerpo, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = asunto, user, para
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pwd)
        s.send_message(msg)
    log(f"Correo enviado a {para}")


def avisar(nuevas: list[Oferta], cfg: dict, dolar: float):
    nuevas.sort(key=lambda o: (-o.descuento, -o.referencia))
    cuerpo = "\n\n".join(formatear(o) for o in nuevas)
    pie = (f"\n\nReferencia: Card Kingdom (según condición) × dólar {clp(dolar)}. "
           "Revisa condición e idioma antes de comprar.")
    n = cfg.get("max_por_whatsapp", 10)
    wa = f"🔥 {len(nuevas)} ofertas MTG\n\n" + "\n\n".join(formatear(o) for o in nuevas[:n])
    if len(nuevas) > n:
        wa += f"\n\n…y {len(nuevas) - n} más en tu correo."
    enviar_whatsapp(wa[:3500])
    enviar_correo(f"🔥 {len(nuevas)} ofertas de Magic encontradas", cuerpo + pie)


# ---------------------------------------------------------------- estado
def cargar_estado() -> dict:
    return json.loads(ESTADO.read_text()) if ESTADO.exists() else {}


def guardar_estado(estado: dict, ofertas_vigentes: set[str]):
    # Olvida las que ya no están en oferta, para volver a avisar si reaparecen
    estado = {k: v for k, v in estado.items() if k in ofertas_vigentes}
    ESTADO.parent.mkdir(exist_ok=True)
    ESTADO.write_text(json.dumps(estado, indent=1, ensure_ascii=False, sort_keys=True))



# ---------------------------------------------------------------- página web
PAGINA = RAIZ / "docs"

PLANTILLA = """<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ofertas MTG en tiendas chilenas</title>
<style>
 body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#0f1115;color:#e8e8e8}
 main{max-width:1200px;margin:auto;padding:20px}
 h1{font-size:1.5rem;margin:0 0 4px} .sub{color:#9aa0a6;font-size:.85rem;margin:0 0 16px}
 .tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:16px}
 .tile{background:#181b22;border:1px solid #2a2f3a;border-radius:10px;padding:12px}
 .tile b{display:block;font-size:1.6rem} .tile span{color:#9aa0a6;font-size:.8rem}
 .ctl{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin-bottom:10px;font-size:.85rem;color:#9aa0a6}
 select{background:#181b22;color:#e8e8e8;border:1px solid #2a2f3a;border-radius:6px;padding:4px}
 .wrap{overflow-x:auto;border:1px solid #2a2f3a;border-radius:10px}
 table{border-collapse:collapse;width:100%;font-size:.82rem}
 th,td{padding:6px 10px;border-bottom:1px solid #2a2f3a;white-space:nowrap;text-align:left}
 th{background:#181b22;color:#9aa0a6;position:sticky;top:0} td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
 a{color:#8ab4f8} .ok{color:#5bd68a;font-weight:600} .warn{color:#e0b350}
 details{margin-top:16px;color:#9aa0a6;font-size:.82rem}
</style></head><body><main>
<h1>Ofertas de Magic en tiendas chilenas</h1>
<p class="sub">Actualizado: <b id="fecha"></b> (se actualiza solo cada 3 horas). Referencia: Card Kingdom en la misma edición, condición y acabado, con dólar CK de <b id="dolar"></b>. Solo aparecen cartas a <b id="umbral"></b> o más bajo esa referencia.</p>
<div class="tiles">
 <div class="tile"><b id="t-seg">0</b><span>Ofertas seguras</span></div>
 <div class="tile"><b id="t-rev">0</b><span>Para revisar a mano</span></div>
 <div class="tile"><b id="t-tiendas">0</b><span>Tiendas revisadas</span></div>
 <div class="tile"><b id="t-singles">0</b><span>Singles revisados</span></div>
</div>
<div class="ctl">
 <label><input type="checkbox" id="f-seg"> Solo seguras</label>
 <label>Tienda <select id="f-tienda"><option value="">Todas</option></select></label>
 <label>Condición <select id="f-cond"><option value="">Todas</option><option value="NMLP">NM y LP</option><option value="NM">Solo NM</option></select></label>
</div>
<div class="wrap"><table><thead><tr>
 <th>Carta</th><th>Tienda</th><th>Edición</th><th>Acabado</th><th>Idioma</th><th>Cond.</th>
 <th class="n">Precio</th><th class="n">CK (US$)</th><th class="n">$ por dólar CK</th><th class="n">CK más barata (US$)</th><th class="n">Desc.</th><th>Segura</th>
</tr></thead><tbody id="filas"></tbody></table></div>
<p class="sub" id="conteo"></p>
<details><summary>Cómo leer esta lista</summary><ul>
 <li><b>Segura</b>: la carta está bajo el umbral incluso contra la edición más barata que existe de ella en CK.</li>
 <li><b>Para revisar</b>: solo es barata contra la edición que indica la tienda. Muchas veces es un foil promocional mal clasificado: revisa la foto.</li>
 <li>Si la tienda no dice si la carta es foil, se compara contra la versión más barata (foil o normal).</li>
 <li>CK vende cartas en inglés: en otros idiomas suelen valer menos.</li>
</ul></details>
</main>
<script>
const D = __DATOS__;
const clp = n => n == null ? "—" : "$" + Math.round(n).toLocaleString("es-CL");
const $ = id => document.getElementById(id);
$("fecha").textContent = D.fecha; $("dolar").textContent = clp(D.dolar); $("umbral").textContent = D.umbral + "%";
$("t-seg").textContent = D.ofertas.filter(o => o.segura).length;
$("t-rev").textContent = D.ofertas.filter(o => !o.segura).length;
$("t-tiendas").textContent = D.tiendas; $("t-singles").textContent = D.singles.toLocaleString("es-CL");
[...new Set(D.ofertas.map(o => o.tienda))].sort().forEach(t => $("f-tienda").add(new Option(t, t)));
function pintar() {
  const seg = $("f-seg").checked, ti = $("f-tienda").value, co = $("f-cond").value;
  const rows = D.ofertas.filter(o => (!seg || o.segura) && (!ti || o.tienda === ti) &&
    (!co || (co === "NM" ? o.condicion === "NM" : ["NM", "LP"].includes(o.condicion))));
  const tb = $("filas"); tb.replaceChildren();
  for (const o of rows) {
    const tr = document.createElement("tr");
    const td = (t, c) => { const x = document.createElement("td"); x.textContent = t; if (c) x.className = c; tr.append(x); };
    const c = document.createElement("td"); const a = document.createElement("a");
    a.href = /^https:\\/\\//.test(o.url) ? o.url : "#"; a.target = "_blank"; a.rel = "noopener"; a.textContent = o.carta; c.append(a); tr.append(c);
    td(o.tienda); td(o.edicion); td(o.acabado); td(o.idioma); td(o.condicion);
    td(clp(o.precio), "n"); td(o.ck_usd.toFixed(2), "n"); td(clp(o.pesos_por_dolar), "n"); td(o.ck_min_usd.toFixed(2), "n");
    td(o.descuento + "%", "n"); td(o.segura ? "✅ Sí" : "⚠️ Revisar", o.segura ? "ok" : "warn");
    tb.append(tr);
  }
  $("conteo").textContent = rows.length ? `Mostrando ${rows.length} de ${D.ofertas.length}.` : "No hay ofertas con estos filtros.";
}
["f-seg", "f-tienda", "f-cond"].forEach(id => $(id).addEventListener("change", pintar)); pintar();
</script></body></html>
"""


def generar_pagina(ofertas: list[Oferta], dolar: float, cfg: dict, tiendas: int, singles: int):
    """Escribe docs/index.html y docs/ofertas.json con TODAS las ofertas vigentes."""
    from zoneinfo import ZoneInfo
    umbral = cfg.get("descuento_minimo", 0.40)
    filas = []
    for o in sorted(ofertas, key=lambda o: (o.precio > o.ref_mas_barata * (1 - umbral), -o.descuento)):
        ck = o.referencia / dolar
        filas.append({
            "tienda": o.tienda, "carta": o.carta, "edicion": o.edicion,
            "acabado": {True: "Foil", False: "Normal", None: "Sin dato"}[o.foil],
            "idioma": o.idioma, "condicion": o.condicion, "precio": o.precio,
            "ck_usd": round(ck, 2), "ck_min_usd": round(o.ref_mas_barata / dolar, 2),
            "pesos_por_dolar": round(o.precio / ck) if ck else None,
            "descuento": round(o.descuento * 100),
            "segura": o.precio <= o.ref_mas_barata * (1 - umbral), "url": o.url,
        })
    datos = {
        "fecha": datetime.now(ZoneInfo("America/Santiago")).strftime("%d-%m-%Y %H:%M"),
        "dolar": dolar, "umbral": round(umbral * 100), "tiendas": tiendas, "singles": singles,
        "ofertas": filas,
    }
    PAGINA.mkdir(exist_ok=True)
    js = json.dumps(datos, ensure_ascii=False).replace("</", "<\\/")
    (PAGINA / "index.html").write_text(PLANTILLA.replace("__DATOS__", js), encoding="utf-8")
    (PAGINA / "ofertas.json").write_text(json.dumps(datos, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"Página generada: {len(filas)} ofertas en docs/index.html")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sin-avisos", action="store_true")
    ap.add_argument("--probar-avisos", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load((RAIZ / "config.yaml").read_text(encoding="utf-8"))

    if args.probar_avisos:
        enviar_whatsapp("✅ Prueba: el buscador de ofertas MTG está conectado.")
        enviar_correo("✅ Prueba buscador MTG", "Si lees esto, los avisos por correo funcionan.")
        return

    dolar = dolar_del_dia(cfg)
    log(f"Dólar usado: {clp(dolar)}")
    refs = cargar_referencias(descargar_ck())

    todas: list[Oferta] = []
    for t in cfg.get("tiendas", []):
        lector = LECTORES.get(t.get("plataforma", "").lower())
        if not lector:
            log(f"  ? {t['nombre']}: plataforma '{t.get('plataforma')}' no soportada")
            continue
        try:
            todas += lector(t, cfg, refs, dolar)
        except Exception as e:  # noqa: BLE001
            log(f"  ⚠️ {t['nombre']}: error {e}")

    umbral = cfg.get("descuento_minimo", 0.40)
    minimo = cfg.get("referencia_minima_clp", 5000)
    ofertas = [o for o in todas if o.referencia >= minimo and o.descuento >= umbral]
    if cfg.get("solo_ofertas_seguras"):
        ofertas = [o for o in ofertas if o.precio <= o.ref_mas_barata * (1 - umbral)]
    log(f"\n{len(ofertas)} ofertas sobre el {umbral:.0%} de descuento")

    generar_pagina(ofertas, dolar, cfg, CONTEO["tiendas"], CONTEO["singles"])

    estado = cargar_estado()
    rebaja = cfg.get("rebaja_para_reavisar", 0.05)
    nuevas = [o for o in ofertas
              if o.clave not in estado or o.precio <= estado[o.clave] * (1 - rebaja)]
    for o in sorted(ofertas, key=lambda o: -o.descuento):
        log(formatear(o))

    if nuevas and not args.sin_avisos:
        avisar(nuevas, cfg, dolar)
        for o in nuevas:
            estado[o.clave] = o.precio
    elif not nuevas:
        log("Nada nuevo que avisar.")
    if not args.sin_avisos:
        guardar_estado(estado, {o.clave for o in ofertas})


if __name__ == "__main__":
    sys.exit(main())
