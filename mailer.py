"""Sends notification emails over SMTP (Gmail by default).

Credentials are read from environment variables or from a local `.env` file,
never hard-coded, so the password does not end up in the code you share or submit.

  SMTP_USER         the sending Gmail address
  SMTP_PASSWORD     a Gmail *app password* (not your normal password)
  SMTP_HOST / SMTP_PORT   default smtp.gmail.com / 587
  APP_URL           link shown in emails (default http://127.0.0.1:5000)
  MAIL_REDIRECT_TO  optional: send every email to this address instead (handy for demos)
"""
import os, smtplib, threading
from email.message import EmailMessage

PLACEHOLDER_DOMAIN = "@univ.edu"   # addresses used by the seeded demo data; they are not real inboxes


def _load_env_file():  # (db.py also does this; kept so mailer works standalone)
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path): return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
_load_env_file()


def configured():
    return bool(os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASSWORD"))


def _send(to, subject, body):
    try:
        user = os.environ["SMTP_USER"]
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, f"TeamUp <{user}>", to
        msg.set_content(body)
        with smtplib.SMTP(os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", 587)), timeout=20) as s:
            s.starttls()
            s.login(user, os.environ["SMTP_PASSWORD"].replace(" ", ""))
            s.send_message(msg)
        print(f"[mail] sent to {to}: {subject}")
    except Exception as e:                       # an email problem must never break the app
        print(f"[mail] FAILED to send to {to}: {e}")


def send_async(to, subject, body):
    """Send in a background thread so the web request is not slowed down."""
    if not to: return
    if not configured():
        print("[mail] skipped: SMTP_USER / SMTP_PASSWORD are not set"); return
    subject = " ".join(subject.split())          # no line breaks in headers
    redirect = os.environ.get("MAIL_REDIRECT_TO")
    if redirect:
        subject, to = f"[for {to}] {subject}", redirect
    elif to.lower().endswith(PLACEHOLDER_DOMAIN):
        print(f"[mail] skipped placeholder address {to} (set MAIL_REDIRECT_TO to test with demo accounts)"); return
    threading.Thread(target=_send, args=(to, subject, body), daemon=True).start()
