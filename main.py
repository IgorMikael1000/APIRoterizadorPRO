import os
import psycopg2
import psycopg2.extras
from psycopg2 import errors
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from dotenv import load_dotenv
from datetime import datetime, timedelta
import mercadopago
from typing import Optional

# Carrega as variáveis do arquivo .env
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
# Variável de ambiente do Render
MERCADO_PAGO_ACCESS_TOKEN = os.getenv("ACCESS_TOKEN") 

app = FastAPI(title="API Motorista Pro")

# Inicializa o SDK do Mercado Pago
sdk = mercadopago.SDK(MERCADO_PAGO_ACCESS_TOKEN)

# --- CONFIGURAÇÃO DOS PLANOS (Valores Progressivos) ---
PLANOS = {
    "mensal": {"dias": 30, "valor": 9.90, "desc": "Assinatura Mensal"},
    "trimestral": {"dias": 90, "valor": 26.90, "desc": "Assinatura Trimestral"},
    "semestral": {"dias": 180, "valor": 49.90, "desc": "Assinatura Semestral"},
    "anual": {"dias": 365, "valor": 94.90, "desc": "Assinatura Anual"}
}

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

class AssinaturaUpdate(BaseModel):
    firebase_uid: str
    status: str  # Ex: 'ATIVO', 'VENCIDA'
    plano: str = "mensal" # Aceita 'mensal', 'trimestral', 'semestral', 'anual'

# Modelos do Mercado Pago
class PixRequest(BaseModel):
    firebase_uid: str
    email: str
    nome: str
    cpf: str
    plano: str = "mensal"


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
        
        # Concede 7 dias de teste grátis (TRIAL)
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


@app.delete("/deletar-usuario/{firebase_uid}")
def deletar_usuario(firebase_uid: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM assinaturas WHERE firebase_uid = %s;", (firebase_uid,))
        cursor.execute("DELETE FROM historico_rotas WHERE firebase_uid = %s;", (firebase_uid,))
        cursor.execute("DELETE FROM usuarios WHERE firebase_uid = %s;", (firebase_uid,))
        
        conn.commit()
        return {"mensagem": "Usuário e todos os seus dados foram excluídos com sucesso."}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Erro ao excluir dados do servidor: {str(e)}")
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
            
            # Verifica se o período expirou
            if datetime.now(data_vencimento.tzinfo) > data_vencimento and status_atual != 'ATIVO':
                cursor.execute("UPDATE assinaturas SET status = 'VENCIDA' WHERE firebase_uid = %s", (firebase_uid,))
                conn.commit()
                return {"status": "VENCIDA", "bloquear_app": True}
                
            return {"status": status_atual, "bloquear_app": False, "vence_em": data_vencimento}
            
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    finally:
        cursor.close()
        conn.close()


@app.post("/atualizar-assinatura")
def atualizar_assinatura(req: AssinaturaUpdate):
    """Rota usada pelo app (Google Billing) para ativar a assinatura com base no plano escolhido."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        plano_info = PLANOS.get(req.plano.lower(), PLANOS["mensal"])
        nova_data_vencimento = datetime.now() + timedelta(days=plano_info["dias"])
        
        cursor.execute("""
            UPDATE assinaturas 
            SET status = %s, data_vencimento = %s 
            WHERE firebase_uid = %s
        """, (req.status, nova_data_vencimento, req.firebase_uid))
        conn.commit()
        return {"mensagem": f"Assinatura {req.plano} atualizada com sucesso na nuvem!", "nova_data": nova_data_vencimento}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conn.close()


# ==========================================
# ROTAS DO MERCADO PAGO (PIX COM PLANOS)
# ==========================================

@app.post("/gerar-pix")
def gerar_pix(req: PixRequest):
    plano_info = PLANOS.get(req.plano.lower(), PLANOS["mensal"])
    
    # Codifica o UID e o plano na external_reference para o Webhook identificar depois
    external_ref = f"{req.firebase_uid}|{req.plano.lower()}"

    payment_data = {
        "transaction_amount": plano_info["valor"],
        "payment_method_id": "pix",
        "payer": {
            "email": req.email,
            "first_name": req.nome,
            "identification": {
                "type": "CPF",
                "number": req.cpf
            }
        },
        "description": f"{plano_info['desc']} - Roterizador PRO",
        "external_reference": external_ref 
    }

    result = sdk.payment().create(payment_data)
    payment = result.get("response", {})

    if "id" not in payment:
        raise HTTPException(status_code=400, detail=f"Erro ao gerar Pix no MP: {payment}")

    return {
        "id_pagamento": payment["id"],
        "pix_copia_cola": payment["point_of_interaction"]["transaction_data"]["qr_code"],
        "qr_code_base64": payment["point_of_interaction"]["transaction_data"]["qr_code_base64"],
        "plano_gerado": req.plano.lower()
    }


@app.post("/webhook-mercadopago")
async def webhook_mercadopago(request: Request):
    """
    Recebe atualizações de status de pagamento Pix do Mercado Pago.
    """
    try:
        data = await request.json()
        print(f"Webhook MP recebido: {data}")

        tipo = data.get("type") or data.get("topic")
        data_payload = data.get("data", {})
        payment_id = data_payload.get("id")
        
        if not payment_id and "id" in data:
            payment_id = data.get("id")

        if (tipo == "payment" or data.get("action") == "payment.updated" or data.get("action") == "payment.created") and payment_id:
            payment_info = sdk.payment().get(payment_id)
            payment_response = payment_info.get("response", {})
            status = payment_response.get("status")
            
            external_ref_raw = payment_response.get("external_reference", "")
            
            # Extrai o UID do Firebase e o Plano codificado
            partes_ref = external_ref_raw.split("|")
            firebase_uid = partes_ref[0] if len(partes_ref) > 0 else None
            plano_str = partes_ref[1] if len(partes_ref) > 1 else "mensal"

            print(f"Pagamento {payment_id} | Status: {status} | UID: {firebase_uid} | Plano: {plano_str}")

            if status == "approved" and firebase_uid:
                plano_info = PLANOS.get(plano_str, PLANOS["mensal"])
                nova_data = datetime.now() + timedelta(days=plano_info["dias"])
                
                conn = get_db_connection()
                cursor = conn.cursor()
                try:
                    cursor.execute("""
                        UPDATE assinaturas 
                        SET status = 'ATIVO', data_vencimento = %s 
                        WHERE firebase_uid = %s
                    """, (nova_data, firebase_uid))
                    conn.commit()
                    print(f"Assinatura do UID {firebase_uid} renovada por {plano_info['dias']} dias com sucesso!")
                except Exception as db_err:
                    conn.rollback()
                    print(f"Erro ao atualizar banco via webhook: {db_err}")
                finally:
                    cursor.close()
                    conn.close()

        return {"status": "ok"}
    except Exception as e:
        print(f"Erro crítico no processamento do webhook: {str(e)}")
        return {"status": "error", "detail": str(e)}


# ==========================================
# HISTÓRICO DE ROTAS
# ==========================================

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


@app.delete("/deletar-historico/{rota_id}")
def deletar_historico(rota_id: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM historico_rotas WHERE id = %s;", (rota_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Rota não encontrada no histórico.")
        conn.commit()
        return {"mensagem": "Rota deletada com sucesso do histórico."}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conn.close()