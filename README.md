# Landing de captación — Constructora

Landing con formulario para captar potenciales clientes desde el link de la bio de Instagram.
Los leads se guardan en base de datos y se ven en un panel `/admin` (exportable a Excel/CSV).

## Qué pide el formulario
- Nombre, WhatsApp y Email (obligatorios)
- ¿Busca invertir en pozo o propiedad terminada?
- Tipo de propiedad
- Zona
- Comentario (opcional)

## Personalizar (nombre, textos, colores)
- **Nombre de la constructora y textos:** variables `EMPRESA` y `SUBTITULO` (arriba de `app.py`, o mejor como variables de entorno en Railway).
- **Colores:** editá las variables `--azul` y `--oro` en `templates/index.html`.
- **WhatsApp de la empresa** (botón en la pantalla de gracias): variable `WHATSAPP_EMPRESA` (solo números, ej: `5491155551234`).

## Variables de entorno (Railway)
| Variable | Para qué | Ejemplo |
|---|---|---|
| `EMPRESA` | Nombre que se muestra | `Constructora Martin` |
| `SUBTITULO` | Frase debajo del título | `Te asesoramos sin cargo` |
| `WHATSAPP_EMPRESA` | Botón de WhatsApp | `5491155551234` |
| `ADMIN_PASSWORD` | Clave del panel `/admin` | (poné una segura) |
| `SECRET_KEY` | Seguridad de sesión | (texto largo al azar) |
| `DATABASE_URL` | La crea Railway al agregar PostgreSQL | (automática) |

## Probar en tu compu
```bash
pip install -r requirements.txt
python app.py
```
Abrí http://localhost:5000 (formulario) y http://localhost:5000/admin (panel).
En local usa un archivo SQLite (`leads.db`), no necesitás PostgreSQL.

## Subir a Railway
1. Subí esta carpeta a un repo de GitHub.
2. En Railway: **New Project → Deploy from GitHub repo**.
3. Agregá **New → Database → PostgreSQL** (Railway crea `DATABASE_URL` solo).
4. En **Variables**, cargá `EMPRESA`, `ADMIN_PASSWORD`, `SECRET_KEY`, etc.
5. Railway te da la URL pública → esa va en la bio de Instagram.

## Rutas
- `/` — formulario público (el link de la bio)
- `/admin` — panel de leads (pide contraseña)
- `/exportar` — descarga todos los leads en CSV
