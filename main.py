import os
import json
import logging
import hashlib
import hmac
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
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

SYSTEM_PROMPT = Path("sistema.txt").read_text(encoding="utf-8")

# Histórico de conversa por número (em memória — reinicia com o servidor)
historico: dict[str, list[dict]] = {}

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
        ).hexdigest()
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

    # Montar histórico (máximo 10 turnos)
    if remetente not in historico:
        historico[remetente] = []
    historico[remetente].append({"role": "user", "content": texto})
    historico[remetente] = historico[remetente][-10:]

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

    await enviar_mensagem(remetente, texto_resposta)
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


# ── Health check ─────────────────────────────────────────────────────────────
@app.get("/")
def health():
    return {"status": "online", "escritorio": "Cardim & Castro Advocacia"}
