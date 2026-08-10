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
from datetime import datetime
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
    session, Response, flash
)
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker

# ---------------------------------------------------------------------------
# CONFIGURACION - Editá estos valores para tu constructora
# ---------------------------------------------------------------------------
EMPRESA = os.environ.get("EMPRESA", "BHD CONSTRUCCIONES")
SUBTITULO = os.environ.get(
    "SUBTITULO",
    "Dejanos tus datos y te asesoramos sin cargo sobre la propiedad ideal para vos."
)
WHATSAPP_EMPRESA = os.environ.get("WHATSAPP_EMPRESA", "5491130757520")  # solo numeros, con codigo de pais

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
        "descripcion": "5 departamentos de 3 ambientes de aprox. 70 m² con cochera, "
                       "frente al Olivos Rugby Club. El del primer piso incluye patio "
                       "de 5 x 8,5 m; los del segundo piso, terraza propia de 70 m². "
                       "Excelente oportunidad de inversión.",
        "imagen": "img/munro.png",
    },
]
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


Base.metadata.create_all(engine)


# --- Helpers ----------------------------------------------------------------
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
        f"Comentario: {d['mensaje']}"
    )
    url = "https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode({
        "phone": CALLMEBOT_PHONE,
        "text": texto,
        "apikey": CALLMEBOT_APIKEY,
    })
    with urllib.request.urlopen(url, timeout=20) as resp:
        return resp.read().decode("utf-8", "ignore")


# --- Rutas publicas ---------------------------------------------------------
@app.route("/")
def index():
    return render_template(
        "index.html",
        empresa=EMPRESA,
        subtitulo=SUBTITULO,
        nosotros=NOSOTROS,
        stats=STATS,
        proyectos=PROYECTOS,
        whatsapp=WHATSAPP_EMPRESA,
    )


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


@app.route("/diag-email")
def diag_email():
    # Ruta temporal de diagnostico. Se elimina despues de resolver el email.
    if request.args.get("key") != "diag-bhd-2026":
        return "no autorizado", 403

    datos_prueba = {
        "nombre": "DIAGNÓSTICO", "whatsapp": "1130757520", "email": NOTIFY_EMAIL,
        "busca": "-", "tipo_propiedad": "-", "zona": "-",
        "mensaje": "Prueba de diagnóstico del aviso",
    }

    # Diagnostico de WhatsApp (CallMeBot)
    if request.args.get("canal") == "whatsapp":
        info = {
            "callmebot_apikey_cargada": bool(CALLMEBOT_APIKEY),
            "callmebot_phone": CALLMEBOT_PHONE,
        }
        if not CALLMEBOT_APIKEY:
            info["resultado"] = "Falta cargar CALLMEBOT_APIKEY en Railway"
            return info
        try:
            info["respuesta_callmebot"] = enviar_whatsapp(datos_prueba)
            info["resultado"] = "Mensaje enviado. Revisá tu WhatsApp."
        except Exception as e:
            info["error"] = repr(e)
        return info

    # Diagnostico de email (Resend)
    info = {
        "resend_api_key_cargada": bool(RESEND_API_KEY),
        "email_from": EMAIL_FROM,
        "notify_email": NOTIFY_EMAIL,
    }
    if not RESEND_API_KEY:
        info["resultado"] = "Falta cargar RESEND_API_KEY en Railway"
        return info
    try:
        asunto, html, texto = _armar_email({
            "nombre": "DIAGNÓSTICO", "whatsapp": "1130757520", "email": NOTIFY_EMAIL,
            "busca": "-", "tipo_propiedad": "-", "zona": "-",
            "mensaje": "Prueba de diagnóstico del envío de email",
        })
        respuesta = _enviar_resend(asunto, html, texto)
        info["envio"] = "OK"
        info["respuesta_resend"] = respuesta
        info["resultado"] = "Email enviado. Revisá la casilla."
    except Exception as e:
        cuerpo = ""
        if hasattr(e, "read"):
            try:
                cuerpo = e.read().decode("utf-8")
            except Exception:
                pass
        info["error"] = repr(e)
        if cuerpo:
            info["detalle"] = cuerpo
    return info


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


@app.route("/admin")
@login_required
def admin():
    db = SessionLocal()
    try:
        leads = db.query(Lead).order_by(Lead.creado.desc()).all()
    finally:
        db.close()
    return render_template("admin.html", leads=leads, empresa=EMPRESA)


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
    finally:
        db.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Fecha", "Nombre", "WhatsApp", "Email", "Busca",
                     "Tipo de propiedad", "Zona", "Mensaje"])
    for l in leads:
        fecha = l.creado.strftime("%d/%m/%Y %H:%M") if l.creado else ""
        writer.writerow([fecha, l.nombre, l.whatsapp, l.email, l.busca,
                         l.tipo_propiedad, l.zona, l.mensaje])

    csv_data = "﻿" + output.getvalue()  # BOM para que Excel abra bien los acentos
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=leads.csv"},
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
