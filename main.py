import os
import psycopg2
from psycopg2 import errors
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
from datetime import datetime, timedelta

# Carrega a URL do banco de dados do arquivo .env
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

app = FastAPI(title="API Motorista Pro")

# --- MODELOS DE DADOS (O que a API espera receber do Android) ---
class UsuarioNovo(BaseModel):
    firebase_uid: str
    nome: str
    email: str
    cpf: str
    android_id: str

class RotaBackup(BaseModel):
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

# --- FUNÇÃO DE CONEXÃO COM O NEON ---
def get_db_connection():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro de conexão com o banco: {str(e)}")

# ==========================================
# 1. ROTA DO AUTO-UPDATE (Manifesto)
# ==========================================
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

# ==========================================
# 2. ROTA DE REGISTRO E LIBERAÇÃO DOS 7 DIAS
# ==========================================
@app.post("/registrar-usuario")
def registrar_usuario(user: UsuarioNovo):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # 1. Tenta inserir o usuário (As travas UNIQUE do CPF e ANDROID_ID vão atuar aqui)
        cursor.execute("""
            INSERT INTO usuarios (firebase_uid, nome, email, cpf, android_id)
            VALUES (%s, %s, %s, %s, %s)
        """, (user.firebase_uid, user.nome, user.email, user.cpf, user.android_id))
        
        # 2. Se passou, cria a assinatura TRIAL com vencimento para daqui a 7 dias
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

# ==========================================
# 3. ROTA DE CHECAGEM DO PAYWALL (Bloqueio)
# ==========================================
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
            
            # Checa se a data de hoje já ultrapassou o vencimento
            if datetime.now(data_vencimento.tzinfo) > data_vencimento:
                # Opcional: Atualizar no banco para 'VENCIDA'
                cursor.execute("UPDATE assinaturas SET status = 'VENCIDA' WHERE firebase_uid = %s", (firebase_uid,))
                conn.commit()
                return {"status": "VENCIDA", "bloquear_app": True}
                
            return {"status": status_atual, "bloquear_app": False, "vence_em": data_vencimento}
            
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    finally:
        cursor.close()
        conn.close()

# ==========================================
# 4. ROTA PARA SALVAR O HISTÓRICO DA ROTA
# ==========================================
@app.post("/salvar-historico")
def salvar_historico(rota: RotaBackup):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO historico_rotas (
                firebase_uid, data_inicio_millis, data_fim_millis, tempo_decorrido_segundos,
                total_paradas, pacotes_entregues, pacotes_falhos, km_rodados,
                faturamento_bruto, consumo_kml, preco_combustivel
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            rota.firebase_uid, rota.data_inicio_millis, rota.data_fim_millis,
            rota.tempo_decorrido_segundos, rota.total_paradas, rota.pacotes_entregues,
            rota.pacotes_falhos, rota.km_rodados, rota.faturamento_bruto,
            rota.consumo_kml, rota.preco_combustivel
        ))
        conn.commit()
        return {"mensagem": "Histórico salvo com sucesso na nuvem!"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        conn.close()