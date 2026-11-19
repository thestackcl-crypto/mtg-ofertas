# 🔥 Buscador de ofertas MTG (Chile)

Revisa cada 3 horas el stock de tiendas chilenas y te avisa **por WhatsApp y correo** cuando una carta está **40% o más bajo el precio de Card Kingdom para su edición y condición** (USD × "dólar CK", por defecto $800).

Corre gratis en GitHub Actions: no necesitas tener el PC prendido.

---

## Cómo funciona

1. Descarga una vez al día la **lista de precios pública de Card Kingdom**, que trae un precio para cada condición: NM, EX (≈LP), VG (≈MP) y G (≈HP/DMG).
2. Lee el catálogo público de cada tienda de `config.yaml`, solo si el `robots.txt` de la tienda lo permite.
3. Compara el precio de la tienda contra el precio de CK **en la misma condición** (una carta MP se compara con el precio VG de CK, no con el NM). Si la tienda indica la edición, usa esa edición. Si no, usa la **edición más barata**, para no darte falsas alarmas.
4. Solo te avisa de las ofertas **nuevas**. Si una carta ya avisada baja otro 5%, te vuelve a avisar.

---

## Instalación (unos 15 minutos)

### 1. Crea el repositorio en GitHub
1. Crea una cuenta en https://github.com, si no tienes una.
2. Haz clic en **New repository**, ponle de nombre `mtg-ofertas` y márcalo **Private**.
3. Haz clic en **Add file → Upload files** y arrastra **todo el contenido** de esta carpeta, incluida la carpeta `.github`.
   > Si la carpeta `.github` no se sube al arrastrarla, créala a mano: **Add file → Create new file** y escribe el nombre `.github/workflows/ofertas.yml`. Después pega el contenido del archivo.

### 2. WhatsApp (CallMeBot, gratis)
1. Agrega a tus contactos el número que aparece en https://www.callmebot.com/blog/free-api-whatsapp-messages/.
2. Envíale por WhatsApp este mensaje: `I allow callmebot to send me messages`
3. Te va a responder con tu **apikey**.

### 3. Correo (Gmail)
1. Activa la verificación en 2 pasos en tu cuenta Google.
2. Entra a https://myaccount.google.com/apppasswords y crea una "contraseña de aplicación". Son 16 letras.

### 4. Guarda las claves en GitHub
En tu repo, entra a **Settings → Secrets and variables → Actions → New repository secret** y crea estos secretos:

| Nombre | Valor |
|---|---|
| `CALLMEBOT_PHONE` | Tu número con código país, ej: `+56912345678` |
| `CALLMEBOT_APIKEY` | La apikey que te mandó CallMeBot |
| `GMAIL_USER` | Tu Gmail |
| `GMAIL_APP_PASSWORD` | La contraseña de aplicación de 16 letras |
| `EMAIL_TO` | (opcional) Otro correo donde recibir los avisos |

### 5. Prueba
1. Entra a la pestaña **Actions → Buscar ofertas MTG → Run workflow**.
2. Marca la casilla "Solo enviar mensaje de prueba" y ejecútalo. Te debería llegar un WhatsApp y un correo.
3. Ejecútalo otra vez **sin** marcar la casilla para hacer la primera búsqueda real. Desde ahí corre solo cada 3 horas.

### 6. Página web con las ofertas (se actualiza sola)
Cada vez que corre la búsqueda (cada 3 horas), el sistema publica una página con **todas** las ofertas vigentes, con filtros y links a cada carta.

1. En tu repo entra a **Settings → Pages**.
2. En **Source**, elige **GitHub Actions**.
3. Corre una búsqueda (**Actions → Run workflow**, sin marcar la prueba).
4. Tu página queda en `https://TU-USUARIO.github.io/mtg-ofertas/`. Guárdala en favoritos del celular.

> GitHub Pages es gratis para repositorios **públicos**. Para un repositorio privado necesitas GitHub Pro (US$4 al mes). Hacerlo público es seguro: tus claves de WhatsApp y Gmail quedan en *Secrets*, que nunca se muestran. Lo único visible es el código, `config.yaml` y la lista de ofertas.

---

## Personalizar (`config.yaml`)

- `descuento_minimo: 0.40`: es el porcentaje de descuento que gatilla un aviso.
- `referencia_minima_clp: 5000`: ignora cartas que valen poco.
- `dolar_ck: 800`: cuántos pesos vale cada dólar de CK. Las tiendas chilenas usan entre 700 y 850. Con 800 y 40% de descuento, te avisa cuando una carta cuesta $480 o menos por cada dólar de CK. Con `auto` usa el dólar observado del día.
- `tiendas:`: agrega tiendas **Shopify** copiando el bloque de ejemplo. Para saber si una tienda usa Shopify, abre `https://LATIENDA.cl/products.json` en el navegador. Si ves texto con `"products"`, sí la usa.
- Para cambiar la frecuencia, edita la línea `cron` en `.github/workflows/ofertas.yml`.

## Ejemplo de aviso

```
🃏 Doubling Season [CMM] EN?
   Piedra Bruja: $12.000 vs ref $27.000 (-56%)
   https://piedrabruja.cl/products/...
```

`EN?` significa que el título no indica el idioma. Antes de comprar, revisa siempre la **condición, el idioma y la edición** en la página de la tienda.

## Limitaciones honestas

- **scry.cl no se revisa automáticamente**: el sitio pide explícitamente que no se le haga scraping. Úsalo a mano para confirmar una oferta.
- Viene configurado con **10 tiendas**: 7 Shopify (Paytowin, Game of Magic Singles, Collector Center, Singles Winterland, Card Souls, Piedra Bruja, Oasis Games) y 3 WooCommerce (Rhystic Bazaar, Card Nexus, OnPlay).
- No se pueden leer: Guildreams, Third Impact y Reino Eldrazi (las bloquea su `robots.txt`). Tampoco las que usan Jumpseller o un sitio propio sin catálogo público (Chronomagic, Game Quest, Cat Lotus, La Fortaleza, Blood Moon, Dominio Arcano, Torneos TCG, Magic Sur). Cartonazo y Hunter Card no publican singles de Magic.
- Cada aviso dice **✅ segura** si la carta es barata incluso contra su edición más barata. Si dice **⚠️**, solo es barata contra la edición que indica la tienda (muchas veces es un promo). Con `solo_ofertas_seguras: true` en `config.yaml` te llegan solo las seguras.
- Card Kingdom es una tienda de EE.UU. Si quieres un criterio más estricto o más amplio, ajusta `dolar_ck` y `descuento_minimo`.
