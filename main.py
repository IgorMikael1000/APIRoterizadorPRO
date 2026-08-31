import os
import psycopg2
import psycopg2.extras
from psycopg2 import errors
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
from datetime import datetime, timedelta
import mercadopago

# Carrega as variáveis do arquivo .env
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
MERCADO_PAGO_TOKEN = os.getenv("MERCADO_PAGO_ACCESS_TOKEN")

app = FastAPI(title="API Motorista Pro")

# Inicializa SDK do Mercado Pago
sdk = mercadopago.SDK(MERCADO_PAGO_TOKEN)

# --- MODELOS DE DADOS ---
class UsuarioNovo(BaseModel):
    firebase_uid: str
    nome: str
    email: str
    cpf: str
    android_id: str

class RotaBackup(BaseModel):
    id: str
    firebase_uid: str
    data_inicio_millis: int
    data_fim_millis: int
    tempo_decorrido_segundos: int
    total_paradas: int
    pacotes_entregues: int
    pacotes_falhos: int
    km_rodados: float
    faturamento_bruto: float
    consumo_kml: float
    preco_combustivel: float

class PixRequest(BaseModel):
    email_usuario: str
    valor: float
    uid_firebase: str

# --- CONEXÃO COM O NEON ---
def get_db_connection():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro de conexão com o banco: {str(e)}")


@app.get("/checar-versao")
def checar_versao():
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT versao_codigo, versao_nome, link_drive, notas_atualizacao FROM app_config WHERE id = 1;")
        config = cursor.fetchone()
        
        if config:
            return {
                "versao_codigo": config[0],
                "versao_nome": config[1],
                "link_drive": config[2],
                "notas_atualizacao": config[3]
            }
        raise HTTPException(status_code=404, detail="Configuração não encontrada.")
    finally:
        cursor.close()
        conn.close()


@app.post("/registrar-usuario")
def registrar_usuario(user: UsuarioNovo):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            INSERT INTO usuarios (firebase_uid, nome, email, cpf, android_id)
            VALUES (%s, %s, %s, %s, %s)
        """, (user.firebase_uid, user.nome, user.email, user.cpf, user.android_id))
        
        data_vencimento = datetime.now() + timedelta(days=7)
        
        cursor.execute("""
            INSERT INTO assinaturas (firebase_uid, status, data_vencimento)
            VALUES (%s, 'TRIAL', %s)
        """, (user.firebase_uid, data_vencimento))
        
        conn.commit()
        return {"mensagem": "Conta criada com sucesso! 7 dias grátis ativados.", "status_assinatura": "TRIAL"}
        
    except errors.UniqueViolation as e:
        conn.rollback()
        erro_msg = str(e)
        if "usuarios_cpf_key" in erro_msg:
            raise HTTPException(status_code=400, detail="Este CPF já foi utilizado no período de testes.")
        elif "usuarios_android_id_key" in erro_msg:
            raise HTTPException(status_code=400, detail="Este dispositivo já esgotou o limite de contas grátis.")
        else:
            raise HTTPException(status_code=400, detail="E-mail já cadastrado.")
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conn.close()


@app.get("/status-assinatura/{firebase_uid}")
def status_assinatura(firebase_uid: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT status, data_vencimento FROM assinaturas WHERE firebase_uid = %s;", (firebase_uid,))
        assinatura = cursor.fetchone()
        
        if assinatura:
            status_atual = assinatura[0]
            data_vencimento = assinatura[1]
            
            if datetime.now(data_vencimento.tzinfo) > data_vencimento:
                cursor.execute("UPDATE assinaturas SET status = 'VENCIDA' WHERE firebase_uid = %s", (firebase_uid,))
                conn.commit()
                return {"status": "VENCIDA", "bloquear_app": True}
                
            return {"status": status_atual, "bloquear_app": False, "vence_em": data_vencimento}
            
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    finally:
        cursor.close()
        conn.close()


@app.post("/salvar-historico")
def salvar_historico(rota: RotaBackup):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO historico_rotas (
                id, firebase_uid, data_inicio_millis, data_fim_millis, tempo_decorrido_segundos,
                total_paradas, pacotes_entregues, pacotes_falhos, km_rodados,
                faturamento_bruto, consumo_kml, preco_combustivel
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                data_inicio_millis = EXCLUDED.data_inicio_millis,
                data_fim_millis = EXCLUDED.data_fim_millis,
                tempo_decorrido_segundos = EXCLUDED.tempo_decorrido_segundos,
                total_paradas = EXCLUDED.total_paradas,
                pacotes_entregues = EXCLUDED.pacotes_entregues,
                pacotes_falhos = EXCLUDED.pacotes_falhos,
                km_rodados = EXCLUDED.km_rodados,
                faturamento_bruto = EXCLUDED.faturamento_bruto,
                consumo_kml = EXCLUDED.consumo_kml,
                preco_combustivel = EXCLUDED.preco_combustivel
        """, (
            rota.id, rota.firebase_uid, rota.data_inicio_millis, rota.data_fim_millis,
            rota.tempo_decorrido_segundos, rota.total_paradas, rota.pacotes_entregues,
            rota.pacotes_falhos, rota.km_rodados, rota.faturamento_bruto,
            rota.consumo_kml, rota.preco_combustivel
        ))
        conn.commit()
        return {"mensagem": "Histórico salvo/atualizado com sucesso na nuvem!"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conn.close()


@app.get("/obter-historico")
def obter_historico(firebase_uid: str):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cursor.execute("""
            SELECT id, data_inicio_millis, data_fim_millis, tempo_decorrido_segundos,
                   total_paradas, pacotes_entregues, pacotes_falhos, km_rodados,
                   faturamento_bruto, consumo_kml, preco_combustivel
            FROM historico_rotas
            WHERE firebase_uid = %s
            ORDER BY data_fim_millis DESC
        """, (firebase_uid,))
        
        rotas = cursor.fetchall()
        return rotas
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conn.close()


# ==========================================
# 5. ROTAS DE PAGAMENTO (MERCADO PAGO)
# ==========================================
@app.post("/gerar-pix")
async def gerar_pix(req: PixRequest):
    payment_data = {
        "transaction_amount": req.valor,
        "payment_method_id": "pix",
        "payer": {
            "email": req.email_usuario
        },
        "description": f"Assinatura RoterizadorPRO - UID:{req.uid_firebase}" # Nome corrigido aqui
    }

    result = sdk.payment().create(payment_data)
    payment = result["response"]

    if "id" not in payment:
        raise HTTPException(status_code=400, detail="Erro ao gerar Pix no Mercado Pago")

    return {
        "id_pagamento": payment["id"],
        "pix_copia_cola": payment["point_of_interaction"]["transaction_data"]["qr_code"],
        "qr_code_base64": payment["point_of_interaction"]["transaction_data"]["qr_code_base64"]
    }

@app.post("/webhook")
async def mercado_pago_webhook(data: dict):
    if data.get("type") == "payment":
        payment_id = data["data"]["id"]
        
        # Consulta o Mercado Pago para confirmar se realmente foi pago
        payment_info = sdk.payment().get(payment_id)
        status = payment_info["response"]["status"]
        
        if status == "approved":
            # Extrai o UID que mandamos na description "Assinatura Motorista Pro - UID:123..."
            description = payment_info["response"]["description"]
            uid = description.split("UID:")[-1]
            
            # Adiciona 30 dias de assinatura
            nova_data_vencimento = datetime.now() + timedelta(days=30)
            
            conn = get_db_connection()
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    UPDATE assinaturas 
                    SET status = 'ATIVO', data_vencimento = %s 
                    WHERE firebase_uid = %s
                """, (nova_data_vencimento, uid))
                conn.commit()
            finally:
                cursor.close()
                conn.close()

    return {"status": "ok"}