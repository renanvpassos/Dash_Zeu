import os
import re
import io
import time
import base64
import threading
import requests
import unicodedata
from collections import Counter
from datetime import datetime, date
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo
import pandas as pd
import streamlit as st
from fpdf import FPDF
from supabase import create_client, Client
from streamlit_autorefresh import st_autorefresh

from google.oauth2.service_account import Credentials
import google.auth.transport.requests

# ==========================================
# CONFIGURAÇÃO DE BORDAS E ESPAÇAMENTO DO STREAMLIT
# ==========================================
st.set_page_config(layout="wide", page_title="Monitor Operacional - Multprocessing", page_icon="📊")

st.markdown(
    """
    <style>
        .main .block-container {
            padding-top: 1rem !important;
            padding-bottom: 1rem !important;
            padding-left: 1rem !important;
            padding-right: 1rem !important;
            max-width: 100% !important;
        }
        header[data-testid="stHeader"] {
            display: none;
        }
    </style>
    """,
    unsafe_allow_html=True
)

# Intervalo do autorefresh (ms) e validade do cache de sincronização (s).
# O TTL é MENOR que o intervalo para garantir que cada refresh dispare uma nova sincronização.
AUTOREFRESH_MS = 60_000
SYNC_TTL_SECONDS = 55


def get_logo_base64():
    try:
        with open("logoMult.png", "rb") as img_file:
            return base64.b64encode(img_file.read()).decode()
    except Exception:
        return "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

def exibir_intro():
    """Exibe a intro apenas com o logotipo durante o carregamento"""
    intro_placeholder = st.empty()
    logo_base64 = get_logo_base64()

    intro_html = f"""
    <style>
        .intro-container {{
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            z-index: 99999;
            transition: opacity 0.8s ease-in-out;
        }}
        .intro-container.fade-out {{
            opacity: 0;
            pointer-events: none;
        }}
        .logo-container {{
            display: flex;
            justify-content: center;
            align-items: center;
            width: 100%;
            animation: float 2s ease-in-out infinite;
        }}
        .logo-image {{
            max-width: 400px;
            width: 80%;
            margin: 0 auto;
            display: block;
        }}
        @keyframes float {{
            0% {{ transform: translateY(0px); }}
            50% {{ transform: translateY(-10px); }}
            100% {{ transform: translateY(0px); }}
        }}
    </style>

    <div id="intro-container" class="intro-container">
        <div class="logo-container">
            <img src="data:image/png;base64,{logo_base64}" class="logo-image" alt="Logo">
        </div>
    </div>

    <script>
        var percent = 0;
        var container = document.getElementById('intro-container');

        var interval = setInterval(function() {{
            percent += Math.floor(Math.random() * 15) + 5;
            if (percent > 100) percent = 100;

            if (percent >= 100) {{
                clearInterval(interval);
                setTimeout(function() {{
                    if (container) {{
                        container.classList.add('fade-out');
                        setTimeout(function() {{
                            container.style.display = 'none';
                        }}, 800);
                    }}
                }}, 300);
            }}
        }}, 100);
    </script>
    """

    intro_placeholder.markdown(intro_html, unsafe_allow_html=True)
    time.sleep(1.5)
    intro_placeholder.empty()
    return intro_placeholder

# ==========================================
# INICIALIZAÇÃO
# ==========================================
if 'intro_exibida' not in st.session_state:
    exibir_intro()
    st.session_state['intro_exibida'] = True

TZ_BR = ZoneInfo("America/Sao_Paulo")

def get_now_br() -> datetime:
    """Retorna o datetime atual no fuso de Brasília."""
    return datetime.now(TZ_BR)

st_autorefresh(interval=AUTOREFRESH_MS, key="auto_sync_timer")

# ==========================================
# CONEXÃO COM O SUPABASE
# ==========================================
@st.cache_resource
def init_supabase() -> Client:
    url = st.secrets.get("SUPABASE_URL")
    key = st.secrets.get("SUPABASE_KEY")
    if not url or not key:
        st.error("❌ Configurações do Supabase (SUPABASE_URL / SUPABASE_KEY) não encontradas nos Secrets.")
        st.stop()
    return create_client(url, key)

supabase = init_supabase()

# ==========================================
# GERADOR DE TOKEN GOOGLE OAUTH2
# ==========================================
@st.cache_resource
def get_google_credentials() -> Credentials:
    """Cria (uma única vez) o objeto de credenciais da Service Account."""
    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/drive.readonly"
        ]

        if "gcp_service_account" in st.secrets:
            creds_dict = dict(st.secrets["gcp_service_account"])
        else:
            creds_dict = {
                "type": st.secrets.get("type"),
                "project_id": st.secrets.get("project_id"),
                "private_key_id": st.secrets.get("private_key_id"),
                "private_key": st.secrets.get("private_key"),
                "client_email": st.secrets.get("client_email"),
                "client_id": st.secrets.get("client_id"),
                "auth_uri": st.secrets.get("auth_uri"),
                "token_uri": st.secrets.get("token_uri"),
                "auth_provider_x509_cert_url": st.secrets.get("auth_provider_x509_cert_url"),
                "client_x509_cert_url": st.secrets.get("client_x509_cert_url")
            }

        return Credentials.from_service_account_info(creds_dict, scopes=scopes)
    except Exception as e:
        st.error(f"❌ Erro ao carregar Credenciais do Google: {e}")
        st.stop()

def get_google_access_token() -> str:
    """Retorna um Access Token válido, renovando automaticamente se estiver expirado.
    DEVE ser chamada na thread principal do Streamlit (usa st.*)."""
    try:
        credentials = get_google_credentials()
        if not credentials.valid or credentials.expired:
            auth_req = google.auth.transport.requests.Request()
            credentials.refresh(auth_req)
        return credentials.token
    except Exception as e:
        st.error(f"❌ Erro ao obter Token de Acesso do Google: {e}")
        st.stop()

# ==========================================
# EXTRAÇÃO DE ID DAS PLANILHAS
# ==========================================
NOME_ABA_LOG = "LOG"

def extract_spreadsheet_id(url_or_id: str) -> str:
    match = re.search(r"/d/([a-zA-Z0-9-_]+)", str(url_or_id))
    return match.group(1) if match else str(url_or_id).strip()

LISTA_PLANILHAS = {
    f"PLANILHA {nome.replace('_', ' ')}": extract_spreadsheet_id(sheet_id_or_url)
    for nome, sheet_id_or_url in st.secrets.get("planilhas", {}).items()
}

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

# ==========================================
# ESTADO COMPARTILHADO DE SINCRONIZAÇÃO (entre todas as sessões)
# ==========================================
@st.cache_resource
def get_sync_state() -> dict:
    """Objeto único por processo do servidor. Guarda quando cada período foi
    sincronizado pela última vez, quais estão em andamento, erros e uma 'versão'
    que muda a cada sincronização concluída (usada para invalidar o cache de leitura)."""
    return {
        "lock": threading.Lock(),
        "last_sync": {},   # {(start, end): timestamp}
        "running": set(),  # {(start, end)}
        "errors": {},      # {(start, end): [mensagens]}
        "version": 0,
    }

# ==========================================
# GERENCIAMENTO DE LOGS VIA SUPABASE
# ==========================================
def _paginate(build_query, page_size: int = 1000) -> list:
    """O Supabase/PostgREST devolve no máximo 1000 linhas por requisição.
    Esta função percorre todas as páginas para não perder registros."""
    rows, offset = [], 0
    while True:
        resp = build_query().range(offset, offset + page_size - 1).execute()
        data = resp.data or []
        rows.extend(data)
        if len(data) < page_size:
            break
        offset += page_size
    return rows

@st.cache_data(show_spinner=False, max_entries=20)
def load_logs_by_period(start_date: date, end_date: date, version: int):
    """Lê os logs do período. O parâmetro `version` faz parte da chave de cache:
    quando uma sincronização termina, a versão muda e o cache é invalidado na hora.
    Sem sincronização nova, todas as sessões/reruns reaproveitam o mesmo resultado
    (zero consultas extras ao Supabase)."""
    try:
        return _paginate(
            lambda: supabase.table("atividades_eliseu")
            .select("*")
            .gte("date", start_date.isoformat())
            .lte("date", end_date.isoformat())
            .order("id", desc=True)
        )
    except Exception as e:
        raise RuntimeError(f"Erro ao buscar logs do Supabase: {e}")

def get_existing_signatures_for_sheet(sheet_name: str, start_date: date, end_date: date) -> Counter:
    data = _paginate(
        lambda: supabase.table("atividades_eliseu")
        .select("id, timestamp, mensagem")
        .eq("sheet_name", sheet_name)
        .gte("date", start_date.isoformat())
        .lte("date", end_date.isoformat())
        .order("id")
    )
    return Counter(f"{row.get('timestamp', '')}_{row.get('mensagem', '')}" for row in data)

def add_log_entries_bulk(logs_list) -> list:
    erros = []
    if not logs_list:
        return erros
    chunk_size = 500
    for i in range(0, len(logs_list), chunk_size):
        chunk = logs_list[i : i + chunk_size]
        try:
            supabase.table("atividades_eliseu").insert(chunk).execute()
        except Exception as e:
            erros.append(f"Erro ao salvar lote de registros no Supabase: {e}")
    return erros

# ==========================================
# TRATAMENTO DE TEXTO E PROCESSAMENTO
# ==========================================
def normalize_text(text: str) -> str:
    text_str = str(text).strip()
    try:
        text_str = text_str.encode('latin1').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass

    nfkd_form = unicodedata.normalize('NFKD', text_str)
    only_ascii = "".join([c for c in nfkd_form if not unicodedata.combining(c)])
    return only_ascii.upper()

def process_single_sheet_update(sheet_name, uploaded_df, start_date: date, end_date: date):
    new_columns = []
    for col in uploaded_df.columns:
        col_str = str(col).strip()
        norm_col = normalize_text(col_str)

        if "REFERENC" in norm_col or "REFERANC" in norm_col:
            new_columns.append("REFERÊNCIA")
        elif "IMPORTAD" in norm_col:
            new_columns.append("IMPORTADOR")
        elif "DIGITAD" in norm_col:
            new_columns.append("DIGITADOR")
        elif "DATA" in norm_col and "ATUALIZ" in norm_col:
            new_columns.append("DATA ATUALIZAÇÃO")
        elif "OBSERVAC" in norm_col or "OBSERVAB" in norm_col:
            new_columns.append("OBSERVAÇÃO")
        else:
            new_columns.append(col_str)

    uploaded_df.columns = new_columns

    req_cols = ["REFERÊNCIA", "DIGITADOR", "DATA ATUALIZAÇÃO"]
    missing = [col for col in req_cols if col not in uploaded_df.columns]

    if missing:
        erro = (
            f"❌ A aba '{NOME_ABA_LOG}' da planilha '{sheet_name}' não possui as colunas necessárias: {', '.join(missing)}.\n\n"
            f"**Colunas encontradas:** {list(uploaded_df.columns)}"
        )
        return False, erro

    uploaded_df = uploaded_df.fillna("-").astype(str)

    datas_parseadas = pd.to_datetime(
        uploaded_df["DATA ATUALIZAÇÃO"], dayfirst=True, errors="coerce"
    ).dt.date
    dentro_do_periodo = (datas_parseadas >= start_date) & (datas_parseadas <= end_date)
    uploaded_df = uploaded_df[dentro_do_periodo]

    if uploaded_df.empty:
        return True, None

    existing_counts = get_existing_signatures_for_sheet(sheet_name, start_date, end_date)
    used_counts = Counter()

    new_logs = []
    now_br = get_now_br()

    for _, row in uploaded_df.iterrows():
        digitador = str(row.get("DIGITADOR", "-")).strip()
        ref = str(row.get("REFERÊNCIA", "-")).strip()
        data_atualizacao = str(row.get("DATA ATUALIZAÇÃO", "-")).strip()

        if not digitador or digitador in ["-", "nan", "None"] or not ref or ref in ["-", "nan", "None"]:
            continue

        importador = str(row.get("IMPORTADOR", "-")).strip()
        if importador in ["nan", "None", ""]:
            importador = "-"

        observacao = str(row.get("OBSERVAÇÃO", "-")).strip()
        if observacao in ["nan", "None", ""]:
            observacao = "-"

        msg_log = (
            f"{data_atualizacao} — [{importador}] — {digitador} — "
            f"Ação: {observacao} — Referência: {ref}"
        )

        sig_key = f"{data_atualizacao}_{msg_log}"
        used_counts[sig_key] += 1

        if used_counts[sig_key] > existing_counts.get(sig_key, 0):
            try:
                dt_obj = pd.to_datetime(data_atualizacao, dayfirst=True, errors="coerce")
                if pd.notna(dt_obj):
                    parsed_date = dt_obj.strftime("%Y-%m-%d")
                else:
                    parsed_date = now_br.strftime("%Y-%m-%d")
            except Exception:
                parsed_date = now_br.strftime("%Y-%m-%d")

            new_logs.append({
                "timestamp": data_atualizacao,
                "date": parsed_date,
                "sheet_name": str(sheet_name),
                "digitador": digitador,
                "referencia": ref,
                "mensagem": msg_log
            })

    if new_logs:
        erros_insert = add_log_entries_bulk(new_logs)
        if erros_insert:
            return False, f"❌ **'{sheet_name}'**: " + " | ".join(erros_insert)

    return True, None

# ==========================================
# LEITURA DE PLANILHA VIA GOOGLE SHEETS API
# ==========================================
def fetch_and_process_sheet(name, sheet_id, token, start_date: date, end_date: date):
    try:
        range_ = f"{NOME_ABA_LOG}"
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{range_}"
        headers = {"Authorization": f"Bearer {token}"}
        params = {"valueRenderOption": "UNFORMATTED_VALUE", "dateTimeRenderOption": "FORMATTED_STRING"}

        response = requests.get(url, headers=headers, params=params, timeout=20)

        if response.status_code == 404:
            return False, f"❌ **Erro na '{name}'**: A aba '{NOME_ABA_LOG}' ou a planilha não foi encontrada."
        elif response.status_code == 403:
            return False, f"❌ **Erro na '{name}'**: Sem permissão. Verifique se a planilha foi compartilhada com o e-mail da Service Account."
        elif response.status_code != 200:
            return False, f"❌ Erro HTTP {response.status_code} ao buscar '{name}'."

        data = response.json()
        values = data.get("values", [])

        if not values or len(values) < 2:
            return True, None

        header, *rows = values
        max_len = len(header)
        rows_fixed = [row + [""] * (max_len - len(row)) for row in rows]

        df_dl = pd.DataFrame(rows_fixed, columns=header)

        return process_single_sheet_update(name, df_dl, start_date, end_date)

    except Exception as e:
        return False, f"❌ Erro inesperado ao processar '{name}': {e}"

def executar_sincronizacao(start_date: date, end_date: date, token: str):
    sucessos = 0
    erros = []

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [
            executor.submit(fetch_and_process_sheet, name, sheet_id, token, start_date, end_date)
            for name, sheet_id in LISTA_PLANILHAS.items()
        ]

        for future in as_completed(futures):
            try:
                success, err_msg = future.result()
            except Exception as e:
                success, err_msg = False, f"❌ Erro inesperado: {e}"
            if success:
                sucessos += 1
            elif err_msg:
                erros.append(err_msg)

    return sucessos, erros

# ==========================================
# ORQUESTRAÇÃO DA SINCRONIZAÇÃO COM CACHE
# ==========================================
def _run_sync_job(key, token, state):
    start_date, end_date = key
    try:
        _, erros = executar_sincronizacao(start_date, end_date, token)
    except Exception as e:
        erros = [f"❌ Falha na sincronização: {e}"]

    with state["lock"]:
        state["last_sync"][key] = time.time()
        state["errors"][key] = erros
        state["running"].discard(key)
        state["version"] += 1

        if len(state["last_sync"]) > 50:
            mais_antigos = sorted(state["last_sync"], key=state["last_sync"].get)[:-50]
            for k in mais_antigos:
                state["last_sync"].pop(k, None)
                state["errors"].pop(k, None)

def garantir_sincronizacao(start_date: date, end_date: date):
    state = get_sync_state()
    key = (start_date, end_date)

    with state["lock"]:
        last = state["last_sync"].get(key)
        esta_vencido = last is None or (time.time() - last) >= SYNC_TTL_SECONDS
        ja_rodando = key in state["running"]
        deve_iniciar = esta_vencido and not ja_rodando
        primeira_vez = last is None
        if deve_iniciar:
            state["running"].add(key)

    if not deve_iniciar:
        return

    try:
        token = get_google_access_token()
    except BaseException:
        with state["lock"]:
            state["running"].discard(key)
        raise

    if primeira_vez:
        with st.spinner("Sincronizando histórico do período selecionado..."):
            _run_sync_job(key, token, state)
    else:
        threading.Thread(target=_run_sync_job, args=(key, token, state), daemon=True).start()

# ==========================================
# RELATÓRIO PDF
# ==========================================
class PDFReport(FPDF):
    def header(self):
        self.set_font("Helvetica", "B", 14)
        self.cell(0, 10, "Relatorio de Atividades dos Digitadores", border=False, new_x="LMARGIN", new_y="NEXT", align="C")
        self.set_font("Helvetica", "", 9)
        self.cell(0, 5, f"Gerado em: {get_now_br().strftime('%d/%m/%Y %H:%M:%S')}", border=False, new_x="LMARGIN", new_y="NEXT", align="C")
        self.ln(5)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.cell(0, 10, f"Pagina {self.page_no()}/{{nb}}", align="C")

def sanitize_pdf_text(text: str) -> str:
    return unicodedata.normalize('NFKD', str(text)).encode('latin-1', 'ignore').decode('latin-1')

PDF_RED = (200, 30, 30)
PDF_BLUE = (41, 128, 185)
PDF_BLACK = (0, 0, 0)

def build_pdf_segments(mensagem: str):
    segments = []
    pattern = re.compile(r"de '([^']*)' para '([^']*)'|Referência: (.*)$")
    pos = 0

    for m in pattern.finditer(mensagem):
        start, end = m.span()
        if start > pos:
            segments.append((mensagem[pos:start], None))

        if m.group(1) is not None:
            segments.append(("de '", None))
            segments.append((m.group(1), PDF_RED))
            segments.append(("' para '", None))
            segments.append((m.group(2), PDF_RED))
            segments.append(("'", None))
        elif m.group(3) is not None:
            segments.append(("Referência: ", None))
            segments.append((m.group(3), PDF_BLUE))

        pos = end

    if pos < len(mensagem):
        segments.append((mensagem[pos:], None))

    return segments

def generate_pdf(logs_filtered, start_date, end_date) -> bytes:
    pdf = PDFReport()
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.set_font("Helvetica", "", 10)

    str_inicio = start_date.strftime('%d/%m/%Y') if hasattr(start_date, 'strftime') else str(start_date)
    str_fim = end_date.strftime('%d/%m/%Y') if hasattr(end_date, 'strftime') else str(end_date)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, sanitize_pdf_text(f"Periodo selecionado: {str_inicio} a {str_fim}"), new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 8, sanitize_pdf_text(f"Total de Registros: {len(logs_filtered)}"), new_x="LMARGIN", new_y="NEXT")

    digitador_counter = Counter()
    importador_counter = Counter()
    importador_pattern = re.compile(r"—\s*\[(.*?)\]\s*—")

    for item in logs_filtered:
        digitador = str(item.get("digitador", "-")).strip()
        if digitador and digitador not in ("-", "nan", "None"):
            digitador_counter[digitador] += 1

        match = importador_pattern.search(item.get("mensagem", ""))
        if match:
            importador = match.group(1).strip()
            if importador and importador not in ("-", "nan", "None"):
                importador_counter[importador] += 1

    if digitador_counter:
        top_digitador, qtd_digitador = digitador_counter.most_common(1)[0]
        pdf.cell(
            0, 8,
            sanitize_pdf_text(f"Digitador com mais atividade: {top_digitador} ({qtd_digitador} registro(s))"),
            new_x="LMARGIN", new_y="NEXT"
        )

    if importador_counter:
        top_importador, qtd_importador = importador_counter.most_common(1)[0]
        pdf.cell(
            0, 8,
            sanitize_pdf_text(f"Importador com mais atividade: {top_importador} ({qtd_importador} registro(s))"),
            new_x="LMARGIN", new_y="NEXT"
        )

    pdf.ln(3)
    pdf.set_draw_color(220, 220, 220)
    pdf.set_line_width(0.3)
    pdf.line(pdf.get_x(), pdf.get_y(), pdf.get_x() + 190, pdf.get_y())
    pdf.ln(5)

    pdf.set_font("Helvetica", "", 9)
    for item in logs_filtered:
        mensagem = item.get("mensagem", "")
        segments = build_pdf_segments(mensagem)

        for texto, cor in segments:
            texto_limpo = sanitize_pdf_text(texto)
            if cor:
                pdf.set_text_color(*cor)
            else:
                pdf.set_text_color(*PDF_BLACK)
            pdf.write(5, texto_limpo)

        pdf.set_text_color(*PDF_BLACK)
        pdf.ln(6)

    output = pdf.output()
    return bytes(output)

def obter_data_log(entry):
    timestamp_str = entry.get("timestamp", "")
    try:
        dt_conv = pd.to_datetime(timestamp_str, dayfirst=True, errors="coerce")
        if pd.notna(dt_conv):
            return dt_conv.to_pydatetime()
    except Exception:
        pass

    msg = entry.get("mensagem", "")
    match = re.search(r"^(\d{2}/\d{2}/\d{4}\s\d{2}:\d{2}:\d{2})", msg)
    if match:
        try:
            return datetime.strptime(match.group(1), "%d/%m/%Y %H:%M:%S")
        except Exception:
            pass

    return datetime.min

@st.cache_data(show_spinner=False, max_entries=5)
def gerar_pdf_cache(start_date: date, end_date: date, version: int) -> bytes:
    logs = load_logs_by_period(start_date, end_date, version)
    logs_ordenados_pdf = sorted(logs, key=obter_data_log, reverse=True)
    return generate_pdf(logs_ordenados_pdf, start_date, end_date)

# ==========================================
# TELA DE AUTENTICAÇÃO
# ==========================================
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if not st.session_state.authenticated:
    st.write("")
    col1, col2, col3 = st.columns([1, 2, 1])

    with col2:
        with st.container(border=True):
            st.title("🔐 Acesso ao Sistema")
            st.caption("Digite a senha para prosseguir")

            pass_required = st.secrets.get("system_password", "multproc")
            password_input = st.text_input("Senha de Acesso", type="password")

            if st.button("Entrar", use_container_width=True):
                if password_input == pass_required:
                    st.session_state.authenticated = True
                    st.rerun()
                else:
                    st.error("Senha incorreta! Tente novamente.")
    st.stop()

# ==========================================
# SELEÇÃO DE PERÍODO (ANTES DA SINCRONIZAÇÃO)
# ==========================================
hoje_br = get_now_br().date()

if "dt_inicio" not in st.session_state:
    st.session_state["dt_inicio"] = hoje_br
if "dt_fim" not in st.session_state:
    st.session_state["dt_fim"] = hoje_br

if st.session_state["dt_inicio"] > st.session_state["dt_fim"]:
    st.warning("⚠️ A Data Inicial é maior que a Data Final. Ajuste o período.")

# ==========================================
# SINCRONIZAÇÃO
# ==========================================
if st.session_state["dt_inicio"] <= st.session_state["dt_fim"]:
    garantir_sincronizacao(st.session_state["dt_inicio"], st.session_state["dt_fim"])

# ==========================================
# PAINEL PRINCIPAL
# ==========================================
col_titulo, col_logo = st.columns([0.88, 0.12], vertical_alignment="center")

with col_titulo:
    st.title("📊 Monitor Operacional em Tempo Real")

with col_logo:
    try:
        st.image("logoMult.png", use_container_width=True)
    except Exception:
        pass

st.caption(f"Monitorando **{len(LISTA_PLANILHAS)}** planilha(s) configurada(s) — dados lidos da aba **'{NOME_ABA_LOG}'**. *(Atenção: Atualiza automaticamente a cada 1 minuto!)*")

st.divider()

# --- SELEÇÃO DE DATAS E BUSCA NO SUPABASE ---
st.subheader("📅 Seleção de Período de Análise")
col_search, col_dt1, col_dt2 = st.columns([2, 1, 1])

with col_dt1:
    dt_inicio = st.date_input(
        "Data Inicial",
        key="dt_inicio",
        format="DD/MM/YYYY"
    )

with col_dt2:
    dt_fim = st.date_input(
        "Data Final",
        key="dt_fim",
        format="DD/MM/YYYY"
    )

with col_search:
    search_query = st.text_input(
        "🔍 Pesquisar no Log:",
        placeholder="Nome do digitador, importador ou referência..."
    )

# --- STATUS DA SINCRONIZAÇÃO ---
_state = get_sync_state()
_key = (dt_inicio, dt_fim)
with _state["lock"]:
    _ultima = _state["last_sync"].get(_key)
    _erros_sync = list(_state["errors"].get(_key, []))
    _em_andamento = _key in _state["running"]
    _versao = _state["version"]

for _msg in _erros_sync:
    st.error(_msg)

if _ultima:
    _hora = datetime.fromtimestamp(_ultima, TZ_BR).strftime("%H:%M:%S")
    st.caption(f"🔄 Última sincronização: **{_hora}**" + (" — sincronizando agora..." if _em_andamento else ""))
elif _em_andamento:
    st.caption("🔄 Sincronizando pela primeira vez...")

# --- CARREGA LOGS DO BANCO ---
if dt_inicio > dt_fim:
    st.stop()

try:
    logs_periodo_brutos = load_logs_by_period(dt_inicio, dt_fim, _versao)
except Exception as e:
    st.error(str(e))
    logs_periodo_brutos = []

if logs_periodo_brutos:
    df_logs_periodo = pd.DataFrame(logs_periodo_brutos)
else:
    df_logs_periodo = pd.DataFrame(columns=["timestamp", "date", "sheet_name", "digitador", "referencia", "mensagem"])

if not df_logs_periodo.empty and "date" in df_logs_periodo.columns:
    df_logs_periodo["parsed_date"] = pd.to_datetime(df_logs_periodo["date"], errors="coerce").dt.date
    df_logs_periodo = df_logs_periodo[
        (df_logs_periodo["parsed_date"] >= dt_inicio) &
        (df_logs_periodo["parsed_date"] <= dt_fim)
    ]

st.session_state["df_logs_periodo"] = df_logs_periodo
st.session_state["last_dt_inicio"] = dt_inicio
st.session_state["last_dt_fim"] = dt_fim

logs_periodo = df_logs_periodo.to_dict("records") if not df_logs_periodo.empty else []

st.divider()

# --- LÓGICA DE CÁLCULO DE AÇÕES AGRUPADAS ---
total_acoes_agrupadas = 0

if not df_logs_periodo.empty:
    col_data = None
    for candidatos in ["timestamp", "DATA ATUALIZAÇÃO", "data_atualizacao", "data_hora", "data"]:
        if candidatos in df_logs_periodo.columns:
            col_data = candidatos
            break

    col_digitador = "digitador" if "digitador" in df_logs_periodo.columns else None

    if col_data and col_digitador:
        df_sorted = df_logs_periodo.copy()
        df_sorted["dt_parsed"] = pd.to_datetime(df_sorted[col_data], dayfirst=True, errors="coerce")
        df_sorted = df_sorted.sort_values(by=[col_digitador, "dt_parsed"])
        df_sorted["diff_tempo"] = df_sorted.groupby(col_digitador)["dt_parsed"].diff()

        df_sorted["nova_acao"] = df_sorted["diff_tempo"].isna() | (df_sorted["diff_tempo"].dt.total_seconds() > 120)

        total_acoes_agrupadas = int(df_sorted["nova_acao"].sum())
        df_acoes_filtradas = df_sorted[df_sorted["nova_acao"]]
    else:
        total_acoes_agrupadas = len(df_logs_periodo)
        df_acoes_filtradas = df_logs_periodo.copy()
else:
    df_acoes_filtradas = pd.DataFrame(columns=["sheet_name", "digitador", "referencia"])

# ==========================================
# ESTATÍSTICAS E SELETOR UNIFICADO DE VISÃO
# ==========================================
st.subheader(f"📈 Estatísticas no Período ({dt_inicio.strftime('%d/%m/%Y')} a {dt_fim.strftime('%d/%m/%Y')})")

# Variável padrão da aba/visão selecionada
aba_selecionada = "🌐 Consolidado (Todas)"

if not df_logs_periodo.empty:
    # Alterado para 5 colunas para acomodar os "DOCS OK"
    col_m1, col_m2, col_m3, col_m4, col_m5 = st.columns(5)

    col_m1.metric("Ações Registradas no Período", total_acoes_agrupadas)
    col_m2.metric("Digitadores Ativos", df_logs_periodo["digitador"].nunique())
    col_m3.metric("Planilhas com Atividade", df_logs_periodo["sheet_name"].nunique())

    total_registrados = int(
        df_logs_periodo["mensagem"].str.contains("REGISTRADO", case=False, na=False).sum()
    )
    col_m4.metric("Registrados", total_registrados)

    # Nova métrica para DOCS OK
    total_docs_ok = int(
        df_logs_periodo["mensagem"].str.contains("DOCS OK", case=False, na=False).sum()
    )
    col_m5.metric("DOCS OK", total_docs_ok)

    planilhas_com_movimentacao = sorted([p for p in df_logs_periodo["sheet_name"].unique() if p and str(p) not in ["None", "nan", "-"]])
    planilhas_com_log = ["🌐 Consolidado (Todas)"] + planilhas_com_movimentacao

    # Seletor unificado simulando as abas e controlando simultaneamente estatísticas e histórico
    aba_selecionada = st.radio(
        "Selecione a Visão / Planilha para Estatísticas:",
        options=planilhas_com_log,
        horizontal=True,
        key="aba_estatisticas_ativa"
    )

    # --- FILTRO DE "REGISTRADOS" ---
    if "mensagem" in df_acoes_filtradas.columns:
        df_registrados_filtradas = df_acoes_filtradas[
            df_acoes_filtradas["mensagem"].str.contains("REGISTRADO", case=False, na=False)
        ]
    else:
        df_registrados_filtradas = pd.DataFrame(columns=df_acoes_filtradas.columns)

    # --- FILTRO DE "DOCS OK" ---
    if "mensagem" in df_acoes_filtradas.columns:
        df_docs_ok_filtradas = df_acoes_filtradas[
            df_acoes_filtradas["mensagem"].str.contains("DOCS OK", case=False, na=False)
        ]
    else:
        df_docs_ok_filtradas = pd.DataFrame(columns=df_acoes_filtradas.columns)

    # Renderiza os gráficos de acordo com a seleção unificada
    if aba_selecionada == "🌐 Consolidado (Todas)":
        c1, c2, c3 = st.columns(3)
        with c1:
            st.markdown("**Atividades por Digitador (Geral)**")
            st.bar_chart(df_acoes_filtradas["digitador"].value_counts())
        with c2:
            st.markdown("**Atividades por Planilha**")
            st.bar_chart(df_acoes_filtradas["sheet_name"].value_counts())
        with c3:
            st.markdown(f"**Registrados por Digitador ({len(df_registrados_filtradas)})**")
            st.bar_chart(df_registrados_filtradas["digitador"].value_counts())
    else:
        df_sheet_logs = df_acoes_filtradas[df_acoes_filtradas["sheet_name"] == aba_selecionada]
        df_sheet_registrados = df_registrados_filtradas[df_registrados_filtradas["sheet_name"] == aba_selecionada]

        c_s1, c_s2, c_s3 = st.columns(3)
        with c_s1:
            st.markdown("**Atividades por Digitador**")
            st.bar_chart(df_sheet_logs["digitador"].value_counts())
        with c_s2:
            st.markdown("**Ações mais Frequentes**")
            st.bar_chart(df_sheet_logs["referencia"].value_counts().head(10))
        with c_s3:
            st.markdown(f"**Registrados por Digitador ({len(df_sheet_registrados)})**")
            st.bar_chart(df_sheet_registrados["digitador"].value_counts())
else:
    st.info("Nenhuma atividade registrada no período selecionado.")

st.divider()

# ==========================================
# HISTÓRICO DE EVENTOS (SINCRONIZADO)
# ==========================================
st.markdown("**Histórico de Eventos:**")
log_container = st.container(height=380, border=True)

filtered_logs = []
if logs_periodo:
    for log in logs_periodo:
        # Aplicação automática do filtro da aba selecionada nas estatísticas
        if aba_selecionada != "🌐 Consolidado (Todas)":
            if str(log.get("sheet_name", "")) != aba_selecionada:
                continue

        # Filtro por termo de pesquisa digitado
        if search_query.strip():
            term = search_query.strip().lower()
            msg = str(log.get("mensagem", "")).lower()
            digitador = str(log.get("digitador", "")).lower()
            referencia = str(log.get("referencia", "")).lower()
            sheet = str(log.get("sheet_name", "")).lower()

            if not (term in msg or term in digitador or term in referencia or term in sheet):
                continue

        filtered_logs.append(log)

logs_ordenados = (
    sorted(filtered_logs, key=obter_data_log, reverse=True)
    if filtered_logs
    else []
)

with log_container:
    if logs_ordenados:
        for entry in logs_ordenados:
            mensagem = entry.get("mensagem", "")

            mensagem_formatada = re.sub(
                r"^(\d{2}/\d{2}/\d{4}\s\d{2}:\d{2}:\d{2})",
                r"<span style='color: #008000 !important; background-color: #e0e0e0; padding: 3px 8px; border-radius: 4px; font-weight: bold; display: inline-block;'>\1</span>",
                mensagem,
            )

            mensagem_formatada = re.sub(
                r"\b(de)\b", r"**\1**", mensagem_formatada, flags=re.IGNORECASE
            )
            mensagem_formatada = re.sub(
                r"\b(para)\b", r"**\1**", mensagem_formatada, flags=re.IGNORECASE
            )

            mensagem_formatada = re.sub(
                r"Ação:\s*(.*?)\s*—\s*Referência:\s*(.*)$",
                r"<span style='color: red; font-weight: bold;'>Ação:</span> \1 — Referência: <span style='color: #1E90FF; font-weight: bold;'>\2</span>",
                mensagem_formatada,
            )

            st.markdown(mensagem_formatada, unsafe_allow_html=True)
    else:
        st.write("Nenhum registro encontrado para os filtros selecionados.")

# ==========================================
# EXPORTAÇÃO DO LOG EM PDF
# ==========================================
st.write("")

if logs_periodo:
    pdf_bytes = gerar_pdf_cache(dt_inicio, dt_fim, _versao)

    st.download_button(
        label="📄 Extrair Log em PDF",
        data=io.BytesIO(pdf_bytes),
        file_name=f"relatorio_log_{dt_inicio.strftime('%Y%m%d')}_a_{dt_fim.strftime('%Y%m%d')}.pdf",
        mime="application/pdf",
        use_container_width=True,
    )
else:
    st.caption("📄 Nenhum registro no período selecionado para extrair em PDF.")
