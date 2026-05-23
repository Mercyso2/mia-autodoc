import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from core.database import insert_row, list_rows, update_row
from services.sharepoint_mapper import (
    discover_site_from_url,
    prepare_site_hml,
    sync_site_folders,
    bootstrap_hml_structure,
    generate_folder_map,
)

# =========================================================
# MIA AUTODOC — PAINEL VISUAL V4.3
# =========================================================

ACCENT = "#A9798B"
BG = "#070708"
PANEL = "#111113"
PANEL_2 = "#17171A"
BORDER = "rgba(169, 121, 139, 0.22)"
TEXT = "#F4F1F2"
MUTED = "#A7A0A4"
GREEN = "#65C466"
YELLOW = "#D6A84F"
BLUE = "#6AA7E8"
RED = "#E86F76"

st.set_page_config(
    page_title="MIA Autodoc | Painel de Controle",
    page_icon="MIA",
    layout="wide",
    initial_sidebar_state="expanded",
)


def inject_css() -> None:
    st.markdown(
        f"""
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

        :root {{
          --accent: {ACCENT};
          --bg: {BG};
          --panel: {PANEL};
          --panel2: {PANEL_2};
          --border: {BORDER};
          --text: {TEXT};
          --muted: {MUTED};
        }}

        html, body, [class*="css"] {{
          font-family: 'Inter', sans-serif;
        }}

        .stApp {{
          background:
            radial-gradient(circle at 20% 0%, rgba(169,121,139,.14), transparent 34%),
            radial-gradient(circle at 80% 8%, rgba(169,121,139,.08), transparent 32%),
            linear-gradient(145deg, #050506 0%, #0B0B0D 42%, #070708 100%);
          color: var(--text);
        }}

        [data-testid="stSidebar"] {{
          background: linear-gradient(180deg, #080809 0%, #0E0E10 100%);
          border-right: 1px solid rgba(255,255,255,.07);
        }}

        [data-testid="stSidebar"] > div:first-child {{
          padding-top: 24px;
        }}

        .block-container {{
          padding-top: 28px;
          padding-bottom: 48px;
          max-width: 1600px;
        }}

        h1, h2, h3 {{ color: var(--text) !important; letter-spacing: -0.03em; }}
        p, label, span, div {{ color: inherit; }}

        .mia-brand {{
          display:flex; align-items:center; gap:14px; margin: 6px 0 26px 0;
        }}
        .mia-logo {{
          width:56px;height:56px;border-radius:18px;
          display:flex;align-items:center;justify-content:center;
          background: linear-gradient(145deg, rgba(169,121,139,.36), rgba(169,121,139,.05));
          border: 1px solid rgba(169,121,139,.55);
          box-shadow: 0 0 28px rgba(169,121,139,.32), inset 0 0 18px rgba(169,121,139,.14);
          color:#fff;font-weight:800;font-size:20px;letter-spacing:.04em;
        }}
        .mia-title {{ font-size:20px;font-weight:800;color:#fff; line-height:1.1; }}
        .mia-subtitle {{ color:var(--muted);font-size:12px;margin-top:4px; }}

        .side-env {{
          margin-top: 28px; padding:18px; border-radius:18px;
          background: linear-gradient(145deg, rgba(255,255,255,.06), rgba(255,255,255,.025));
          border: 1px solid rgba(255,255,255,.09);
        }}
        .side-env small {{ color: var(--accent); letter-spacing:.22em; font-size:11px; font-weight:700; }}
        .side-env b {{ display:block; font-size:20px; margin:8px 0 8px; color:#fff; }}
        .dot {{ width:9px; height:9px; display:inline-block; border-radius:999px; background:{GREEN}; box-shadow:0 0 12px rgba(101,196,102,.55); margin-right:7px; }}

        .hero {{
          display:flex; justify-content:space-between; align-items:flex-start; gap:24px; margin-bottom:18px;
        }}
        .eyebrow {{
          color: var(--accent); font-size:12px; font-weight:800; letter-spacing:.32em; margin-bottom:8px;
        }}
        .hero h1 {{ font-size:42px; line-height:1; margin:0 0 10px; font-weight:800; }}
        .hero p {{ color: var(--muted); margin:0; max-width:800px; font-size:14px; line-height:1.55; }}
        .action-row {{ display:flex; gap:12px; flex-wrap:wrap; justify-content:flex-end; }}
        .mia-btn {{
          border-radius:12px; padding:12px 18px; font-weight:700; font-size:13px;
          background: rgba(255,255,255,.035); border:1px solid rgba(255,255,255,.12); color:#fff;
        }}
        .mia-btn.primary {{
          background: linear-gradient(135deg, var(--accent), #8E5F72);
          border-color: rgba(255,255,255,.14); box-shadow: 0 10px 26px rgba(169,121,139,.24);
        }}

        .kpi-grid {{
          display:grid; grid-template-columns: repeat(5, minmax(150px,1fr)); gap:14px; margin: 18px 0 14px;
        }}
        .kpi-card {{
          position:relative; overflow:hidden; padding:18px; min-height:105px; border-radius:18px;
          background: linear-gradient(145deg, rgba(255,255,255,.065), rgba(255,255,255,.025));
          border: 1px solid rgba(255,255,255,.085);
          box-shadow: 0 16px 35px rgba(0,0,0,.22);
        }}
        .kpi-card:after {{
          content:""; position:absolute; right:-30px; top:-35px; width:110px; height:110px;
          background: radial-gradient(circle, rgba(169,121,139,.22), transparent 70%);
        }}
        .kpi-icon {{
          width:38px;height:38px;border-radius:13px;display:flex;align-items:center;justify-content:center;
          color:var(--accent); background:rgba(169,121,139,.12); border:1px solid rgba(169,121,139,.26);
          font-weight:800; margin-bottom:10px;
        }}
        .kpi-title {{ color:var(--muted); font-size:12px; font-weight:700; }}
        .kpi-value {{ color:#fff; font-size:30px; font-weight:800; line-height:1.15; margin-top:4px; }}
        .kpi-help {{ color:var(--muted); font-size:12px; margin-top:4px; }}

        .panel {{
          border-radius:20px; padding:18px; background: linear-gradient(145deg, rgba(255,255,255,.055), rgba(255,255,255,.02));
          border: 1px solid rgba(255,255,255,.085); box-shadow: 0 18px 40px rgba(0,0,0,.24);
          margin-bottom:14px;
        }}
        .panel-title {{
          display:flex;align-items:center;gap:10px;font-size:19px;font-weight:800;color:#fff;margin-bottom:4px;
        }}
        .panel-title .accent-icon {{ color:var(--accent); }}
        .panel-desc {{ color:var(--muted); font-size:12px; margin-bottom:14px; }}

        .status-pill {{
          display:inline-flex; align-items:center; justify-content:center; white-space:nowrap;
          padding:6px 10px; border-radius:999px; font-size:12px; font-weight:800;
          border:1px solid rgba(255,255,255,.12);
        }}
        .pill-green {{ background:rgba(101,196,102,.13); color:#98F19A; }}
        .pill-yellow {{ background:rgba(214,168,79,.13); color:#FFD985; }}
        .pill-blue {{ background:rgba(106,167,232,.13); color:#A9D2FF; }}
        .pill-red {{ background:rgba(232,111,118,.13); color:#FFA0A6; }}
        .pill-muted {{ background:rgba(255,255,255,.08); color:#D5D0D2; }}

        .scorebar {{ height:7px; background:rgba(255,255,255,.10); border-radius:999px; overflow:hidden; margin-top:5px; }}
        .scorebar > div {{ height:100%; border-radius:999px; background: linear-gradient(90deg, var(--accent), #D5A7B9); }}

        .timeline {{ position:relative; margin-top:12px; }}
        .timeline:before {{ content:""; position:absolute; left:18px; top:18px; bottom:18px; width:1px; background:rgba(169,121,139,.35); }}
        .step {{ display:flex; gap:14px; position:relative; margin:0 0 20px; }}
        .step-num {{
          width:36px;height:36px;border-radius:999px;display:flex;align-items:center;justify-content:center;flex:0 0 36px;
          background:rgba(169,121,139,.12); border:1px solid rgba(169,121,139,.48); color:#fff; font-weight:800; font-size:12px; z-index:2;
        }}
        .step b {{ color:#fff; font-size:14px; }}
        .step p {{ color:var(--muted); font-size:12px; margin:3px 0 0; }}

        .trace-grid {{ display:grid; grid-template-columns: 1fr 1fr; gap:12px 20px; }}
        .trace-item {{ border-bottom:1px solid rgba(255,255,255,.07); padding:8px 0; }}
        .trace-label {{ color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.06em; }}
        .trace-value {{ color:#fff; font-size:13px; font-weight:700; margin-top:3px; overflow-wrap:anywhere; }}
        .trace-link {{ color:#D9AABA; }}

        .map-row {{
          display:grid; grid-template-columns: 1fr auto auto; gap:12px; align-items:center;
          padding:10px 0; border-bottom:1px solid rgba(255,255,255,.07);
        }}
        .breadcrumb-chip {{
          display:inline-flex; align-items:center; gap:6px; padding:5px 8px; border-radius:8px;
          background:rgba(255,255,255,.045); border:1px solid rgba(255,255,255,.08); color:#fff; font-size:12px; margin:2px 2px;
        }}
        .breadcrumb-chip.accent {{ color:#fff; border-color:rgba(169,121,139,.30); background:rgba(169,121,139,.12); }}

        div[data-testid="stDataFrame"] {{ border-radius:16px; overflow:hidden; }}
        .stDataFrame, .stTable {{ background: transparent; }}

        .stButton>button {{
          border-radius:12px !important; border:1px solid rgba(169,121,139,.32) !important;
          background: linear-gradient(145deg, rgba(169,121,139,.24), rgba(169,121,139,.10)) !important;
          color:#fff !important; font-weight:800 !important; padding:0.6rem 1rem !important;
        }}
        .stButton>button:hover {{ border-color: rgba(169,121,139,.80) !important; box-shadow:0 0 0 3px rgba(169,121,139,.14); }}

        input, textarea, select {{ color:#fff !important; }}
        [data-baseweb="input"], [data-baseweb="select"], [data-baseweb="textarea"] {{
          background: rgba(255,255,255,.04) !important;
          border-color: rgba(255,255,255,.08) !important;
        }}

        hr {{ border-color: rgba(255,255,255,.08); }}

        @media (max-width: 1200px) {{
          .kpi-grid {{ grid-template-columns: repeat(2, minmax(150px,1fr)); }}
          .hero {{ flex-direction:column; }}
          .trace-grid {{ grid-template-columns:1fr; }}
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def safe_list(table: str, limit: int = 500, filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    try:
        return list_rows(table, limit=limit, filters=filters)
    except Exception as exc:
        st.warning(f"Não foi possível carregar {table}: {exc}")
        return []


def safe_count(table: str, filters: Optional[Dict[str, Any]] = None) -> int:
    return len(safe_list(table, limit=1000, filters=filters))


def norm_status(status: Optional[str]) -> str:
    if not status:
        return "SEM_STATUS"
    return str(status).upper().strip()


def status_label(status: Optional[str]) -> str:
    mapping = {
        "PRONTO_PARA_SHAREPOINT": "Pronto SharePoint",
        "SALVO_HML": "Salvo em HML",
        "SALVO_PROD": "Salvo PROD",
        "AGUARDANDO_APROVACAO": "Pendente",
        "NAO_IDENTIFICADO": "Atenção",
        "ERRO": "Erro",
        "BAIXADO": "Baixado Robô",
        "AGUARDANDO_DOWNLOAD": "Fila Robô",
        "DETECTADO": "Detectado",
        "RECEBIDO": "Recebido",
        "PROCESSADO": "Processado",
    }
    return mapping.get(norm_status(status), str(status or "—"))


def status_class(status: Optional[str]) -> str:
    s = norm_status(status)
    if s in {"SALVO_HML", "SALVO_PROD", "PRONTO_PARA_SHAREPOINT", "PROCESSADO"}:
        return "pill-green"
    if s in {"AGUARDANDO_APROVACAO", "NAO_IDENTIFICADO", "AGUARDANDO_DOWNLOAD", "DETECTADO"}:
        return "pill-yellow"
    if s in {"BAIXADO", "EM_PROCESSAMENTO", "PROCESSANDO"}:
        return "pill-blue"
    if s in {"ERRO", "FALHA"}:
        return "pill-red"
    return "pill-muted"


def pill(text: str, cls: str = "pill-muted") -> str:
    return f'<span class="status-pill {cls}">{text}</span>'


def kpi_card(icon: str, title: str, value: Any, helper: str) -> str:
    return f"""
    <div class="kpi-card">
      <div class="kpi-icon">{icon}</div>
      <div class="kpi-title">{title}</div>
      <div class="kpi-value">{value}</div>
      <div class="kpi-help">{helper}</div>
    </div>
    """


def build_queue_df(files: List[Dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for f in files:
        score = f.get("confidence_score") or f.get("score") or 0
        try:
            score_int = int(float(score))
        except Exception:
            score_int = 0
        saved_path = (
            f.get("sharepoint_suggested_path")
            or f.get("sharepoint_path")
            or f.get("sharepoint_web_url")
            or f.get("local_path")
            or "—"
        )
        project = f.get("project_detected") or f.get("project") or "—"
        discipline = f.get("discipline_detected") or f.get("discipline") or "—"
        rows.append(
            {
                "ARQUIVO": f.get("file_name") or "—",
                "PROJETO": project,
                "DISCIPLINA": discipline,
                "ONDE FOI SALVO": saved_path,
                "SCORE": f"{score_int}%",
                "STATUS": status_label(f.get("status")),
                "ID": f.get("id"),
            }
        )
    return pd.DataFrame(rows)


def render_queue_cards(files: List[Dict[str, Any]]) -> None:
    if not files:
        st.info("Nenhum arquivo encontrado ainda. Quando o n8n enviar e-mails, a fila aparecerá aqui.")
        return

    search = st.text_input("Buscar arquivo, projeto ou disciplina", placeholder="Ex.: ARQ, SAE, ESTRUTURA", key="queue_search")
    statuses = sorted({status_label(f.get("status")) for f in files if f.get("status")})
    selected_status = st.selectbox("Filtrar status", ["Todos os status"] + statuses, key="queue_status")

    filtered = files
    if search:
        s = search.lower()
        filtered = [f for f in filtered if s in str(f).lower()]
    if selected_status != "Todos os status":
        filtered = [f for f in filtered if status_label(f.get("status")) == selected_status]

    header = """
    <div style="display:grid; grid-template-columns: 1.4fr 1fr 1fr 2.2fr .65fr .9fr; gap:12px; padding:10px 12px; color:#A7A0A4; font-size:11px; font-weight:800; letter-spacing:.06em; border-bottom:1px solid rgba(255,255,255,.08);">
      <div>ARQUIVO</div><div>PROJETO</div><div>DISCIPLINA</div><div>ONDE FOI SALVO</div><div>SCORE</div><div>STATUS</div>
    </div>
    """
    html = [header]
    for f in filtered[:12]:
        score = f.get("confidence_score") or 0
        try:
            score_int = max(0, min(100, int(float(score))))
        except Exception:
            score_int = 0
        file_name = f.get("file_name") or "—"
        ext = (f.get("extension") or file_name.split(".")[-1] if "." in file_name else "FILE").upper()
        project = f.get("project_detected") or "—"
        discipline = f.get("discipline_detected") or "—"
        path = f.get("sharepoint_suggested_path") or f.get("sharepoint_path") or f.get("sharepoint_web_url") or "Aguardando mapeamento"
        status = f.get("status")
        html.append(
            f"""
            <div style="display:grid; grid-template-columns: 1.4fr 1fr 1fr 2.2fr .65fr .9fr; gap:12px; align-items:center; padding:12px; border-bottom:1px solid rgba(255,255,255,.06);">
              <div><div style="font-weight:800;color:#fff;">{file_name}</div><div style="font-size:11px;color:#A7A0A4;">{ext}</div></div>
              <div style="font-size:12px;color:#F4F1F2;">{project}</div>
              <div style="font-size:12px;color:#F4F1F2;">{discipline}</div>
              <div style="font-size:12px;color:#F4F1F2; overflow-wrap:anywhere;">{path}</div>
              <div><b style="font-size:13px;color:#fff;">{score_int}%</b><div class="scorebar"><div style="width:{score_int}%"></div></div></div>
              <div>{pill(status_label(status), status_class(status))}</div>
            </div>
            """
        )
    st.markdown("".join(html), unsafe_allow_html=True)
    st.caption(f"Mostrando {min(len(filtered), 12)} de {len(filtered)} itens filtrados.")


def sidebar() -> str:
    with st.sidebar:
        st.markdown(
            """
            <div class="mia-brand">
              <div class="mia-logo">MIA</div>
              <div>
                <div class="mia-title">MIA Autodoc</div>
                <div class="mia-subtitle">Central visual de e-mails, arquivos, robô e SharePoint HML.</div>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        nav = st.radio(
            "Navegação",
            [
                "Dashboard",
                "Inbox Autodoc",
                "Fila de Arquivos",
                "Robô Autodoc",
                "SharePoint HML",
                "Histórico",
                "Mapas de Pastas",
                "Configurações",
            ],
            label_visibility="collapsed",
        )
        st.markdown(
            """
            <div class="side-env">
              <small>AMBIENTE</small>
              <b>HML Seguro</b>
              <span class="dot"></span><span style="color:#65C466;font-size:13px;font-weight:700;">Online</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
    return nav


def render_header() -> None:
    c1, c2 = st.columns([1.65, 1])
    with c1:
        st.markdown(
            """
            <div class="eyebrow">OPERAÇÃO AUTODOC</div>
            <h1 style="margin-bottom:8px;">Painel de Controle</h1>
            <p style="color:#A7A0A4; max-width:820px; line-height:1.55; margin-top:0;">
              Painel central para acompanhar e-mails, parser, filas, robô, SharePoint e rastreabilidade dos arquivos em tempo real.
            </p>
            """,
            unsafe_allow_html=True,
        )
    with c2:
        a, b, c = st.columns(3)
        if a.button("Atualizar", use_container_width=True):
            st.rerun()
        if b.button("Sincronizar Agora", use_container_width=True):
            st.session_state["nav_hint"] = "SharePoint HML"
            st.toast("Abra SharePoint HML para sincronizar o site desejado.")
        if c.button("Preparar Site", use_container_width=True):
            st.session_state["nav_hint"] = "SharePoint HML"
            st.toast("Abra SharePoint HML e execute Preparar Tudo no site selecionado.")


def dashboard_page() -> None:
    render_header()
    files = safe_list("autodoc_files", limit=1000)
    emails = safe_list("autodoc_emails", limit=1000)
    sites = safe_list("autodoc_sharepoint_sites", limit=1000)
    errors = safe_list("autodoc_errors", limit=1000)

    detected = len(files)
    ready = len([f for f in files if norm_status(f.get("status")) in {"PRONTO_PARA_SHAREPOINT", "BAIXADO"}])
    saved = len([f for f in files if norm_status(f.get("status")) == "SALVO_HML"])
    critical = len([f for f in files if norm_status(f.get("status")) in {"ERRO", "NAO_IDENTIFICADO", "AGUARDANDO_APROVACAO"}]) + len(errors)
    active_sites = len([s for s in sites if s.get("active", True)])

    st.markdown(
        f"""
        <div class="kpi-grid">
          {kpi_card('▰', 'Arquivos detectados', detected, 'Novos arquivos identificados')}
          {kpi_card('✓', 'Prontos para SharePoint', ready, 'Aguardando upload ou validação')}
          {kpi_card('⬆', 'Salvos em HML', saved, 'Arquivos finalizados no fluxo')}
          {kpi_card('!', 'Pendências críticas', critical, 'Itens que exigem ação')}
          {kpi_card('◎', 'Sites sincronizados', active_sites, 'Sites ativos no mapa')}
        </div>
        """,
        unsafe_allow_html=True,
    )

    left, right = st.columns([2.25, 1], gap="medium")
    with left:
        st.markdown('<div class="panel"><div class="panel-title"><span class="accent-icon">▱</span> Fila Autodoc</div><div class="panel-desc">Arquivos processados, score, status e destino de salvamento no SharePoint.</div>', unsafe_allow_html=True)
        render_queue_cards(files)
        st.markdown('</div>', unsafe_allow_html=True)

        st.markdown('<div class="panel"><div class="panel-title"><span class="accent-icon">▰</span> Mapa de Sites e Pastas</div><div class="panel-desc">Principais destinos de salvamento no SharePoint HML.</div>', unsafe_allow_html=True)
        maps = safe_list("autodoc_folder_map", limit=8)
        if maps:
            for m in maps[:6]:
                st.markdown(
                    f"""
                    <div class="map-row">
                      <div>
                        <span class="breadcrumb-chip accent">{m.get('project_autodoc','—')}</span>
                        <span class="breadcrumb-chip">Documentos</span>
                        <span class="breadcrumb-chip">{m.get('sharepoint_path','—')}</span>
                      </div>
                      <div style="color:#A7A0A4;font-size:12px;">{m.get('discipline_autodoc','—')}</div>
                      <div>{pill('ativo' if m.get('active', True) else 'inativo', 'pill-green' if m.get('active', True) else 'pill-muted')}</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
        else:
            st.info("Nenhum mapa criado ainda. Use SharePoint HML → Preparar Tudo.")
        st.markdown('</div>', unsafe_allow_html=True)

    with right:
        st.markdown(
            """
            <div class="panel">
              <div class="panel-title"><span class="accent-icon">⌁</span> Fluxo Operacional</div>
              <div class="timeline">
                <div class="step"><div class="step-num">01</div><div><b>E-mail recebido</b><p>Outlook / Microsoft Graph registra o conteúdo.</p></div></div>
                <div class="step"><div class="step-num">02</div><div><b>Parser + Score</b><p>Leitura, classificação e cálculo de confiança.</p></div></div>
                <div class="step"><div class="step-num">03</div><div><b>Fila do robô</b><p>Arquivos sem anexo seguem para download.</p></div></div>
                <div class="step"><div class="step-num">04</div><div><b>Upload local</b><p>Arquivo salvo temporariamente com rastreio.</p></div></div>
                <div class="step"><div class="step-num">05</div><div><b>SharePoint HML</b><p>Upload e organização nas pastas corretas.</p></div></div>
                <div class="step"><div class="step-num">06</div><div><b>Histórico e Auditoria</b><p>Registro completo para consulta futura.</p></div></div>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        selected = files[0] if files else {}
        st.markdown('<div class="panel"><div class="panel-title"><span class="accent-icon">◇</span> Rastreabilidade</div><div class="panel-desc">Detalhes do arquivo selecionado.</div>', unsafe_allow_html=True)
        trace = {
            "Projeto": selected.get("project_detected", "—"),
            "Site SharePoint": selected.get("sharepoint_site_ref", "—"),
            "Biblioteca": "Documentos",
            "Pasta final": selected.get("sharepoint_suggested_path") or selected.get("sharepoint_path") or "—",
            "Arquivo": selected.get("file_name", "—"),
            "Última atualização": selected.get("updated_at", "—"),
            "Responsável": "MIA Autodoc Robô",
            "Status atual": status_label(selected.get("status")),
            "Link / caminho salvo": selected.get("sharepoint_web_url") or selected.get("local_path") or "—",
        }
        html = ['<div class="trace-grid">']
        for k, v in trace.items():
            cls = "trace-value trace-link" if "Link" in k else "trace-value"
            html.append(f'<div class="trace-item"><div class="trace-label">{k}</div><div class="{cls}">{v}</div></div>')
        html.append('</div>')
        st.markdown("".join(html), unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)


def inbox_page() -> None:
    render_header()
    st.markdown('<div class="panel"><div class="panel-title"><span class="accent-icon">✉</span> Inbox Autodoc</div><div class="panel-desc">E-mails recebidos, origem, assunto, pasta e status de processamento.</div>', unsafe_allow_html=True)
    emails = safe_list("autodoc_emails", limit=500)
    st.dataframe(pd.DataFrame(emails), use_container_width=True, hide_index=True)
    st.markdown('</div>', unsafe_allow_html=True)


def files_page() -> None:
    render_header()
    st.markdown('<div class="panel"><div class="panel-title"><span class="accent-icon">▱</span> Fila de Arquivos</div><div class="panel-desc">Lista operacional com destino sugerido, score e status.</div>', unsafe_allow_html=True)
    files = safe_list("autodoc_files", limit=1000)
    render_queue_cards(files)
    with st.expander("Tabela técnica completa"):
        st.dataframe(build_queue_df(files), use_container_width=True, hide_index=True)
    st.markdown('</div>', unsafe_allow_html=True)


def robot_page() -> None:
    render_header()
    st.markdown('<div class="panel"><div class="panel-title"><span class="accent-icon">🤖</span> Robô Autodoc</div><div class="panel-desc">Fila de download, status de execução e arquivos baixados pelo robô.</div>', unsafe_allow_html=True)
    queue = safe_list("autodoc_robot_queue", limit=500)
    st.dataframe(pd.DataFrame(queue), use_container_width=True, hide_index=True)
    st.markdown('</div>', unsafe_allow_html=True)


def sharepoint_page() -> None:
    render_header()
    sites = safe_list("autodoc_sharepoint_sites", limit=1000)

    st.markdown('<div class="panel"><div class="panel-title"><span class="accent-icon">▣</span> SharePoint HML</div><div class="panel-desc">Cadastro, sincronização e preparação automática dos sites e pastas de homologação.</div>', unsafe_allow_html=True)
    with st.form("discover_site_form"):
        c1, c2 = st.columns([1, 1.4])
        with c1:
            project_name = st.text_input("Nome do projeto/obra", placeholder="SAE - GUANÁS")
            aliases = st.text_input("Aliases separados por vírgula", placeholder="SAE GUANAS, GUANÁS, SAE")
        with c2:
            site_url = st.text_input("URL do site SharePoint", placeholder="https://netorg11988711.sharepoint.com/sites/SAE-GUANS")
            library = st.text_input("Biblioteca", value=os.getenv("SHAREPOINT_DEFAULT_LIBRARY_NAME", "Documentos"))
        if st.form_submit_button("Descobrir e cadastrar site automaticamente"):
            try:
                result = discover_site_from_url(project_name, site_url, aliases, library)
                st.success("Site cadastrado com sucesso.")
                st.json(result)
            except Exception as exc:
                st.error(exc)

    if sites:
        site_id = st.selectbox("Selecionar site", [s["id"] for s in sites], format_func=lambda x: next((s.get("project_name") for s in sites if s.get("id") == x), x))
        base = st.text_input("Pasta base para sincronizar", value="")
        c1, c2, c3, c4 = st.columns(4)
        if c1.button("Sincronizar pastas", use_container_width=True):
            try:
                st.json(sync_site_folders(site_id, base))
            except Exception as exc:
                st.error(exc)
        if c2.button("Criar HML", use_container_width=True):
            try:
                st.json(bootstrap_hml_structure(site_id, base))
            except Exception as exc:
                st.error(exc)
        if c3.button("Gerar mapa", use_container_width=True):
            try:
                st.json(generate_folder_map(site_id))
            except Exception as exc:
                st.error(exc)
        if c4.button("Preparar Tudo", use_container_width=True):
            try:
                st.json(prepare_site_hml(site_id, base))
            except Exception as exc:
                st.error(exc)

    st.markdown("<hr/>", unsafe_allow_html=True)
    st.subheader("Sites cadastrados")
    st.dataframe(pd.DataFrame(sites), use_container_width=True, hide_index=True)
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="panel"><div class="panel-title"><span class="accent-icon">▰</span> Inventário de Pastas</div><div class="panel-desc">Pastas lidas e criadas dentro de cada site SharePoint.</div>', unsafe_allow_html=True)
    inv = safe_list("autodoc_sharepoint_inventory", limit=1000)
    st.dataframe(pd.DataFrame(inv), use_container_width=True, hide_index=True)
    st.markdown('</div>', unsafe_allow_html=True)


def history_page() -> None:
    render_header()
    st.markdown('<div class="panel"><div class="panel-title"><span class="accent-icon">◷</span> Histórico</div><div class="panel-desc">Rastreamento completo de e-mails, arquivos, downloads, uploads e obsoletos.</div>', unsafe_allow_html=True)
    hist = safe_list("autodoc_history", limit=1000)
    st.dataframe(pd.DataFrame(hist), use_container_width=True, hide_index=True)
    st.markdown('</div>', unsafe_allow_html=True)


def maps_page() -> None:
    render_header()
    st.markdown('<div class="panel"><div class="panel-title"><span class="accent-icon">▰</span> Mapas de Pastas</div><div class="panel-desc">Regras Projeto + Disciplina → caminho de destino no SharePoint HML.</div>', unsafe_allow_html=True)
    sites = safe_list("autodoc_sharepoint_sites", limit=1000)
    with st.form("folder_map_form"):
        site_ids = [s["id"] for s in sites]
        sp_ref = st.selectbox("Site SharePoint", site_ids, format_func=lambda x: next((s.get("project_name") for s in sites if s.get("id") == x), x)) if site_ids else None
        c1, c2, c3 = st.columns(3)
        project = c1.text_input("Projeto Autodoc")
        discipline = c2.text_input("Disciplina Autodoc", placeholder="ARQUITETURA")
        folder_path = c3.text_input("Pasta destino HML", placeholder="_AUTODOC_HOMOLOGACAO/ARQUITETURA")
        aliases = st.text_input("Aliases da disciplina", placeholder="ARQ, ARQUITETURA, PROJETO ARQUITETONICO")
        exts = st.text_input("Extensões permitidas", value="pdf,dwg")
        if st.form_submit_button("Salvar mapa") and sp_ref:
            try:
                insert_row(
                    "autodoc_folder_map",
                    {
                        "sharepoint_site_ref": sp_ref,
                        "project_autodoc": project,
                        "project_aliases": [x.strip() for x in project.split(",") if x.strip()] if "," in project else [project],
                        "discipline_autodoc": discipline,
                        "discipline_normalized": discipline.upper(),
                        "sharepoint_folder_path": folder_path.split("/")[-1],
                        "sharepoint_path": folder_path,
                        "file_extensions": [x.strip() for x in exts.split(",") if x.strip()],
                        "aliases": [x.strip() for x in aliases.split(",") if x.strip()],
                        "active": True,
                        "environment": "HML",
                        "auto_approve_min_score": 90,
                    },
                )
                st.success("Mapa salvo")
            except Exception as exc:
                st.error(exc)
    st.dataframe(pd.DataFrame(safe_list("autodoc_folder_map", limit=1000)), use_container_width=True, hide_index=True)
    st.markdown('</div>', unsafe_allow_html=True)


def settings_page() -> None:
    render_header()
    st.markdown('<div class="panel"><div class="panel-title"><span class="accent-icon">⚙</span> Configurações</div><div class="panel-desc">Variáveis principais, status do ambiente e erros recentes.</div>', unsafe_allow_html=True)
    env_data = {
        "APP_ENV": os.getenv("APP_ENV", "HML"),
        "ALLOW_PRODUCTION_UPLOAD": os.getenv("ALLOW_PRODUCTION_UPLOAD", "false"),
        "SHAREPOINT_HOSTNAME": os.getenv("SHAREPOINT_HOSTNAME", "—"),
        "SHAREPOINT_HML_ROOT": os.getenv("SHAREPOINT_HML_ROOT", "_AUTODOC_HOMOLOGACAO"),
        "API_BASE_URL": os.getenv("API_BASE_URL", "—"),
    }
    st.json(env_data)
    st.subheader("Erros recentes")
    st.dataframe(pd.DataFrame(safe_list("autodoc_errors", limit=500)), use_container_width=True, hide_index=True)
    st.markdown('</div>', unsafe_allow_html=True)


def main() -> None:
    inject_css()
    nav = sidebar()

    if nav == "Dashboard":
        dashboard_page()
    elif nav == "Inbox Autodoc":
        inbox_page()
    elif nav == "Fila de Arquivos":
        files_page()
    elif nav == "Robô Autodoc":
        robot_page()
    elif nav == "SharePoint HML":
        sharepoint_page()
    elif nav == "Histórico":
        history_page()
    elif nav == "Mapas de Pastas":
        maps_page()
    elif nav == "Configurações":
        settings_page()


if __name__ == "__main__":
    main()
