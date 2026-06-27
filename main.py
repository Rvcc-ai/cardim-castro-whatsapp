import os
import json
import logging
import hashlib
import hmac
import smtplib
from email.mime.text import MIMEText
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, HTMLResponse
import anthropic
import httpx

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="Cardim & Castro — Atendimento WhatsApp")

VERIFY_TOKEN       = os.environ["WHATSAPP_VERIFY_TOKEN"]
WHATSAPP_TOKEN     = os.environ["WHATSAPP_ACCESS_TOKEN"]
PHONE_NUMBER_ID    = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
APP_SECRET         = os.environ.get("WHATSAPP_APP_SECRET", "")
ATTORNEY_PHONE     = os.environ.get("ATTORNEY_PHONE", "")
EMAIL_DESTINO      = os.environ.get("EMAIL_DESTINO", "")
EMAIL_SENHA        = os.environ.get("EMAIL_SENHA", "")

SYSTEM_PROMPT = Path("sistema.txt").read_text(encoding="utf-8")

# Histórico de conversa por número (em memória — reinicia com o servidor)
historico: dict[str, list[dict]] = {}

# Registro completo com timestamps para o painel de conversas
registros: dict[str, list[dict]] = {}

# IDs de mensagens já processadas (evita duplicatas do webhook)
mensagens_processadas: set[str] = set()

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ── Verificação do webhook (GET) ─────────────────────────────────────────────
@app.get("/webhook")
async def verificar_webhook(request: Request):
    mode      = request.query_params.get("hub.mode")
    token     = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        log.info("Webhook verificado pela Meta.")
        return PlainTextResponse(challenge)

    raise HTTPException(status_code=403, detail="Token inválido.")


# ── Recebimento de mensagens (POST) ──────────────────────────────────────────
@app.post("/webhook")
async def receber_mensagem(request: Request):
    # Validação de assinatura (segurança)
    if APP_SECRET:
        signature = request.headers.get("X-Hub-Signature-256", "")
        body = await request.body()
        expected = "sha256=" + hmac.new(
            APP_SECRET.encode(), body, hashlib.sha256
        ).hexdigest()  # type: ignore[attr-defined]
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(status_code=403, detail="Assinatura inválida.")
    else:
        body = await request.body()

    data = json.loads(body)

    try:
        entry   = data["entry"][0]
        changes = entry["changes"][0]
        value   = changes["value"]
        msgs    = value.get("messages", [])
    except (KeyError, IndexError):
        return {"status": "ok"}

    if not msgs:
        return {"status": "ok"}

    msg    = msgs[0]
    msg_id = msg.get("id", "")
    remetente = msg.get("from", "")
    tipo      = msg.get("type", "")

    # Ignorar mensagens duplicadas
    if msg_id in mensagens_processadas:
        return {"status": "ok"}
    mensagens_processadas.add(msg_id)
    if len(mensagens_processadas) > 500:
        mensagens_processadas.clear()

    # Só processar mensagens de texto
    if tipo != "text":
        await enviar_mensagem(remetente,
            "Prezado(a) Senhor(a), no momento processamos somente mensagens de texto. "
            "Por favor, descreva sua necessidade por escrito.\n\n"
            "Cristina — Cardim & Castro Advocacia")
        return {"status": "ok"}

    texto = msg["text"]["body"].strip()
    if not texto:
        return {"status": "ok"}

    log.info(f"Mensagem de {remetente}: {texto[:80]}")

    agora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    # Montar histórico (máximo 10 turnos)
    if remetente not in historico:
        historico[remetente] = []
    historico[remetente].append({"role": "user", "content": texto})
    historico[remetente] = historico[remetente][-10:]

    # Registrar para o painel
    if remetente not in registros:
        registros[remetente] = []
    registros[remetente].append({"role": "user", "content": texto, "time": agora})

    # Chamar Claude (agente Cristina)
    try:
        resposta = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=historico[remetente],
        )
        texto_resposta = resposta.content[0].text
    except Exception as e:
        log.error(f"Erro ao chamar Claude: {e}")
        texto_resposta = (
            "Prezado(a) Senhor(a), estamos com uma instabilidade momentânea. "
            "Por favor, tente novamente em alguns instantes.\n\n"
            "Cristina — Cardim & Castro Advocacia"
        )

    historico[remetente].append({"role": "assistant", "content": texto_resposta})
    registros[remetente].append({"role": "assistant", "content": texto_resposta, "time": datetime.now().strftime("%d/%m/%Y %H:%M:%S")})

    await enviar_mensagem(remetente, texto_resposta)
    await notificar_advogado(remetente, texto, texto_resposta)
    return {"status": "ok"}


# ── Envio de mensagem via WhatsApp Business API ───────────────────────────────
async def enviar_mensagem(destinatario: str, texto: str):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": destinatario,
        "type": "text",
        "text": {"body": texto},
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(url, json=payload, headers=headers)
        if r.status_code != 200:
            log.error(f"Erro ao enviar mensagem: {r.status_code} {r.text}")


# ── Notificação ao advogado ───────────────────────────────────────────────────
async def notificar_advogado(remetente: str, msg_cliente: str, resposta: str):
    if ATTORNEY_PHONE:
        texto = (
            f"⚖️ *Cardim & Castro — Novo atendimento*\n\n"
            f"📱 Cliente: +{remetente}\n\n"
            f"💬 Mensagem: {msg_cliente[:300]}\n\n"
            f"🤖 Cristina respondeu:\n{resposta[:300]}"
        )
        await enviar_mensagem(ATTORNEY_PHONE, texto)

    if EMAIL_DESTINO and EMAIL_SENHA:
        try:
            corpo = (
                f"Novo atendimento — Cristina IA\n\n"
                f"Cliente: +{remetente}\n\n"
                f"Mensagem do cliente:\n{msg_cliente}\n\n"
                f"Resposta da Cristina:\n{resposta}\n\n"
                f"---\nPainel completo: https://web-production-3c442.up.railway.app/conversas"
            )
            msg = MIMEText(corpo, "plain", "utf-8")
            msg["Subject"] = f"Cristina — Novo atendimento de +{remetente}"
            msg["From"] = EMAIL_DESTINO
            msg["To"] = EMAIL_DESTINO
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as smtp:
                smtp.login(EMAIL_DESTINO, EMAIL_SENHA)
                smtp.send_message(msg)
        except Exception as e:
            log.error(f"Erro ao enviar e-mail: {e}")


# ── Painel de conversas ───────────────────────────────────────────────────────
@app.get("/conversas", response_class=HTMLResponse)
def ver_conversas():
    total = sum(len(v) for v in registros.values())
    cards = ""
    for numero, msgs in sorted(registros.items(), key=lambda x: x[1][-1]["time"] if x[1] else "", reverse=True):
        bubbles = ""
        for m in msgs:
            lado = "cliente" if m["role"] == "user" else "cristina"
            nome = f"📱 {numero}" if m["role"] == "user" else "🤖 Cristina"
            cor  = "#e8f5e9" if m["role"] == "assistant" else "#e3f2fd"
            alinha = "flex-end" if m["role"] == "assistant" else "flex-start"
            texto_esc = m["content"].replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace("\n","<br>")
            bubbles += f"""
            <div style="display:flex;flex-direction:column;align-items:{alinha};margin:6px 0">
              <div style="font-size:11px;color:#888;margin-bottom:2px">{nome} &bull; {m['time']}</div>
              <div style="background:{cor};border-radius:12px;padding:10px 14px;max-width:75%;font-size:14px;line-height:1.5">{texto_esc}</div>
            </div>"""
        ultimo = msgs[-1]["time"] if msgs else ""
        cards += f"""
        <div style="background:#fff;border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,0.08);margin-bottom:24px;overflow:hidden">
          <div style="background:#1a1a2e;color:#fff;padding:12px 20px;display:flex;justify-content:space-between;align-items:center">
            <span style="font-weight:600">📞 +{numero}</span>
            <span style="font-size:12px;opacity:0.7">Último: {ultimo} &bull; {len(msgs)} mensagens</span>
          </div>
          <div style="padding:16px 20px;max-height:400px;overflow-y:auto">{bubbles}</div>
        </div>"""

    if not cards:
        cards = '<div style="text-align:center;color:#aaa;padding:60px">Nenhuma conversa ainda. Aguardando mensagens dos clientes.</div>'

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Cristina — Painel de Conversas</title>
  <meta http-equiv="refresh" content="30">
  <style>body{{margin:0;font-family:'Segoe UI',sans-serif;background:#f0f2f5}}header{{background:#1a1a2e;color:#fff;padding:20px 32px;display:flex;justify-content:space-between;align-items:center}}main{{max-width:900px;margin:32px auto;padding:0 16px}}</style>
</head>
<body>
  <header>
    <div>
      <div style="font-size:20px;font-weight:700">⚖️ Cardim &amp; Castro Advocacia</div>
      <div style="font-size:13px;opacity:0.7;margin-top:4px">Painel de Atendimento — Cristina IA</div>
    </div>
    <div style="text-align:right;font-size:13px;opacity:0.8">
      {len(registros)} conversa(s) &bull; {total} mensagens<br>
      <span style="font-size:11px">Atualiza a cada 30s</span>
    </div>
  </header>
  <main>{cards}</main>
</body>
</html>"""


# ── Health check ─────────────────────────────────────────────────────────────
@app.get("/")
def health():
    return {"status": "online", "escritorio": "Cardim & Castro Advocacia"}
