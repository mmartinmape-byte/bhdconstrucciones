import os
import csv
import io
import json
import socket
import smtplib
import urllib.request
import urllib.parse
from contextlib import contextmanager
from email.message import EmailMessage
from datetime import datetime, date, timedelta
from functools import wraps


@contextmanager
def forzar_ipv4():
    """Fuerza las conexiones a usar IPv4 (Railway no tiene ruta IPv6 para SMTP)."""
    original = socket.getaddrinfo

    def solo_ipv4(host, port, family=0, type=0, proto=0, flags=0):
        return original(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = solo_ipv4
    try:
        yield
    finally:
        socket.getaddrinfo = original

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, Response, flash, abort
)
from sqlalchemy import (
    create_engine, Column, Integer, String, Text, DateTime, Date,
    Boolean, LargeBinary, ForeignKey, or_
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

try:
    from PIL import Image  # para redimensionar/comprimir las fotos antes de guardarlas
except Exception:
    Image = None

# ---------------------------------------------------------------------------
# CONFIGURACION - Editá estos valores para tu constructora
# ---------------------------------------------------------------------------
EMPRESA = os.environ.get("EMPRESA", "BHD CONSTRUCCIONES")
SUBTITULO = os.environ.get(
    "SUBTITULO",
    "Dejanos tus datos y te asesoramos sin cargo sobre la propiedad ideal para vos."
)
WHATSAPP_EMPRESA = os.environ.get("WHATSAPP_EMPRESA", "5491169616870")  # solo numeros, con codigo de pais

# --- Aviso por email cuando entra un lead ---
# Railway bloquea SMTP, asi que usamos Resend (API por HTTPS). Cargar RESEND_API_KEY en Railway.
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "mmartinmape@gmail.com")  # a donde llega el aviso
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")  # clave de resend.com
EMAIL_FROM = os.environ.get("EMAIL_FROM", "BHD CONSTRUCCIONES <onboarding@resend.dev>")
# Aviso por WhatsApp (CallMeBot). Cargar CALLMEBOT_APIKEY en Railway.
CALLMEBOT_APIKEY = os.environ.get("CALLMEBOT_APIKEY", "")
CALLMEBOT_PHONE = os.environ.get("CALLMEBOT_PHONE", WHATSAPP_EMPRESA)  # a que numero llega el aviso
# SMTP (solo para uso local; en Railway no funciona por bloqueo de puertos)
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")

# Texto de presentacion de la empresa
NOSOTROS = os.environ.get(
    "NOSOTROS",
    "Desde 2004, en BHD CONSTRUCCIONES desarrollamos y construimos dúplex y "
    "departamentos en Vicente López y Tigre. Con más de 20 proyectos entregados, "
    "combinamos diseño, calidad constructiva y cumplimiento en los plazos para que "
    "confíes tu inversión con tranquilidad."
)

# Numeros que se muestran en la seccion de presentacion
STATS = [
    {"numero": "2004", "texto": "año de fundación"},
    {"numero": "+20", "texto": "proyectos entregados"},
    {"numero": "2", "texto": "zonas: Vicente López y Tigre"},
]

# Proyectos que se muestran en la web.
# Para poner una foto real: guardala en la carpeta static/img/ y poné el nombre en "imagen".
# Si el archivo no existe, se muestra un fondo de color automáticamente.
PROYECTOS = [
    {
        "nombre": "Dúplex en Tigre Centro",
        "ubicacion": "Tigre Centro",
        "estado": "Terminado",
        "descripcion": "Dúplex de 2 ambientes con cochera en planta baja. "
                       "Primer piso: cocina-comedor, toilet y balcón. Segundo piso: "
                       "dormitorio con escritorio, baño en suite y balcón. "
                       "Con una hermosa vista al verde. Listo para habitar.",
        "imagen": "img/tigre.png",
    },
    {
        "nombre": "Edificio Munro",
        "ubicacion": "Munro, Vicente López",
        "estado": "En construcción",
        "descripcion": "5 departamentos de 2 dormitorios y 2 baños, de 66 m² cubiertos "
                       "+ 6 m² semicubiertos, con una cochera por unidad, frente al "
                       "Olivos Rugby Club. La planta baja tiene jardín de 40 m² con "
                       "parrilla; los del segundo piso, terraza propia de 72 m². "
                       "Excelente oportunidad de inversión.",
        "imagen": "img/munro.png",
    },
]

# Etapas del seguimiento de prospectos (en orden)
ESTADOS = ["Nuevo", "Contactado", "Visita agendada", "Negociando", "Reservó", "Compró", "Descartado"]
ESTADOS_CERRADOS = {"Compró", "Descartado"}
# De dónde puede venir un prospecto cargado a mano (los de la web traen el ?origen= del link)
ORIGENES_MANUALES = ["Instagram", "Cartel de obra", "Referido", "Llamado", "Visita a obra",
                     "Portal inmobiliario", "Inmobiliaria", "Otro"]
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "cambia-esta-clave-secreta")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin1234")

# --- Base de datos (PostgreSQL en Railway, SQLite en local) -----------------
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///leads.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
Base = declarative_base()
SessionLocal = sessionmaker(bind=engine)


class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True)
    nombre = Column(String(120), nullable=False)
    whatsapp = Column(String(60), nullable=False)
    email = Column(String(120), nullable=False)
    busca = Column(String(60))          # "Pozo (preventa)" / "Propiedad terminada"
    tipo_propiedad = Column(String(60)) # Departamento, Casa, etc.
    zona = Column(String(160))
    mensaje = Column(Text)
    creado = Column(DateTime, default=datetime.utcnow)
    # --- seguimiento ---
    origen = Column(String(80), default="web")         # instagram, cartel-pelliza, referido...
    proyecto = Column(String(120), default="")          # proyecto que le interesa
    unidad = Column(String(60), default="")             # unidad puntual (ej: 2°A)
    estado = Column(String(30), default="Nuevo")        # ver ESTADOS
    proximo_contacto = Column(Date)                     # cuándo volver a contactarlo
    carga = Column(String(20), default="web")           # web / manual

    notas = relationship(
        "LeadNota", back_populates="lead",
        cascade="all, delete-orphan", order_by="LeadNota.creado.desc()",
    )


class LeadNota(Base):
    __tablename__ = "lead_notas"

    id = Column(Integer, primary_key=True)
    lead_id = Column(Integer, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False)
    texto = Column(Text, nullable=False)
    creado = Column(DateTime, default=datetime.utcnow)

    lead = relationship("Lead", back_populates="notas")


class Inmueble(Base):
    __tablename__ = "inmuebles"

    id = Column(Integer, primary_key=True)
    titulo = Column(String(160), nullable=False)
    operacion = Column(String(30), default="Venta")     # Venta / Alquiler
    tipo = Column(String(40), default="Departamento")    # Departamento, Casa, PH, Lote, Local
    precio = Column(Integer, default=0)
    moneda = Column(String(10), default="USD")           # USD / ARS
    ubicacion = Column(String(160), default="")
    direccion_mapa = Column(String(250), default="")     # dirección exacta opcional para el mapa
    ambientes = Column(String(20), default="")
    dormitorios = Column(String(20), default="")
    banos = Column(String(20), default="")
    m2 = Column(String(30), default="")
    cochera = Column(Boolean, default=False)
    estado = Column(String(30), default="Disponible")    # Disponible / Reservado / Vendido
    descripcion = Column(Text, default="")
    destacado = Column(Boolean, default=False)
    publicado = Column(Boolean, default=True)
    creado = Column(DateTime, default=datetime.utcnow)

    fotos = relationship(
        "InmuebleFoto", back_populates="inmueble",
        cascade="all, delete-orphan", order_by="InmuebleFoto.orden",
    )


class InmuebleFoto(Base):
    __tablename__ = "inmueble_fotos"

    id = Column(Integer, primary_key=True)
    inmueble_id = Column(Integer, ForeignKey("inmuebles.id", ondelete="CASCADE"), nullable=False)
    datos = Column(LargeBinary, nullable=False)
    mimetype = Column(String(60), default="image/jpeg")
    orden = Column(Integer, default=0)

    inmueble = relationship("Inmueble", back_populates="fotos")


class Folleto(Base):
    """Folleto PDF de un proyecto, que se entrega a quien deja sus datos en /folleto/<slug>."""
    __tablename__ = "folletos"

    id = Column(Integer, primary_key=True)
    slug = Column(String(80), unique=True, nullable=False)   # parte del link: /folleto/<slug>
    proyecto = Column(String(120), nullable=False)            # nombre del proyecto (igual que en PROYECTOS)
    archivo = Column(String(160), default="folleto.pdf")      # nombre del archivo al descargar
    pdf = Column(LargeBinary, nullable=False)
    actualizado = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(engine)


def _migrar_columnas():
    """Agrega columnas nuevas a tablas ya existentes (create_all no las agrega)."""
    from sqlalchemy import inspect, text as _sql_text
    try:
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("inmuebles")}
        if "direccion_mapa" not in cols:
            with engine.begin() as conn:
                conn.execute(_sql_text(
                    "ALTER TABLE inmuebles ADD COLUMN direccion_mapa VARCHAR(250) DEFAULT ''"
                ))
    except Exception as e:
        print("Aviso migración inmuebles:", e)

    # Columnas de seguimiento en leads (cada una por separado: si una falla, las demás siguen)
    nuevas = [
        ("origen", "VARCHAR(80) DEFAULT 'web'"),
        ("proyecto", "VARCHAR(120) DEFAULT ''"),
        ("unidad", "VARCHAR(60) DEFAULT ''"),
        ("estado", "VARCHAR(30) DEFAULT 'Nuevo'"),
        ("proximo_contacto", "DATE"),
        ("carga", "VARCHAR(20) DEFAULT 'web'"),
    ]
    try:
        cols = {c["name"] for c in inspect(engine).get_columns("leads")}
    except Exception as e:
        print("Aviso migración leads:", e)
        return
    for nombre, tipo in nuevas:
        if nombre in cols:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(_sql_text(f"ALTER TABLE leads ADD COLUMN {nombre} {tipo}"))
        except Exception as e:
            print(f"Aviso migración leads.{nombre}:", e)


_migrar_columnas()

# Límite de subida (varias fotos por propiedad)
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024  # 40 MB por request


# --- Helpers ----------------------------------------------------------------
def hoy_ar():
    """Fecha de hoy en Argentina (UTC-3)."""
    return (datetime.utcnow() - timedelta(hours=3)).date()


def wa_num(numero):
    """Normaliza un WhatsApp argentino para wa.me (agrega 549 si falta)."""
    n = "".join(ch for ch in (numero or "") if ch.isdigit())
    if n.startswith("549") and len(n) == 13:
        return n
    if n.startswith("54") and len(n) == 12:
        return "549" + n[2:]
    n = n.lstrip("0")
    # "11 15 4444-5555": característica + 15 + número -> sacar el 15
    if len(n) == 12:
        for k in (2, 3, 4):
            if n[k:k + 2] == "15":
                n = n[:k] + n[k + 2:]
                break
    if len(n) == 10 and n.startswith("15"):   # "15 4444 5555" sin característica -> AMBA (11)
        n = "11" + n[2:]
    if len(n) == 10:
        return "549" + n
    return n


app.jinja_env.filters["wa_num"] = wa_num


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def _armar_email(d):
    """Devuelve (asunto, html, texto) del aviso de lead."""
    wa = d["whatsapp"].replace(" ", "").replace("+", "")
    asunto = f"🏗️ Nuevo lead: {d['nombre']}"
    texto = (
        f"Nuevo contacto desde la web de {EMPRESA}\n\n"
        f"Nombre: {d['nombre']}\n"
        f"WhatsApp: {d['whatsapp']}\n"
        f"Email: {d['email']}\n"
        f"Busca: {d['busca']}\n"
        f"Tipo de propiedad: {d['tipo_propiedad']}\n"
        f"Zona: {d['zona']}\n"
        f"Proyecto: {d.get('proyecto') or '-'}\n"
        f"Origen: {d.get('origen') or 'web'}\n"
        f"Comentario: {d['mensaje']}\n"
    )
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:520px;margin:auto;border:1px solid #e6eaee;border-radius:12px;overflow:hidden">
      <div style="background:#111418;color:#fff;padding:16px 20px;font-size:17px;font-weight:700">
        Nuevo lead — <span style="color:#29b6e8">{EMPRESA}</span>
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:15px;color:#1c2733">
        <tr><td style="padding:10px 20px;color:#8a94a0;width:150px">Nombre</td><td style="padding:10px 20px;font-weight:600">{d['nombre']}</td></tr>
        <tr><td style="padding:10px 20px;color:#8a94a0">WhatsApp</td><td style="padding:10px 20px"><a href="https://wa.me/{wa}" style="color:#128C7E;font-weight:600">{d['whatsapp']}</a></td></tr>
        <tr><td style="padding:10px 20px;color:#8a94a0">Email</td><td style="padding:10px 20px"><a href="mailto:{d['email']}" style="color:#29b6e8">{d['email']}</a></td></tr>
        <tr><td style="padding:10px 20px;color:#8a94a0">Busca</td><td style="padding:10px 20px">{d['busca']}</td></tr>
        <tr><td style="padding:10px 20px;color:#8a94a0">Tipo</td><td style="padding:10px 20px">{d['tipo_propiedad']}</td></tr>
        <tr><td style="padding:10px 20px;color:#8a94a0">Zona</td><td style="padding:10px 20px">{d['zona']}</td></tr>
        <tr><td style="padding:10px 20px;color:#8a94a0">Proyecto</td><td style="padding:10px 20px">{d.get('proyecto') or '-'}</td></tr>
        <tr><td style="padding:10px 20px;color:#8a94a0">Origen</td><td style="padding:10px 20px">{d.get('origen') or 'web'}</td></tr>
        <tr><td style="padding:10px 20px;color:#8a94a0">Comentario</td><td style="padding:10px 20px">{d['mensaje']}</td></tr>
      </table>
      <div style="padding:16px 20px">
        <a href="https://wa.me/{wa}" style="display:inline-block;background:#25D366;color:#fff;text-decoration:none;padding:11px 20px;border-radius:8px;font-weight:700">Responder por WhatsApp</a>
      </div>
    </div>
    """
    return asunto, html, texto


def _enviar_resend(asunto, html, texto, reply_to=None):
    payload = {
        "from": EMAIL_FROM,
        "to": [NOTIFY_EMAIL],
        "subject": asunto,
        "html": html,
        "text": texto,
    }
    if reply_to:
        payload["reply_to"] = reply_to
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8")


def _enviar_smtp(asunto, html, texto, reply_to=None):
    msg = EmailMessage()
    msg["Subject"] = asunto
    msg["From"] = SMTP_USER
    msg["To"] = NOTIFY_EMAIL
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(texto)
    msg.add_alternative(html, subtype="html")
    with forzar_ipv4():
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)


def enviar_notificacion(d):
    """Avisa por email de un lead nuevo. Usa Resend (HTTPS); SMTP solo de respaldo local."""
    asunto, html, texto = _armar_email(d)
    reply_to = d.get("email") or None
    if RESEND_API_KEY:
        _enviar_resend(asunto, html, texto, reply_to)
    elif SMTP_USER and SMTP_PASS:
        _enviar_smtp(asunto, html, texto, reply_to)


def enviar_whatsapp(d):
    """Avisa por WhatsApp de un lead nuevo usando CallMeBot (HTTPS)."""
    if not CALLMEBOT_APIKEY:
        return
    texto = (
        f"🏗️ Nuevo lead - {EMPRESA}\n"
        f"Nombre: {d['nombre']}\n"
        f"WhatsApp: {d['whatsapp']}\n"
        f"Email: {d['email']}\n"
        f"Busca: {d['busca']} ({d['tipo_propiedad']})\n"
        f"Zona: {d['zona']}\n"
        f"Proyecto: {d.get('proyecto') or '-'} · Origen: {d.get('origen') or 'web'}\n"
        f"Comentario: {d['mensaje']}"
    )
    url = "https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode({
        "phone": CALLMEBOT_PHONE,
        "text": texto,
        "apikey": CALLMEBOT_APIKEY,
    })
    with urllib.request.urlopen(url, timeout=20) as resp:
        return resp.read().decode("utf-8", "ignore")


# --- Helpers de inmuebles ---------------------------------------------------
def _procesar_imagen(raw, mimetype):
    """Redimensiona y comprime una imagen para no guardar archivos enormes en la BD.
    Si Pillow no está disponible, guarda el archivo tal cual."""
    if not Image:
        return raw, (mimetype or "image/jpeg")
    try:
        im = Image.open(io.BytesIO(raw))
        im = im.convert("RGB")
        max_lado = 1600
        if max(im.size) > max_lado:
            im.thumbnail((max_lado, max_lado))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=82, optimize=True)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return raw, (mimetype or "image/jpeg")


def _guardar_fotos(db, inmueble, files):
    """Guarda las fotos subidas (lista de FileStorage) asociadas al inmueble."""
    orden = max([f.orden for f in inmueble.fotos], default=-1) + 1
    for f in files:
        if not f or not getattr(f, "filename", ""):
            continue
        raw = f.read()
        if not raw:
            continue
        datos, mt = _procesar_imagen(raw, f.mimetype)
        db.add(InmuebleFoto(inmueble_id=inmueble.id, datos=datos, mimetype=mt, orden=orden))
        orden += 1


def _inmueble_dict(inm):
    """Serializa un inmueble (con los ids de sus fotos) para el template."""
    return {
        "id": inm.id,
        "titulo": inm.titulo,
        "operacion": inm.operacion,
        "tipo": inm.tipo,
        "precio": inm.precio or 0,
        "moneda": inm.moneda,
        "ubicacion": inm.ubicacion,
        "direccion_mapa": inm.direccion_mapa or "",
        "mapa_query": (inm.direccion_mapa or inm.ubicacion or "").strip(),
        "ambientes": inm.ambientes,
        "dormitorios": inm.dormitorios,
        "banos": inm.banos,
        "m2": inm.m2,
        "cochera": inm.cochera,
        "estado": inm.estado,
        "descripcion": inm.descripcion,
        "destacado": inm.destacado,
        "publicado": inm.publicado,
        "fotos": [f.id for f in inm.fotos],
    }


def _leer_form_inmueble(form):
    """Toma los campos del formulario del admin y los normaliza."""
    def num(v):
        try:
            return int(float(str(v).replace(".", "").replace(",", "").strip() or 0))
        except (TypeError, ValueError):
            return 0
    return {
        "titulo": form.get("titulo", "").strip(),
        "operacion": form.get("operacion", "Venta").strip() or "Venta",
        "tipo": form.get("tipo", "Departamento").strip() or "Departamento",
        "precio": num(form.get("precio")),
        "moneda": form.get("moneda", "USD").strip() or "USD",
        "ubicacion": form.get("ubicacion", "").strip(),
        "direccion_mapa": form.get("direccion_mapa", "").strip(),
        "ambientes": form.get("ambientes", "").strip(),
        "dormitorios": form.get("dormitorios", "").strip(),
        "banos": form.get("banos", "").strip(),
        "m2": form.get("m2", "").strip(),
        "cochera": form.get("cochera") == "on",
        "estado": form.get("estado", "Disponible").strip() or "Disponible",
        "descripcion": form.get("descripcion", "").strip(),
        "destacado": form.get("destacado") == "on",
        "publicado": form.get("publicado") == "on",
    }


# --- Rutas publicas ---------------------------------------------------------
@app.route("/")
def index():
    # ?origen=cartel-pelliza  -> se guarda en la sesión (gana el primer origen con el que llegó)
    origen = (request.args.get("origen") or "").strip()[:80]
    if origen and not session.get("origen"):
        session["origen"] = origen
    proyecto_arg = (request.args.get("proyecto") or "").strip().lower()
    proyecto_sel = next((p["nombre"] for p in PROYECTOS
                         if proyecto_arg and (proyecto_arg in p["nombre"].lower()
                                              or proyecto_arg in p["ubicacion"].lower())), "")
    db = SessionLocal()
    try:
        inms = (db.query(Inmueble)
                  .filter(Inmueble.publicado == True)  # noqa: E712
                  .order_by(Inmueble.destacado.desc(), Inmueble.creado.desc())
                  .all())
        inmuebles = [_inmueble_dict(i) for i in inms]
    finally:
        db.close()
    return render_template(
        "index.html",
        empresa=EMPRESA,
        subtitulo=SUBTITULO,
        nosotros=NOSOTROS,
        stats=STATS,
        proyectos=PROYECTOS,
        inmuebles=inmuebles,
        whatsapp=WHATSAPP_EMPRESA,
        origen=session.get("origen", origen),
        proyecto_sel=proyecto_sel,
    )


@app.route("/foto/<int:foto_id>")
def foto(foto_id):
    db = SessionLocal()
    try:
        f = db.get(InmuebleFoto, foto_id)
        if not f:
            abort(404)
        datos, mt = f.datos, f.mimetype or "image/jpeg"
    finally:
        db.close()
    return Response(datos, mimetype=mt, headers={"Cache-Control": "public, max-age=86400"})


@app.route("/propiedad/<int:inm_id>")
def propiedad(inm_id):
    db = SessionLocal()
    try:
        inm = db.get(Inmueble, inm_id)
        if not inm or not inm.publicado:
            abort(404)
        data = _inmueble_dict(inm)
    finally:
        db.close()
    # link de WhatsApp con el nombre de la propiedad ya escrito
    wa_link = ""
    if WHATSAPP_EMPRESA:
        texto = (f"¡Hola {EMPRESA}! Me interesa la propiedad \"{data['titulo']}\" "
                 f"que vi en la web. ¿Me pasás más información?")
        wa_link = "https://wa.me/" + WHATSAPP_EMPRESA + "?text=" + urllib.parse.quote(texto)
    return render_template("propiedad.html", empresa=EMPRESA, inm=data,
                           whatsapp=WHATSAPP_EMPRESA, wa_link=wa_link)


@app.route("/enviar", methods=["POST"])
def enviar():
    db = SessionLocal()
    try:
        lead = Lead(
            nombre=request.form.get("nombre", "").strip(),
            whatsapp=request.form.get("whatsapp", "").strip(),
            email=request.form.get("email", "").strip(),
            busca=request.form.get("busca", "").strip(),
            tipo_propiedad=request.form.get("tipo_propiedad", "").strip(),
            zona=request.form.get("zona", "").strip(),
            mensaje=request.form.get("mensaje", "").strip(),
            proyecto=request.form.get("proyecto", "").strip()[:120],
            origen=(request.form.get("origen") or session.get("origen") or "web").strip()[:80],
            estado="Nuevo",
            carga="web",
        )
        if not lead.nombre or not lead.whatsapp or not lead.email:
            flash("Por favor completá nombre, WhatsApp y email.")
            return redirect(url_for("index"))
        db.add(lead)
        db.commit()
        datos = {
            "nombre": lead.nombre, "whatsapp": lead.whatsapp, "email": lead.email,
            "busca": lead.busca, "tipo_propiedad": lead.tipo_propiedad,
            "zona": lead.zona, "mensaje": lead.mensaje,
            "proyecto": lead.proyecto, "origen": lead.origen,
        }
    finally:
        db.close()

    # armar el mensaje de WhatsApp que el cliente enviara a la empresa
    partes = [
        f"¡Hola {EMPRESA}! Completé el formulario en la web y quiero más información.",
        "",
        f"Nombre: {datos['nombre']}",
        f"Busco: {datos['busca']} ({datos['tipo_propiedad']})",
    ]
    if datos["proyecto"]:
        partes.append(f"Proyecto: {datos['proyecto']}")
    if datos["zona"]:
        partes.append(f"Zona: {datos['zona']}")
    if datos["mensaje"]:
        partes.append(f"Comentario: {datos['mensaje']}")
    session["wa_texto"] = "\n".join(partes)

    # avisos internos opcionales (solo si estan configurados; no frenan la respuesta)
    try:
        enviar_notificacion(datos)
    except Exception as e:
        app.logger.warning("No se pudo enviar el email de aviso: %s", e)
    try:
        enviar_whatsapp(datos)
    except Exception as e:
        app.logger.warning("No se pudo enviar el WhatsApp de aviso: %s", e)

    return redirect(url_for("gracias"))


@app.route("/gracias")
def gracias():
    wa_texto = session.pop("wa_texto", "") or \
        f"¡Hola {EMPRESA}! Quiero más información sobre las propiedades."
    wa_link = ""
    if WHATSAPP_EMPRESA:
        wa_link = "https://wa.me/" + WHATSAPP_EMPRESA + "?text=" + urllib.parse.quote(wa_texto)
    return render_template("gracias.html", empresa=EMPRESA,
                           whatsapp=WHATSAPP_EMPRESA, wa_link=wa_link)


# --- Link de folleto: deja sus datos y recibe el PDF -------------------------
def slugify(txt):
    import unicodedata, re
    t = unicodedata.normalize("NFKD", txt or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:80]


def _proyecto_info(nombre):
    return next((p for p in PROYECTOS if p["nombre"] == nombre), None)


def _buscar_lead_por_whatsapp(db, numero):
    n = wa_num(numero)
    if not n:
        return None
    for l in db.query(Lead).order_by(Lead.creado.desc()).all():
        if wa_num(l.whatsapp) == n:
            return l
    return None


@app.route("/folleto/<slug>", methods=["GET", "POST"])
def folleto_form(slug):
    db = SessionLocal()
    try:
        f = db.query(Folleto).filter(Folleto.slug == slug).first()
        if not f:
            abort(404)
        proyecto = f.proyecto
        if request.method == "POST":
            nombre = request.form.get("nombre", "").strip()
            whatsapp = request.form.get("whatsapp", "").strip()
            email = request.form.get("email", "").strip()
            if not nombre or not whatsapp or not email:
                flash("Por favor completá nombre, WhatsApp y email.")
                return redirect(url_for("folleto_form", slug=slug, origen=request.form.get("origen", "")))
            origen = (request.form.get("origen") or session.get("origen") or "link-folleto").strip()[:80]
            busca = request.form.get("busca", "").strip()
            mensaje = request.form.get("mensaje", "").strip()
            lead = _buscar_lead_por_whatsapp(db, whatsapp)
            if lead:
                # ya lo teníamos: no duplicar, dejar constancia en su historial
                if not lead.proyecto:
                    lead.proyecto = proyecto
                if not lead.email and email:
                    lead.email = email
                db.add(LeadNota(lead_id=lead.id, texto=f"Volvió a pedir el folleto de {proyecto} (origen: {origen})"
                                + (f". Comentario: {mensaje}" if mensaje else "")))
                es_nuevo = False
            else:
                lead = Lead(nombre=nombre, whatsapp=whatsapp, email=email, busca=busca,
                            tipo_propiedad="Departamento", zona="", mensaje=mensaje,
                            proyecto=proyecto, origen=origen, estado="Nuevo", carga="web")
                db.add(lead)
                db.flush()
                db.add(LeadNota(lead_id=lead.id, texto=f"Descargó el folleto de {proyecto}"))
                es_nuevo = True
            db.commit()
            datos = {"nombre": nombre, "whatsapp": whatsapp, "email": email, "busca": busca,
                     "tipo_propiedad": "Departamento", "zona": "", "proyecto": proyecto, "origen": origen,
                     "mensaje": ("[Pidió el folleto] " if es_nuevo else "[Volvió a pedir el folleto] ") + mensaje}
            permitidos = set(session.get("folletos", []))
            permitidos.add(slug)
            session["folletos"] = sorted(permitidos)
            try:
                enviar_notificacion(datos)
            except Exception as e:
                app.logger.warning("No se pudo enviar el email de aviso: %s", e)
            try:
                enviar_whatsapp(datos)
            except Exception as e:
                app.logger.warning("No se pudo enviar el WhatsApp de aviso: %s", e)
            return redirect(url_for("folleto_gracias", slug=slug))
        info = _proyecto_info(proyecto) or {}
        return render_template("folleto_form.html", empresa=EMPRESA, slug=slug, proyecto=proyecto,
                               info=info, origen=request.args.get("origen", "")[:80])
    finally:
        db.close()


@app.route("/folleto/<slug>/listo")
def folleto_gracias(slug):
    if slug not in session.get("folletos", []):
        return redirect(url_for("folleto_form", slug=slug))
    db = SessionLocal()
    try:
        f = db.query(Folleto).filter(Folleto.slug == slug).first()
        if not f:
            abort(404)
        proyecto = f.proyecto
    finally:
        db.close()
    wa_link = ""
    if WHATSAPP_EMPRESA:
        texto = f"¡Hola {EMPRESA}! Descargué el folleto de {proyecto} y quiero más información."
        wa_link = "https://wa.me/" + WHATSAPP_EMPRESA + "?text=" + urllib.parse.quote(texto)
    return render_template("folleto_gracias.html", empresa=EMPRESA, slug=slug, proyecto=proyecto, wa_link=wa_link)


@app.route("/folleto/<slug>/pdf")
def folleto_pdf(slug):
    if slug not in session.get("folletos", []):
        return redirect(url_for("folleto_form", slug=slug))
    db = SessionLocal()
    try:
        f = db.query(Folleto).filter(Folleto.slug == slug).first()
        if not f:
            abort(404)
        datos, archivo = f.pdf, f.archivo or "folleto.pdf"
    finally:
        db.close()
    nombre_ascii = archivo.encode("ascii", "ignore").decode() or "folleto.pdf"
    return Response(datos, mimetype="application/pdf", headers={
        "Content-Disposition": f"inline; filename=\"{nombre_ascii}\"; filename*=UTF-8''{urllib.parse.quote(archivo)}",
    })


# --- Admin ------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("admin"))
        flash("Contraseña incorrecta.")
    return render_template("login.html", empresa=EMPRESA)


@app.route("/logout")
def logout():
    session.pop("admin", None)
    return redirect(url_for("login"))


def _proyectos_opciones(db):
    """Proyectos para elegir: los de la web + los que ya figuran en algún prospecto."""
    nombres = [p["nombre"] for p in PROYECTOS]
    for (pr,) in db.query(Lead.proyecto).distinct():
        if pr and pr not in nombres:
            nombres.append(pr)
    return nombres


def _leer_form_lead(form):
    pc = (form.get("proximo_contacto") or "").strip()
    try:
        prox = datetime.strptime(pc, "%Y-%m-%d").date() if pc else None
    except ValueError:
        prox = None
    estado = form.get("estado", "Nuevo")
    return {
        "nombre": form.get("nombre", "").strip(),
        "whatsapp": form.get("whatsapp", "").strip(),
        "email": form.get("email", "").strip(),
        "proyecto": form.get("proyecto", "").strip()[:120],
        "unidad": form.get("unidad", "").strip()[:60],
        "origen": (form.get("origen", "").strip() or "Otro")[:80],
        "estado": estado if estado in ESTADOS else "Nuevo",
        "proximo_contacto": prox,
        "busca": form.get("busca", "").strip(),
        "tipo_propiedad": form.get("tipo_propiedad", "").strip(),
        "zona": form.get("zona", "").strip(),
        "mensaje": form.get("mensaje", "").strip(),
    }


@app.route("/admin")
@login_required
def admin():
    hoy = hoy_ar()
    f_estado = request.args.get("estado", "")
    f_proyecto = request.args.get("proyecto", "")
    f_origen = request.args.get("origen", "")
    q = (request.args.get("q") or "").strip()
    db = SessionLocal()
    try:
        todos = db.query(Lead).all()
        # embudo: cantidad por estado (sobre todos los prospectos)
        embudo = {e: 0 for e in ESTADOS}
        for l in todos:
            embudo[l.estado or "Nuevo"] = embudo.get(l.estado or "Nuevo", 0) + 1

        # "para hoy": vencidos o de hoy + nuevos que nadie contactó todavía
        hoy_lista = [l for l in todos if (l.estado or "Nuevo") not in ESTADOS_CERRADOS and (
            (l.proximo_contacto and l.proximo_contacto <= hoy) or (l.estado or "Nuevo") == "Nuevo")]
        hoy_lista.sort(key=lambda l: (l.proximo_contacto or date.min, l.creado or datetime.min))

        qry = db.query(Lead)
        if f_estado:
            qry = qry.filter(or_(Lead.estado == f_estado, Lead.estado.is_(None)) if f_estado == "Nuevo"
                             else Lead.estado == f_estado)
        if f_proyecto:
            qry = qry.filter(Lead.proyecto == f_proyecto)
        if f_origen:
            qry = qry.filter(Lead.origen == f_origen)
        if q:
            like = f"%{q}%"
            qry = qry.filter(or_(Lead.nombre.ilike(like), Lead.whatsapp.ilike(like),
                                 Lead.email.ilike(like), Lead.mensaje.ilike(like)))
        leads = qry.order_by(Lead.creado.desc()).all()
        ultima_nota = {l.id: (l.notas[0].texto if l.notas else "") for l in leads + hoy_lista}
        origenes = sorted({l.origen for l in todos if l.origen})
        proyectos = _proyectos_opciones(db)
        return render_template(
            "admin.html", empresa=EMPRESA, leads=leads, hoy_lista=hoy_lista, hoy=hoy,
            embudo=embudo, estados=ESTADOS, origenes=origenes, proyectos=proyectos,
            ultima_nota=ultima_nota, total=len(todos),
            f_estado=f_estado, f_proyecto=f_proyecto, f_origen=f_origen, q=q,
        )
    finally:
        db.close()


@app.route("/admin/prospecto/nuevo", methods=["GET", "POST"])
@login_required
def prospecto_nuevo():
    db = SessionLocal()
    try:
        if request.method == "POST":
            d = _leer_form_lead(request.form)
            if not d["nombre"] or not d["whatsapp"]:
                flash("Nombre y WhatsApp son obligatorios.")
                return redirect(url_for("prospecto_nuevo"))
            lead = Lead(**d, carga="manual")
            db.add(lead)
            db.flush()
            nota = request.form.get("nota", "").strip()
            db.add(LeadNota(lead_id=lead.id, texto="Cargado a mano" + (f": {nota}" if nota else "")))
            db.commit()
            return redirect(url_for("prospecto", lead_id=lead.id))
        return render_template("prospecto.html", empresa=EMPRESA, l=None, estados=ESTADOS,
                               origenes=ORIGENES_MANUALES, proyectos=_proyectos_opciones(db),
                               hoy=hoy_ar(), wa_link="")
    finally:
        db.close()


@app.route("/admin/prospecto/<int:lead_id>", methods=["GET", "POST"])
@login_required
def prospecto(lead_id):
    db = SessionLocal()
    try:
        lead = db.get(Lead, lead_id)
        if not lead:
            abort(404)
        if request.method == "POST":
            d = _leer_form_lead(request.form)
            if not d["nombre"] or not d["whatsapp"]:
                flash("Nombre y WhatsApp son obligatorios.")
                return redirect(url_for("prospecto", lead_id=lead_id))
            antes = lead.estado or "Nuevo"
            for k, v in d.items():
                setattr(lead, k, v)
            if d["estado"] != antes:
                db.add(LeadNota(lead_id=lead.id, texto=f"Estado: {antes} → {d['estado']}"))
            db.commit()
            flash("Cambios guardados.")
            return redirect(url_for("prospecto", lead_id=lead_id))
        origenes = list(ORIGENES_MANUALES)
        if lead.origen and lead.origen not in origenes:
            origenes.insert(0, lead.origen)
        nombre = (lead.nombre or "").split(" ")[0]
        texto = f"Hola {nombre}! Te escribo de {EMPRESA.title()}"
        texto += f" por tu consulta sobre {lead.proyecto}." if lead.proyecto else " por tu consulta."
        wa_link = f"https://wa.me/{wa_num(lead.whatsapp)}?text=" + urllib.parse.quote(texto)
        return render_template("prospecto.html", empresa=EMPRESA, l=lead, notas=list(lead.notas),
                               estados=ESTADOS, origenes=origenes, proyectos=_proyectos_opciones(db),
                               hoy=hoy_ar(), wa_link=wa_link)
    finally:
        db.close()


@app.route("/admin/prospecto/<int:lead_id>/nota", methods=["POST"])
@login_required
def prospecto_nota(lead_id):
    """Registrar un contacto: nota + (opcional) nuevo estado y fecha del próximo contacto."""
    db = SessionLocal()
    try:
        lead = db.get(Lead, lead_id)
        if not lead:
            abort(404)
        texto = request.form.get("texto", "").strip()
        nuevo_estado = request.form.get("estado", "")
        antes = lead.estado or "Nuevo"
        partes = []
        if nuevo_estado in ESTADOS and nuevo_estado != antes:
            lead.estado = nuevo_estado
            partes.append(f"Estado: {antes} → {nuevo_estado}")
        elif antes == "Nuevo" and texto:
            lead.estado = "Contactado"   # si le escribí algo, ya no es "nuevo"
            partes.append("Estado: Nuevo → Contactado")
        pc = request.form.get("proximo_contacto", "").strip()
        dias = request.form.get("en_dias", "").strip()
        if dias.isdigit():
            lead.proximo_contacto = hoy_ar() + timedelta(days=int(dias))
        elif pc:
            try:
                lead.proximo_contacto = datetime.strptime(pc, "%Y-%m-%d").date()
            except ValueError:
                pass
        elif request.form.get("sin_fecha"):
            lead.proximo_contacto = None
        if texto:
            partes.insert(0, texto)
        if partes:
            db.add(LeadNota(lead_id=lead.id, texto=" · ".join(partes)))
        db.commit()
    finally:
        db.close()
    return redirect(request.form.get("volver") or url_for("prospecto", lead_id=lead_id))


@app.route("/admin/nota/<int:nota_id>/borrar", methods=["POST"])
@login_required
def nota_borrar(nota_id):
    db = SessionLocal()
    try:
        n = db.get(LeadNota, nota_id)
        lead_id = n.lead_id if n else None
        if n:
            db.delete(n)
            db.commit()
    finally:
        db.close()
    return redirect(url_for("prospecto", lead_id=lead_id) if lead_id else url_for("admin"))


@app.route("/borrar/<int:lead_id>", methods=["POST"])
@login_required
def borrar(lead_id):
    db = SessionLocal()
    try:
        lead = db.get(Lead, lead_id)
        if lead:
            db.delete(lead)
            db.commit()
    finally:
        db.close()
    return redirect(url_for("admin"))


@app.route("/exportar")
@login_required
def exportar():
    db = SessionLocal()
    try:
        leads = db.query(Lead).order_by(Lead.creado.desc()).all()
        filas = []
        for l in leads:
            filas.append([
                l.creado.strftime("%d/%m/%Y %H:%M") if l.creado else "", l.nombre, l.whatsapp, l.email,
                l.proyecto or "", l.unidad or "", l.origen or "", l.carga or "", l.estado or "Nuevo",
                l.proximo_contacto.strftime("%d/%m/%Y") if l.proximo_contacto else "",
                l.busca, l.tipo_propiedad, l.zona, l.mensaje,
                " | ".join(f"{n.creado.strftime('%d/%m/%Y') if n.creado else ''} {n.texto}" for n in l.notas),
            ])
    finally:
        db.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Fecha", "Nombre", "WhatsApp", "Email", "Proyecto", "Unidad", "Origen", "Carga",
                     "Estado", "Próximo contacto", "Busca", "Tipo de propiedad", "Zona", "Mensaje", "Notas"])
    writer.writerows(filas)

    csv_data = "\ufeff" + output.getvalue()  # BOM para que Excel abra bien los acentos
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=prospectos.csv"},
    )


# --- Admin: folletos --------------------------------------------------------
@app.route("/admin/folletos", methods=["GET", "POST"])
@login_required
def admin_folletos():
    db = SessionLocal()
    try:
        if request.method == "POST":
            archivo = request.files.get("pdf")
            proyecto = request.form.get("proyecto", "").strip()
            slug = slugify(request.form.get("slug", "") or proyecto)
            raw = archivo.read() if archivo and archivo.filename else b""
            existente = db.query(Folleto).filter(Folleto.slug == slug).first() if slug else None
            if not proyecto or not slug:
                flash("Elegí el proyecto.")
            elif not raw and not existente:
                flash("Subí el PDF del folleto.")
            elif raw and not raw.startswith(b"%PDF"):
                flash("El archivo no es un PDF.")
            else:
                f = existente or Folleto(slug=slug, proyecto=proyecto)
                f.proyecto = proyecto
                if raw:
                    f.pdf = raw
                    f.archivo = archivo.filename
                f.actualizado = datetime.utcnow()
                if not existente:
                    db.add(f)
                db.commit()
                flash(f"Folleto de {proyecto} guardado.")
            return redirect(url_for("admin_folletos"))
        folletos = db.query(Folleto).order_by(Folleto.proyecto).all()
        base = request.url_root.rstrip("/")
        if "localhost" not in base and "127.0.0.1" not in base:
            base = base.replace("http://", "https://", 1)  # Railway sirve por https detrás de un proxy
        filas = []
        for f in folletos:
            link = f"{base}/folleto/{f.slug}?origen=whatsapp"
            texto = (f"¡Hola! Te paso el link para descargar el folleto de {f.proyecto}. "
                     f"Completá tus datos y lo recibís al instante: {link}")
            descargas = db.query(LeadNota).filter(LeadNota.texto.like(f"%folleto de {f.proyecto}%")).count()
            filas.append({"id": f.id, "slug": f.slug, "proyecto": f.proyecto, "archivo": f.archivo,
                          "kb": len(f.pdf or b"") // 1024, "descargas": descargas,
                          "actualizado": f.actualizado, "link": link,
                          "wa_share": "https://wa.me/?text=" + urllib.parse.quote(texto)})
        return render_template("admin_folletos.html", empresa=EMPRESA, folletos=filas,
                               proyectos=[p["nombre"] for p in PROYECTOS])
    finally:
        db.close()


@app.route("/admin/folletos/<int:fid>/borrar", methods=["POST"])
@login_required
def admin_folleto_borrar(fid):
    db = SessionLocal()
    try:
        f = db.get(Folleto, fid)
        if f:
            db.delete(f)
            db.commit()
    finally:
        db.close()
    return redirect(url_for("admin_folletos"))


@app.route("/admin/folletos/<int:fid>/ver")
@login_required
def admin_folleto_ver(fid):
    db = SessionLocal()
    try:
        f = db.get(Folleto, fid)
        if not f:
            abort(404)
        datos = f.pdf
    finally:
        db.close()
    return Response(datos, mimetype="application/pdf")


# --- Admin: inmuebles -------------------------------------------------------
@app.route("/admin/inmuebles")
@login_required
def admin_inmuebles():
    db = SessionLocal()
    try:
        inms = db.query(Inmueble).order_by(Inmueble.creado.desc()).all()
        data = []
        for i in inms:
            d = _inmueble_dict(i)
            d["n_fotos"] = len(i.fotos)
            data.append(d)
    finally:
        db.close()
    return render_template("admin_inmuebles.html", inmuebles=data, empresa=EMPRESA)


@app.route("/admin/inmuebles/nuevo", methods=["GET", "POST"])
@login_required
def admin_inmueble_nuevo():
    if request.method == "POST":
        campos = _leer_form_inmueble(request.form)
        if not campos["titulo"]:
            flash("El título es obligatorio.")
            return redirect(url_for("admin_inmueble_nuevo"))
        db = SessionLocal()
        try:
            inm = Inmueble(**campos)
            db.add(inm)
            db.commit()
            db.refresh(inm)
            _guardar_fotos(db, inm, request.files.getlist("fotos"))
            db.commit()
        finally:
            db.close()
        return redirect(url_for("admin_inmuebles"))
    return render_template("inmueble_form.html", empresa=EMPRESA, inm=None, nuevo=True)


@app.route("/admin/inmuebles/<int:inm_id>/editar", methods=["GET", "POST"])
@login_required
def admin_inmueble_editar(inm_id):
    db = SessionLocal()
    try:
        inm = db.get(Inmueble, inm_id)
        if not inm:
            db.close()
            abort(404)
        if request.method == "POST":
            campos = _leer_form_inmueble(request.form)
            if not campos["titulo"]:
                flash("El título es obligatorio.")
                return redirect(url_for("admin_inmueble_editar", inm_id=inm_id))
            for k, v in campos.items():
                setattr(inm, k, v)
            _guardar_fotos(db, inm, request.files.getlist("fotos"))
            db.commit()
            return redirect(url_for("admin_inmuebles"))
        data = _inmueble_dict(inm)
    finally:
        db.close()
    return render_template("inmueble_form.html", empresa=EMPRESA, inm=data, nuevo=False)


@app.route("/admin/inmuebles/<int:inm_id>/borrar", methods=["POST"])
@login_required
def admin_inmueble_borrar(inm_id):
    db = SessionLocal()
    try:
        inm = db.get(Inmueble, inm_id)
        if inm:
            db.delete(inm)
            db.commit()
    finally:
        db.close()
    return redirect(url_for("admin_inmuebles"))


@app.route("/admin/inmuebles/<int:inm_id>/publicar", methods=["POST"])
@login_required
def admin_inmueble_publicar(inm_id):
    db = SessionLocal()
    try:
        inm = db.get(Inmueble, inm_id)
        if inm:
            inm.publicado = not inm.publicado
            db.commit()
    finally:
        db.close()
    return redirect(url_for("admin_inmuebles"))


@app.route("/admin/foto/<int:foto_id>/borrar", methods=["POST"])
@login_required
def admin_foto_borrar(foto_id):
    db = SessionLocal()
    try:
        f = db.get(InmuebleFoto, foto_id)
        inm_id = f.inmueble_id if f else None
        if f:
            db.delete(f)
            db.commit()
    finally:
        db.close()
    if inm_id:
        return redirect(url_for("admin_inmueble_editar", inm_id=inm_id))
    return redirect(url_for("admin_inmuebles"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
