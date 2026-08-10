import os
import csv
import io
import smtplib
from email.message import EmailMessage
from datetime import datetime
from functools import wraps

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
# Para que funcione hay que cargar SMTP_USER y SMTP_PASS en Railway (ver README).
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "mmartinmape@gmail.com")  # a donde llega el aviso
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")  # tu casilla que envia (ej: mmartinmape@gmail.com)
SMTP_PASS = os.environ.get("SMTP_PASS", "")  # contraseña de aplicacion de Gmail (16 caracteres)

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


def enviar_notificacion(d):
    """Manda un email avisando de un lead nuevo. Si no hay SMTP configurado, no hace nada."""
    if not SMTP_USER or not SMTP_PASS:
        return
    wa = d["whatsapp"].replace(" ", "").replace("+", "")
    msg = EmailMessage()
    msg["Subject"] = f"🏗️ Nuevo lead: {d['nombre']}"
    msg["From"] = SMTP_USER
    msg["To"] = NOTIFY_EMAIL
    if d.get("email"):
        msg["Reply-To"] = d["email"]

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
    msg.set_content(texto)
    msg.add_alternative(f"""
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
    """, subtype="html")

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


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

    # aviso por email (no debe frenar la respuesta al usuario si algo falla)
    try:
        enviar_notificacion(datos)
    except Exception as e:
        app.logger.warning("No se pudo enviar el email de aviso: %s", e)

    return redirect(url_for("gracias"))


@app.route("/gracias")
def gracias():
    return render_template("gracias.html", empresa=EMPRESA, whatsapp=WHATSAPP_EMPRESA)


@app.route("/diag-email")
def diag_email():
    # Ruta temporal de diagnostico. Se elimina despues de resolver el email.
    if request.args.get("key") != "diag-bhd-2026":
        return "no autorizado", 403
    info = {
        "smtp_user_cargado": bool(SMTP_USER),
        "smtp_pass_cargado": bool(SMTP_PASS),
        "smtp_pass_largo": len(SMTP_PASS),
        "smtp_pass_tiene_espacios": " " in SMTP_PASS,
        "host": SMTP_HOST,
        "port": SMTP_PORT,
        "notify_email": NOTIFY_EMAIL,
    }
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            info["conexion_login"] = "OK"
        info["resultado"] = "Credenciales válidas, el envío debería funcionar"
    except Exception as e:
        info["error"] = repr(e)
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
