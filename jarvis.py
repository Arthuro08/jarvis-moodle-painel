"""
JARVIS-MOODLE — Assistente Acadêmico Automatizado (IFSP São Paulo)
==================================================================
Integra-se ao Moodle do IFSP (eadcampus.spo.ifsp.edu.br) via REST API oficial
e Playwright (OAuth2 SUAP), sincronizando disciplinas, prazos, notas, PDFs
e gerando automaticamente um cofre Obsidian estruturado.

Comandos disponíveis:
  python jarvis.py sync                              -> Sincroniza tudo do Moodle e atualiza o Obsidian
  python jarvis.py prazos                            -> Exibe radar de prazos e entregas no terminal
  python jarvis.py baixar [--curso ID]               -> Baixa todos os PDFs e materiais das disciplinas
  python jarvis.py ler-pdf "<caminho_pdf>"           -> Extrai e exibe o texto completo de um PDF de atividade
  python jarvis.py entregar --assign-id ID ...       -> Envia arquivo/texto para uma tarefa no Moodle
  python jarvis.py auth                              -> Renova o token do Moodle via login automático no SUAP
"""

import argparse
import base64
import datetime
import html
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

# Garante saída UTF-8 no terminal Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
SESSION_STATE_PATH = BASE_DIR / ".session_state.json"
CACHE_PATH = BASE_DIR / "moodle_cache.json"
VAULT_DIR = BASE_DIR / "Obsidian-Vault"
MATERIAIS_DIR = VAULT_DIR / "04_Materiais_e_PDFs"

MOODLE_BASE_URL = "https://eadcampus.spo.ifsp.edu.br"
MOODLE_API_URL = f"{MOODLE_BASE_URL}/webservice/rest/server.php"
MOODLE_UPLOAD_URL = f"{MOODLE_BASE_URL}/webservice/upload.php"


# ==============================================================================
# UTILITÁRIOS DE CONFIGURAÇÃO E FORMATAÇÃO
# ==============================================================================

BRT_TZ = datetime.timezone(datetime.timedelta(hours=-3))


def carregar_env() -> Dict[str, str]:
    env: Dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line_s = line.strip()
            if "=" in line_s and not line_s.startswith("#"):
                k, v = line_s.split("=", 1)
                env[k.strip()] = v.strip()
    for k in ("MOODLE_TOKEN", "MOODLE_USERID", "USER", "SUAP", "GITHUB_TOKEN"):
        if os.environ.get(k) and not env.get(k):
            env[k] = os.environ[k].strip()
    return env


def salvar_chave_env(chave: str, valor: str) -> None:
    linhas = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    encontrou = False
    novas_linhas = []
    for linha in linhas:
        if linha.strip().startswith(f"{chave}="):
            novas_linhas.append(f"{chave}={valor}")
            encontrou = True
        else:
            novas_linhas.append(linha)
    if not encontrou:
        novas_linhas.append(f"{chave}={valor}")
    ENV_PATH.write_text("\n".join(novas_linhas) + "\n", encoding="utf-8")


def limpar_html(texto_html: Optional[str]) -> str:
    if not texto_html:
        return ""
    soup = BeautifulSoup(texto_html, "html.parser")
    texto = soup.get_text(separator="\n")
    linhas = [re.sub(r"\s+", " ", ln).strip() for ln in texto.splitlines()]
    return "\n".join(ln for ln in linhas if ln)


def sanitizar_nome_arquivo(nome: str) -> str:
    limpo = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", nome)
    limpo = re.sub(r"\s+", " ", limpo).strip(" .")
    return limpo[:110]


def formatar_timestamp(ts: Optional[int]) -> str:
    if not ts or ts <= 0:
        return "Sem prazo definido"
    dt = datetime.datetime.fromtimestamp(ts, tz=BRT_TZ)
    return dt.strftime("%d/%m/%Y às %H:%M")


def calcular_status_prazo(ts: Optional[int], entregue: bool) -> str:
    if entregue:
        return "✅ Entregue"
    if not ts or ts <= 0:
        return "⚪ Sem prazo"
    agora = datetime.datetime.now().timestamp()
    diff_horas = (ts - agora) / 3600.0
    if diff_horas < 0:
        dias_atraso = int(abs(diff_horas) // 24)
        return f"⚫ Encerrado / Atrasado ({dias_atraso}d atrás)"
    elif diff_horas <= 48:
        return f"🔴 URGENTE ({int(diff_horas)}h restantes)"
    elif diff_horas <= 168:
        return f"🟡 Esta Semana ({int(diff_horas // 24)}d restantes)"
    else:
        return f"🟢 No Prazo ({int(diff_horas // 24)}d restantes)"


# ==============================================================================
# AUTENTICAÇÃO E CLIENTE DA API MOODLE
# ==============================================================================

def renovar_autenticacao_suap() -> str:
    """Realiza login via Playwright no OAuth2 do SUAP e extrai o wstoken oficial."""
    from playwright.sync_api import sync_playwright

    env = carregar_env()
    user = env.get("USER", "")
    suap_pwd = env.get("SUAP", "")
    if not user or not suap_pwd:
        raise RuntimeError("Credenciais USER e SUAP não encontradas no arquivo .env!")

    print("🔐 Iniciando autenticação no Moodle IFSP via SUAP OAuth2...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome",
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        page.goto(f"{MOODLE_BASE_URL}/login/index.php")

        suap_btn = page.locator('a[href*="oauth2/login.php?id=1"]')
        suap_btn.click()
        page.wait_for_load_state("networkidle")

        if "suap.ifsp.edu.br" in page.url:
            page.fill('input[name="username"]', user)
            page.fill('input[name="password"]', suap_pwd)
            page.wait_for_timeout(1500)

            submit_btn = page.locator('input[type="submit"], button[type="submit"]').first
            submit_btn.click()

            page.wait_for_url(
                lambda url: "eadcampus.spo.ifsp.edu.br" in url and "oauth2" not in url and "login" not in url,
                timeout=25000,
            )
            page.wait_for_load_state("networkidle")

        context.storage_state(path=str(SESSION_STATE_PATH))
        html_content = page.content()
        userid_m = re.search(r'"userid":(\d+)', html_content)
        userid = userid_m.group(1) if userid_m else env.get("MOODLE_USERID", "37843")
        browser.close()

    # Extrai token via mobile launch usando os cookies recém-salvos
    state = json.loads(SESSION_STATE_PATH.read_text(encoding="utf-8"))
    s = requests.Session()
    for c in state["cookies"]:
        s.cookies.set(c["name"], c["value"], domain=c["domain"])

    r = s.get(
        f"{MOODLE_BASE_URL}/admin/tool/mobile/launch.php?service=moodle_mobile_app&passport=12345&urlscheme=moodlemobile",
        allow_redirects=False,
    )
    loc = r.headers.get("Location", "")
    if "token=" not in loc:
        raise RuntimeError("Não foi possível extrair o token do endpoint mobile launch.")

    b64 = loc.split("token=")[1].split("&")[0]
    parts = base64.b64decode(b64).decode("utf-8").split(":::")
    wstoken = parts[1]

    salvar_chave_env("MOODLE_TOKEN", wstoken)
    salvar_chave_env("MOODLE_USERID", str(userid))
    print(f"✅ Autenticação concluída! UserID={userid} | Token atualizado no .env.")
    return wstoken


class MoodleClient:
    def __init__(self) -> None:
        env = carregar_env()
        self.token = env.get("MOODLE_TOKEN", "")
        self.userid = int(env.get("MOODLE_USERID", "37843"))
        if not self.token:
            self.token = renovar_autenticacao_suap()

    def call(self, wsfunction: str, **params: Any) -> Any:
        payload = {
            "wstoken": self.token,
            "wsfunction": wsfunction,
            "moodlewsrestformat": "json",
        }
        payload.update(params)
        r = requests.post(MOODLE_API_URL, data=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("errorcode") == "invalidtoken":
            print("🔄 Token expirado. Renovando automaticamente via SUAP...")
            self.token = renovar_autenticacao_suap()
            payload["wstoken"] = self.token
            r = requests.post(MOODLE_API_URL, data=payload, timeout=30)
            data = r.json()
        return data

    def baixar_arquivo(self, fileurl: str, destino: Path) -> bool:
        destino.parent.mkdir(parents=True, exist_ok=True)
        sep = "&" if "?" in fileurl else "?"
        url_com_token = f"{fileurl}{sep}token={self.token}"
        r = requests.get(url_com_token, stream=True, timeout=60)
        if r.status_code == 200:
            with open(destino, "wb") as f:
                for chunk in r.iter_content(chunk_size=16384):
                    if chunk:
                        f.write(chunk)
            return True
        return False

    def upload_draft_file(self, caminho_arquivo: Path, itemid: int = 0) -> Dict[str, Any]:
        if not caminho_arquivo.exists():
            raise FileNotFoundError(f"Arquivo não encontrado: {caminho_arquivo}")
        params = {
            "token": self.token,
            "filearea": "draft",
            "itemid": itemid,
        }
        with open(caminho_arquivo, "rb") as f:
            files = {"file_box": (caminho_arquivo.name, f)}
            r = requests.post(MOODLE_UPLOAD_URL, params=params, files=files, timeout=60)
            r.raise_for_status()
            res = r.json()
            if isinstance(res, list) and len(res) > 0:
                return res[0]
            raise RuntimeError(f"Erro no upload para área de rascunho: {res}")


# ==============================================================================
# COLETA COMPLETA E CONSTRUÇÃO DO COFRE OBSIDIAN
# ==============================================================================

def sincronizar_moodle(baixar_pdfs: bool = True) -> Dict[str, Any]:
    client = MoodleClient()
    print("📡 Consultando cursos matriculados no Moodle IFSP...")
    cursos = client.call("core_enrol_get_users_courses", userid=client.userid)

    print("📋 Consultando todas as tarefas (assignments) e prazos...")
    assign_data = client.call("mod_assign_get_assignments")
    cursos_assign_map: Dict[int, List[Dict[str, Any]]] = {}
    if isinstance(assign_data, dict) and "courses" in assign_data:
        for c_obj in assign_data["courses"]:
            cursos_assign_map[c_obj["id"]] = c_obj.get("assignments", [])

    print("🧪 Consultando questionários (quizzes)...")
    quizzes_data = client.call("mod_quiz_get_quizzes_by_courses")
    cursos_quiz_map: Dict[int, List[Dict[str, Any]]] = {}
    if isinstance(quizzes_data, dict) and "quizzes" in quizzes_data:
        for q_obj in quizzes_data["quizzes"]:
            cid = q_obj.get("course")
            cursos_quiz_map.setdefault(cid, []).append(q_obj)

    dados_consolidados: Dict[str, Any] = {
        "ultima_sincronizacao": datetime.datetime.now(tz=BRT_TZ).strftime("%d/%m/%Y às %H:%M:%S"),
        "userid": client.userid,
        "cursos": [],
        "todas_tarefas": [],
        "todos_pdfs": [],
    }

    for curso in cursos:
        cid = curso["id"]
        nome_curso = curso.get("fullname", f"Curso {cid}")
        shortname = curso.get("shortname", str(cid))
        pasta_nome = sanitizar_nome_arquivo(nome_curso)
        print(f"  ↳ Processando disciplina: [{cid}] {nome_curso}...")

        # 1. Conteúdos e PDFs da disciplina
        secoes_raw = client.call("core_course_get_contents", courseid=cid)
        secoes_limpas = []
        pdfs_curso = []

        if isinstance(secoes_raw, list):
            for sec in secoes_raw:
                sec_nome = sec.get("name") or f"Seção {sec.get('section', '')}"
                sec_resumo = limpar_html(sec.get("summary", ""))
                modulos_limpos = []

                for mod in sec.get("modules", []):
                    mod_name = mod.get("name", "Sem título")
                    mod_type = mod.get("modname", "")
                    mod_url = mod.get("url", "")
                    mod_desc = limpar_html(mod.get("description", ""))
                    arquivos_mod = []

                    for content in mod.get("contents", []):
                        fname = content.get("filename", "")
                        furl = content.get("fileurl", "")
                        fmime = content.get("mimetype", "")
                        eh_pdf_ou_doc = (
                            fname.lower().endswith((".pdf", ".pptx", ".ppt", ".docx", ".doc", ".zip", ".sql", ".py", ".c"))
                            or "pdf" in fmime.lower()
                        )
                        caminho_relativo_vault = None
                        if eh_pdf_ou_doc and furl:
                            nome_arq_seguro = sanitizar_nome_arquivo(fname)
                            destino_local = MATERIAIS_DIR / pasta_nome / nome_arq_seguro
                            if baixar_pdfs:
                                if not destino_local.exists():
                                    ok = client.baixar_arquivo(furl, destino_local)
                                    if ok:
                                        print(f"     📥 Baixado: {nome_arq_seguro}")
                            if destino_local.exists():
                                caminho_relativo_vault = f"04_Materiais_e_PDFs/{pasta_nome}/{nome_arq_seguro}"
                            info_arq = {
                                "curso_id": cid,
                                "curso_nome": nome_curso,
                                "secao": sec_nome,
                                "modulo": mod_name,
                                "filename": nome_arq_seguro,
                                "fileurl": furl,
                                "caminho_local": str(destino_local),
                                "caminho_vault": caminho_relativo_vault,
                            }
                            arquivos_mod.append(info_arq)
                            pdfs_curso.append(info_arq)
                            dados_consolidados["todos_pdfs"].append(info_arq)

                    modulos_limpos.append({
                        "id": mod.get("id"),
                        "instance": mod.get("instance"),
                        "name": mod_name,
                        "modname": mod_type,
                        "url": mod_url,
                        "description": mod_desc,
                        "arquivos": arquivos_mod,
                    })

                if sec_resumo or modulos_limpos:
                    secoes_limpas.append({
                        "name": sec_nome,
                        "summary": sec_resumo,
                        "modules": modulos_limpos,
                    })

        # 2. Tarefas (Assignments) e Status de Entrega
        tarefas_curso = []
        for assign in cursos_assign_map.get(cid, []):
            aid = assign["id"]
            cmid = assign.get("cmid")
            aname = assign.get("name", f"Tarefa {aid}")
            duedate = assign.get("duedate", 0)
            cutoffdate = assign.get("cutoffdate", 0)
            intro = limpar_html(assign.get("intro", ""))
            submissiondrafts = assign.get("submissiondrafts", 0)

            # Anexos da própria tarefa (ex: PDF de enunciado)
            anexos_tarefa = []
            for att in assign.get("introattachments", []):
                fname = sanitizar_nome_arquivo(att.get("filename", "anexo.pdf"))
                furl = att.get("fileurl", "")
                destino_local = MATERIAIS_DIR / pasta_nome / f"Tarefa_{aid}_{fname}"
                if baixar_pdfs and furl and not destino_local.exists():
                    if client.baixar_arquivo(furl, destino_local):
                        print(f"     📥 Enunciado baixado: Tarefa_{aid}_{fname}")
                anexos_tarefa.append({
                    "filename": f"Tarefa_{aid}_{fname}",
                    "caminho_local": str(destino_local),
                    "caminho_vault": f"04_Materiais_e_PDFs/{pasta_nome}/Tarefa_{aid}_{fname}" if destino_local.exists() else None,
                })

            # Consulta status de envio do Arthur
            entregue = False
            canedit = cutoffdate == 0 or cutoffdate > datetime.datetime.now().timestamp()
            status_envio_raw = "new"
            status_avaliacao = "Não avaliado"
            arquivos_enviados = []
            try:
                st_resp = client.call("mod_assign_get_submission_status", assignid=aid, userid=client.userid)
                if isinstance(st_resp, dict) and "lastattempt" in st_resp:
                    la = st_resp["lastattempt"]
                    canedit = bool(la.get("canedit", canedit))
                    sub = la.get("submission", {})
                    status_envio_raw = sub.get("status", "new")
                    entregue = status_envio_raw == "submitted"
                    grading_st = la.get("gradingstatus", "")
                    status_avaliacao = "Avaliado" if grading_st == "graded" else "Pendente de avaliação"
                    for plugin in sub.get("plugins", []):
                        for farea in plugin.get("fileareas", []):
                            for f_env in farea.get("files", []):
                                arquivos_enviados.append(f_env.get("filename", ""))
            except Exception:
                pass

            t_obj = {
                "assign_id": aid,
                "cmid": cmid,
                "curso_id": cid,
                "curso_nome": nome_curso,
                "nome": aname,
                "duedate": duedate,
                "cutoffdate": cutoffdate,
                "duedate_fmt": formatar_timestamp(duedate),
                "cutoffdate_fmt": formatar_timestamp(cutoffdate),
                "entregue": entregue,
                "canedit": canedit,
                "status_envio_raw": status_envio_raw,
                "status_prazo": calcular_status_prazo(duedate, entregue),
                "status_avaliacao": status_avaliacao,
                "arquivos_enviados": arquivos_enviados,
                "submissiondrafts": submissiondrafts,
                "intro": intro,
                "anexos": anexos_tarefa,
                "url": f"{MOODLE_BASE_URL}/mod/assign/view.php?id={cmid}" if cmid else "",
            }
            tarefas_curso.append(t_obj)
            dados_consolidados["todas_tarefas"].append(t_obj)

        # 3. Notas (Gradebook)
        notas_curso = []
        try:
            grades_resp = client.call("gradereport_user_get_grade_items", courseid=cid, userid=client.userid)
            if isinstance(grades_resp, dict) and "usergrades" in grades_resp:
                for ug in grades_resp["usergrades"]:
                    for item in ug.get("gradeitems", []):
                        iname = item.get("itemname")
                        if item.get("itemtype") == "course":
                            iname = "📊 MÉDIA / TOTAL DO CURSO"
                        if not iname:
                            continue
                        notas_curso.append({
                            "itemname": iname,
                            "gradeformatted": item.get("gradeformatted", "-"),
                            "grademax": item.get("grademax", 10),
                            "percentage": item.get("percentageformatted", "-"),
                            "feedback": limpar_html(item.get("feedback", "")),
                        })
        except Exception:
            pass

        dados_consolidados["cursos"].append({
            "id": cid,
            "fullname": nome_curso,
            "shortname": shortname,
            "pasta_nome": pasta_nome,
            "secoes": secoes_limpas,
            "tarefas": tarefas_curso,
            "quizzes": cursos_quiz_map.get(cid, []),
            "notas": notas_curso,
            "pdfs": pdfs_curso,
        })

    CACHE_PATH.write_text(json.dumps(dados_consolidados, ensure_ascii=False, indent=2), encoding="utf-8")
    gerar_cofre_obsidian(dados_consolidados)
    return dados_consolidados


# ==============================================================================
# GERADOR DO COFRE OBSIDIAN (MARKDOWN ESTRUTURADO)
# ==============================================================================

def gerar_cofre_obsidian(dados: Dict[str, Any]) -> None:
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    disc_dir = VAULT_DIR / "03_Disciplinas"
    disc_dir.mkdir(parents=True, exist_ok=True)
    MATERIAIS_DIR.mkdir(parents=True, exist_ok=True)

    agora = datetime.datetime.now().timestamp()
    todas_tarefas = sorted(
        dados["todas_tarefas"],
        key=lambda x: (x["duedate"] if x["duedate"] > 0 else 9999999999),
    )

    pendentes_ativas = [
        t for t in todas_tarefas
        if not t["entregue"] and (t["duedate"] == 0 or t["duedate"] >= agora - 14 * 86400)
    ]
    entregues = [t for t in todas_tarefas if t["entregue"]]

    # --------------------------------------------------------------------------
    # 1. Nota Principal: 00_Painel_Jarvis.md
    # --------------------------------------------------------------------------
    painel_md = [
        "# 🧠 Painel Central — Jarvis do Moodle (IFSP SPO)",
        f"> [!INFO] Sincronizado automaticamente em **{dados['ultima_sincronizacao']}**",
        "> **Aluno:** Arthur Gomes Almeida (`SP3289362`) | **Curso:** Bacharelado em Sistemas de Informação — IFSP São Paulo",
        "",
        "## ⚡ Navegação Rápida",
        "- 📅 [[01_Radar_de_Prazos_e_Entregas|Radar de Prazos e Entregas]]",
        "- 📊 [[02_Boletim_e_Notas|Boletim e Notas Consolidadas]]",
        "",
        "---",
        "## 🚨 Radar de Atenção Imediata (Pendentes & Recentes)",
    ]

    if not pendentes_ativas:
        painel_md.append("> [!SUCCESS] Nenhuma atividade pendente com prazo próximo! Tudo em dia.")
    else:
        painel_md.append("| Status | Prazo | Disciplina | Atividade | Assign ID |")
        painel_md.append("| :--- | :--- | :--- | :--- | :--- |")
        for t in pendentes_ativas:
            nota_disc = f"[[03_Disciplinas/{t['curso_id']}_{sanitizar_nome_arquivo(t['curso_nome'])}|{t['curso_nome']}]]"
            painel_md.append(
                f"| {t['status_prazo']} | **{t['duedate_fmt']}** | {nota_disc} | [{t['nome']}]({t['url']}) | `{t['assign_id']}` |"
            )

    painel_md.extend([
        "",
        "---",
        "## 📚 Minhas Disciplinas no Moodle",
        "| ID | Disciplina | Tarefas | PDFs/Materiais | Nota no Obsidian |",
        "| :--- | :--- | :---: | :---: | :--- |",
    ])

    for c in dados["cursos"]:
        nome_nota = f"{c['id']}_{c['pasta_nome']}"
        painel_md.append(
            f"| `{c['id']}` | **{c['fullname']}** | {len(c['tarefas'])} | {len(c['pdfs'])} | [[03_Disciplinas/{nome_nota}|Abrir Disciplina]] |"
        )

    painel_md.extend([
        "",
        "---",
        "## 🛠️ Comandos Rápidos do Jarvis (Terminal)",
        "```powershell",
        "# 1. Sincronizar tudo do Moodle e atualizar este Obsidian",
        "python jarvis.py sync",
        "",
        "# 2. Ver radar de prazos no terminal",
        "python jarvis.py prazos",
        "",
        "# 3. Ler o conteúdo de um PDF de atividade",
        'python jarvis.py ler-pdf "caminho/do/arquivo.pdf"',
        "",
        "# 4. Entregar uma tarefa no Moodle via API",
        'python jarvis.py entregar --assign-id <ID> --arquivo "caminho/da/resposta.pdf" --finalizar',
        "```",
    ])
    (VAULT_DIR / "00_Painel_Jarvis.md").write_text("\n".join(painel_md), encoding="utf-8")

    # --------------------------------------------------------------------------
    # 2. Nota de Prazos: 01_Radar_de_Prazos_e_Entregas.md
    # --------------------------------------------------------------------------
    prazos_md = [
        "# 📅 Radar de Prazos e Entregas",
        f"Última atualização: **{dados['ultima_sincronizacao']}** | Voltar para [[00_Painel_Jarvis|Painel Central]]",
        "",
        "## 🎯 Tarefas Pendentes / Ativas",
    ]
    if not pendentes_ativas:
        prazos_md.append("_Nenhuma tarefa pendente no momento._")
    else:
        for t in pendentes_ativas:
            prazos_md.extend([
                f"### {t['status_prazo']} — {t['nome']}",
                f"- **Disciplina:** {t['curso_nome']} (`Curso {t['curso_id']}`)",
                f"- **Prazo de Entrega:** **{t['duedate_fmt']}**",
                f"- **Assign ID (para envio via Jarvis):** `{t['assign_id']}` | [Abrir no Moodle]({t['url']})",
            ])
            if t["anexos"]:
                links_anexos = [
                    f"[[{a['caminho_vault']}|{a['filename']}]]" if a["caminho_vault"] else a["filename"]
                    for a in t["anexos"]
                ]
                prazos_md.append(f"- **Arquivos de Enunciado:** {', '.join(links_anexos)}")
            if t["intro"]:
                prazos_md.append(f"\n> [!NOTE] Descrição / Enunciado\n> " + "\n> ".join(t["intro"].splitlines()[:15]))
            prazos_md.append("")

    prazos_md.extend([
        "---",
        "## ✅ Tarefas Já Entregues",
        "| Disciplina | Atividade | Prazo | Arquivos Enviados | Avaliação |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ])
    for t in reversed(entregues):
        arqs = ", ".join(t["arquivos_enviados"]) if t["arquivos_enviados"] else "Texto online / Entregue"
        prazos_md.append(
            f"| {t['curso_nome']} | [{t['nome']}]({t['url']}) (`{t['assign_id']}`) | {t['duedate_fmt']} | `{arqs}` | {t['status_avaliacao']} |"
        )

    prazos_md.extend([
        "",
        "---",
        "## 📦 Histórico Completo de Todas as Tarefas",
        "| Status | Assign ID | Disciplina | Atividade | Prazo |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ])
    for t in reversed(todas_tarefas):
        prazos_md.append(
            f"| {t['status_prazo']} | `{t['assign_id']}` | {t['curso_nome']} | [{t['nome']}]({t['url']}) | {t['duedate_fmt']} |"
        )

    (VAULT_DIR / "01_Radar_de_Prazos_e_Entregas.md").write_text("\n".join(prazos_md), encoding="utf-8")

    # --------------------------------------------------------------------------
    # 3. Nota de Notas: 02_Boletim_e_Notas.md
    # --------------------------------------------------------------------------
    notas_md = [
        "# 📊 Boletim e Notas Consolidadas",
        f"Última atualização: **{dados['ultima_sincronizacao']}** | Voltar para [[00_Painel_Jarvis|Painel Central]]",
        "",
    ]
    for c in dados["cursos"]:
        if not c["notas"]:
            continue
        notas_md.extend([
            f"## 📘 {c['fullname']}",
            "| Item de Avaliação | Nota | Máximo | Porcentagem | Feedback |",
            "| :--- | :---: | :---: | :---: | :--- |",
        ])
        for n in c["notas"]:
            fb = n["feedback"].replace("\n", " ")[:80] if n["feedback"] else "-"
            notas_md.append(
                f"| {n['itemname']} | **{n['gradeformatted']}** | {n['grademax']} | {n['percentage']} | {fb} |"
            )
        notas_md.append("")

    (VAULT_DIR / "02_Boletim_e_Notas.md").write_text("\n".join(notas_md), encoding="utf-8")

    # --------------------------------------------------------------------------
    # 4. Notas Individuais por Disciplina em 03_Disciplinas/
    # --------------------------------------------------------------------------
    for c in dados["cursos"]:
        nome_nota = f"{c['id']}_{c['pasta_nome']}.md"
        c_md = [
            f"# 📘 {c['fullname']}",
            f"**ID do Curso:** `{c['id']}` | **Código:** `{c['shortname']}` | Voltar para [[00_Painel_Jarvis|Painel Central]]",
            "",
            "---",
            "## 📝 Tarefas e Entregas da Disciplina",
        ]
        if not c["tarefas"]:
            c_md.append("_Nenhuma tarefa do tipo Assignment registrada nesta disciplina._")
        else:
            c_md.append("| Status | Assign ID | Tarefa | Prazo | Arquivos Enviados |")
            c_md.append("| :--- | :--- | :--- | :--- | :--- |")
            for t in c["tarefas"]:
                arqs = ", ".join(t["arquivos_enviados"]) if t["arquivos_enviados"] else "-"
                c_md.append(
                    f"| {t['status_prazo']} | `{t['assign_id']}` | [{t['nome']}]({t['url']}) | {t['duedate_fmt']} | {arqs} |"
                )
            c_md.append("")
            for t in c["tarefas"]:
                c_md.append(f"### 📌 [{t['assign_id']}] {t['nome']} ({t['status_prazo']})")
                c_md.append(f"- **Prazo:** {t['duedate_fmt']} | [Link no Moodle]({t['url']})")
                if t["anexos"]:
                    for a in t["anexos"]:
                        if a["caminho_vault"]:
                            c_md.append(f"- **Enunciado Baixado:** [[{a['caminho_vault']}|{a['filename']}]]")
                if t["intro"]:
                    c_md.append(f"- **Instruções:**\n  > " + "\n  > ".join(t["intro"].splitlines()))
                c_md.append("")

        c_md.extend([
            "---",
            "## 📂 Materiais e PDFs Baixados",
        ])
        if not c["pdfs"]:
            c_md.append("_Nenhum PDF/arquivo baixado nesta disciplina._")
        else:
            for pdf in c["pdfs"]:
                link_obs = f"[[{pdf['caminho_vault']}|{pdf['filename']}]]" if pdf["caminho_vault"] else pdf["filename"]
                c_md.append(f"- 📄 {link_obs} _(Seção: {pdf['secao']} — {pdf['modulo']})_")

        c_md.extend([
            "",
            "---",
            "## 🗂️ Estrutura Completa de Tópicos e Aulas",
        ])
        for sec in c["secoes"]:
            c_md.append(f"### 🔹 {sec['name']}")
            if sec["summary"]:
                c_md.append(f"> {sec['summary']}")
            for m in sec["modules"]:
                mod_line = f"- **[{m['modname'].upper()}]** {m['name']}"
                if m["url"]:
                    mod_line += f" ([Abrir no Moodle]({m['url']}))"
                c_md.append(mod_line)
                if m["description"]:
                    c_md.append(f"  - _{m['description'][:300]}_")
                for arq in m["arquivos"]:
                    if arq["caminho_vault"]:
                        c_md.append(f"  - 📎 Arquivo Local: [[{arq['caminho_vault']}|{arq['filename']}]]")
            c_md.append("")

        (disc_dir / nome_nota).write_text("\n".join(c_md), encoding="utf-8")

    print(f"✨ Cofre Obsidian atualizado com sucesso em: {VAULT_DIR}")
    gerar_painel_mobile_html(dados)


def gerar_painel_mobile_html(dados: Dict[str, Any]) -> None:
    """Gera um Dashboard Mobile HTML single-file no OneDrive para acesso rápido no Android/PC."""
    payload_mobile = {
        "ultima_sincronizacao": dados.get("ultima_sincronizacao", ""),
        "todas_tarefas": dados.get("todas_tarefas", []),
        "cursos": [
            {
                "id": c.get("id"),
                "fullname": c.get("fullname"),
                "shortname": c.get("shortname"),
                "tarefas_count": len(c.get("tarefas", [])),
                "pdfs_count": len(c.get("pdfs", [])),
                "notas": c.get("notas", []),
                "pdfs": [
                    {
                        "filename": p.get("filename"),
                        "secao": p.get("secao"),
                        "modulo": p.get("modulo"),
                    }
                    for p in c.get("pdfs", [])
                ],
            }
            for c in dados.get("cursos", [])
        ],
    }
    json_inline = json.dumps(payload_mobile, ensure_ascii=False).replace("</", "<\\/")

    html_template = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
  <meta name="theme-color" content="#0b1120" />
  <meta name="mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 128 128'%3E%3Crect width='128' height='128' rx='28' fill='%230b1120'/%3E%3Ctext x='64' y='84' font-size='64' text-anchor='middle'%3E%F0%9F%A7%A0%3C/text%3E%3C/svg%3E" />
  <script>if(location.protocol.startsWith('http')){var m=document.createElement('link');m.rel='manifest';m.href='manifest.json';document.head.appendChild(m);}</script>
  <title>Jarvis do Moodle — IFSP SPO</title>
  <style>
    :root {
      --bg: #0b1120;
      --card: #151f32;
      --card-hover: #1e293b;
      --border: #26354d;
      --text: #f1f5f9;
      --muted: #94a3b8;
      --accent: #38bdf8;
      --urgent: #ef4444;
      --urgent-bg: rgba(239, 68, 68, 0.14);
      --warn: #f59e0b;
      --warn-bg: rgba(245, 158, 11, 0.14);
      --ok: #10b981;
      --ok-bg: rgba(16, 185, 129, 0.14);
      --neutral: #64748b;
      --neutral-bg: rgba(100, 116, 139, 0.16);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      padding-bottom: 84px;
      line-height: 1.45;
    }
    header {
      position: sticky;
      top: 0;
      z-index: 20;
      background: rgba(11, 17, 32, 0.92);
      backdrop-filter: blur(12px);
      border-bottom: 1px solid var(--border);
      padding: 14px 16px 12px;
    }
    .header-top {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .brand-icon {
      width: 36px;
      height: 36px;
      border-radius: 10px;
      background: linear-gradient(135deg, #0284c7, #38bdf8);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 19px;
      box-shadow: 0 4px 12px rgba(56, 189, 248, 0.25);
    }
    .brand h1 { font-size: 16px; font-weight: 700; letter-spacing: -0.2px; }
    .brand p { font-size: 11px; color: var(--muted); }
    .sync-pill {
      font-size: 11px;
      color: var(--accent);
      background: rgba(56, 189, 248, 0.12);
      border: 1px solid rgba(56, 189, 248, 0.25);
      padding: 4px 9px;
      border-radius: 999px;
      white-space: nowrap;
    }
    .container { max-width: 760px; margin: 0 auto; padding: 14px 14px 24px; }
    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 8px;
      margin-bottom: 14px;
    }
    .kpi {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 10px 8px;
      text-align: center;
      cursor: pointer;
      transition: transform 0.15s, border-color 0.15s, background 0.15s, box-shadow 0.15s;
      user-select: none;
    }
    .kpi:active { transform: scale(0.97); }
    .kpi.active {
      border-color: var(--accent);
      background: rgba(56, 189, 248, 0.12);
      box-shadow: 0 0 0 1px var(--accent);
    }
    .kpi-num { font-size: 20px; font-weight: 800; line-height: 1.1; }
    .kpi-label { font-size: 10px; color: var(--muted); margin-top: 4px; text-transform: uppercase; letter-spacing: 0.3px; }
    .search-wrap {
      position: relative;
      margin-bottom: 10px;
    }
    .search-bar {
      width: 100%;
      background: var(--card);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 11px 36px 11px 14px;
      border-radius: 12px;
      font-size: 14px;
      outline: none;
    }
    .search-bar:focus { border-color: var(--accent); }
    .clear-search {
      position: absolute;
      right: 10px;
      top: 50%;
      transform: translateY(-50%);
      background: rgba(255,255,255,0.08);
      border: none;
      color: var(--muted);
      width: 24px;
      height: 24px;
      border-radius: 999px;
      font-size: 12px;
      cursor: pointer;
      display: none;
      align-items: center;
      justify-content: center;
    }
    .clear-search.visible { display: inline-flex; }
    .results-bar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 10px;
      padding: 0 4px;
    }
    .filters {
      display: flex;
      gap: 6px;
      overflow-x: auto;
      padding-bottom: 6px;
      margin-bottom: 10px;
      scrollbar-width: none;
    }
    .filters::-webkit-scrollbar { display: none; }
    .chip {
      border: 1px solid var(--border);
      background: var(--card);
      color: var(--muted);
      padding: 7px 12px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 600;
      white-space: nowrap;
      cursor: pointer;
    }
    .chip.active {
      background: var(--accent);
      color: #082f49;
      border-color: var(--accent);
    }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 14px;
      margin-bottom: 10px;
      transition: border-color 0.15s, background 0.15s;
    }
    .clickable-card { cursor: pointer; }
    .clickable-card:hover { border-color: rgba(56, 189, 248, 0.45); }
    .card-urgent { border-left: 4px solid var(--urgent); }
    .card-warn { border-left: 4px solid var(--warn); }
    .card-ok { border-left: 4px solid var(--ok); }
    .card-neutral { border-left: 4px solid var(--neutral); }
    .card-top {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 8px;
      margin-bottom: 6px;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      font-size: 11px;
      font-weight: 700;
      padding: 3px 8px;
      border-radius: 6px;
    }
    .badge-urgent { background: var(--urgent-bg); color: #fca5a5; }
    .badge-warn { background: var(--warn-bg); color: #fcd34d; }
    .badge-ok { background: var(--ok-bg); color: #6ee7b7; }
    .badge-neutral { background: var(--neutral-bg); color: #cbd5e1; }
    .assign-id {
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
      font-size: 11px;
      color: var(--muted);
      background: rgba(255,255,255,0.05);
      padding: 2px 6px;
      border-radius: 5px;
    }
    .course-tag {
      font-size: 11px;
      color: var(--accent);
      font-weight: 600;
      margin-bottom: 3px;
    }
    .task-title {
      font-size: 15px;
      font-weight: 700;
      color: var(--text);
      margin-bottom: 8px;
    }
    .meta-row {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 10px;
    }
    .actions-row {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 8px;
    }
    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      padding: 8px 12px;
      border-radius: 9px;
      font-size: 12px;
      font-weight: 600;
      text-decoration: none;
      border: 1px solid var(--border);
      background: var(--card-hover);
      color: var(--text);
      cursor: pointer;
    }
    .btn-primary {
      background: rgba(56, 189, 248, 0.16);
      border-color: rgba(56, 189, 248, 0.35);
      color: var(--accent);
    }
    .intro-box {
      display: none;
      margin-top: 10px;
      padding: 10px 12px;
      border-radius: 9px;
      background: rgba(0, 0, 0, 0.25);
      border: 1px solid var(--border);
      font-size: 12.5px;
      color: #cbd5e1;
      white-space: pre-line;
      max-height: 260px;
      overflow-y: auto;
      cursor: default;
    }
    .intro-box.open { display: block; }
    .grade-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12.5px;
      margin-top: 8px;
    }
    .grade-table th, .grade-table td {
      padding: 8px 6px;
      border-bottom: 1px solid var(--border);
      text-align: left;
    }
    .grade-table th { color: var(--muted); font-weight: 600; font-size: 11px; text-transform: uppercase; }
    .grade-val { font-weight: 700; color: var(--accent); text-align: center; }
    .bottom-nav {
      position: fixed;
      bottom: 0;
      left: 0;
      right: 0;
      z-index: 30;
      background: rgba(15, 23, 42, 0.96);
      backdrop-filter: blur(12px);
      border-top: 1px solid var(--border);
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      max-width: 760px;
      margin: 0 auto;
    }
    .nav-btn {
      background: none;
      border: none;
      color: var(--muted);
      padding: 10px 4px;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 3px;
      font-size: 11px;
      font-weight: 600;
      cursor: pointer;
    }
    .nav-btn.active { color: var(--accent); }
    .nav-icon { font-size: 17px; }
    .tab-panel { display: none; }
    .tab-panel.active { display: block; }
    .empty-state {
      text-align: center;
      padding: 32px 16px;
      color: var(--muted);
      font-size: 14px;
    }
  </style>
</head>
<body>
  <header>
    <div class="header-top">
      <div class="brand">
        <div class="brand-icon">🧠</div>
        <div>
          <h1>Jarvis do Moodle</h1>
          <p>Arthur Gomes Almeida • IFSP SPO</p>
        </div>
      </div>
      <div class="sync-pill" id="sync-time">Sincronizado</div>
    </div>
  </header>

  <main class="container">
    <!-- TAB 1: RADAR -->
    <section id="tab-radar" class="tab-panel active">
      <div class="kpi-grid">
        <div class="kpi active" id="kpi-card-urgent" data-kpi="urgentes_7d" onclick="setFilter('urgentes_7d')">
          <div class="kpi-num" id="kpi-urgent" style="color:#f87171">0</div>
          <div class="kpi-label">7 Dias</div>
        </div>
        <div class="kpi active" id="kpi-card-ontime" data-kpi="no_prazo" onclick="setFilter('no_prazo')">
          <div class="kpi-num" id="kpi-ontime" style="color:#34d399">0</div>
          <div class="kpi-label">No Prazo</div>
        </div>
        <div class="kpi" id="kpi-card-openlate" data-kpi="atrasadas_abertas" onclick="setFilter('atrasadas_abertas')">
          <div class="kpi-num" id="kpi-openlate" style="color:#fbbf24">0</div>
          <div class="kpi-label">Atraso Abertas</div>
        </div>
        <div class="kpi" id="kpi-card-done" data-kpi="entregues" onclick="switchTab('entregues')">
          <div class="kpi-num" id="kpi-done" style="color:#38bdf8">0</div>
          <div class="kpi-label">Entregues</div>
        </div>
      </div>

      <div class="search-wrap">
        <input type="search" id="search-input" class="search-bar" placeholder="🔍 Filtrar por matéria, atividade ou ID..." oninput="renderTasks()" />
        <button type="button" id="clear-search-btn" class="clear-search" onclick="clearSearch()" title="Limpar busca">✕</button>
      </div>

      <div class="filters">
        <button class="chip active" data-filter="foco" onclick="setFilter('foco')">🎯 Urgentes & No Prazo</button>
        <button class="chip" data-filter="urgentes_7d" onclick="setFilter('urgentes_7d')">🔥 7 Dias</button>
        <button class="chip" data-filter="no_prazo" onclick="setFilter('no_prazo')">🟢 No Prazo</button>
        <button class="chip" data-filter="atrasadas_abertas" onclick="setFilter('atrasadas_abertas')">🔓 Atrasadas (Aceitam Envio)</button>
        <button class="chip" data-filter="sem_prazo" onclick="setFilter('sem_prazo')">⚪ Sem Prazo</button>
        <button class="chip" data-filter="todas_pendentes" onclick="setFilter('todas_pendentes')">📋 Todas Pendentes</button>
      </div>

      <div class="results-bar">
        <span id="results-count">Carregando...</span>
      </div>

      <div id="radar-list"></div>
    </section>

    <!-- TAB 2: DISCIPLINAS -->
    <section id="tab-cursos" class="tab-panel">
      <div id="cursos-list"></div>
    </section>

    <!-- TAB 3: NOTAS -->
    <section id="tab-notas" class="tab-panel">
      <div id="notas-list"></div>
    </section>

    <!-- TAB 4: ENTREGUES -->
    <section id="tab-entregues" class="tab-panel">
      <div id="entregues-list"></div>
    </section>
  </main>

  <nav class="bottom-nav">
    <button class="nav-btn active" data-tab="radar" onclick="switchTab('radar')">
      <span class="nav-icon">🚨</span><span>Radar</span>
    </button>
    <button class="nav-btn" data-tab="cursos" onclick="switchTab('cursos')">
      <span class="nav-icon">📚</span><span>Matérias</span>
    </button>
    <button class="nav-btn" data-tab="notas" onclick="switchTab('notas')">
      <span class="nav-icon">📊</span><span>Notas</span>
    </button>
    <button class="nav-btn" data-tab="entregues" onclick="switchTab('entregues')">
      <span class="nav-icon">✅</span><span>Entregues</span>
    </button>
  </nav>

  <script>
    const DATA = __MOODLE_DATA_JSON__;
    let currentFilter = 'foco';

    function calcLiveStatus(t) {
      const nowSec = Math.floor(Date.now() / 1000);
      const due = t.duedate || 0;
      const canEdit = t.canedit !== undefined ? t.canedit : true;
      if (t.entregue) {
        return { category: 'entregue', label: '✅ Entregue', cls: 'ok', diffH: 999999 };
      }
      if (!due || due <= 0) {
        return { category: 'sem_prazo', label: '⚪ Sem prazo definido', cls: 'neutral', diffH: 99999 };
      }
      const diffSec = due - nowSec;
      const diffH = diffSec / 3600;
      if (diffSec < 0) {
        const d = Math.floor(Math.abs(diffH) / 24);
        const h = Math.floor(Math.abs(diffH) % 24);
        const tempo = d > 0 ? `${d}d atrás` : `${h}h atrás`;
        if (canEdit) {
          return { category: 'atrasada_aberta', label: `🔓 Atrasada (${tempo}) • Aceita Envio`, cls: 'warn', diffH };
        }
        return { category: 'encerrada', label: `🔒 Encerrada (${tempo})`, cls: 'neutral', diffH };
      } else if (diffH <= 48) {
        const h = Math.floor(diffH);
        const m = Math.floor((diffSec % 3600) / 60);
        return { category: 'urgente', label: `🔴 URGENTE (Faltam ${h}h ${m}m)`, cls: 'urgent', diffH };
      } else if (diffH <= 168) {
        const d = Math.floor(diffH / 24);
        const h = Math.floor(diffH % 24);
        return { category: 'semana', label: `🟡 Esta Semana (Faltam ${d}d ${h}h)`, cls: 'warn', diffH };
      } else {
        const d = Math.floor(diffH / 24);
        return { category: 'no_prazo', label: `🟢 No Prazo (${d} dias restantes)`, cls: 'ok', diffH };
      }
    }

    function escapeHtml(s) {
      if (!s) return '';
      return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }

    function updateKPIs() {
      document.getElementById('sync-time').textContent = 'Sync: ' + (DATA.ultima_sincronizacao || '');
      let urgent7d = 0, onTime = 0, openLate = 0, done = 0;
      for (const t of DATA.todas_tarefas) {
        const st = calcLiveStatus(t);
        if (st.category === 'entregue') done++;
        else if (st.category === 'urgente' || st.category === 'semana') urgent7d++;
        else if (st.category === 'no_prazo') onTime++;
        else if (st.category === 'atrasada_aberta') openLate++;
      }
      document.getElementById('kpi-urgent').textContent = urgent7d;
      document.getElementById('kpi-ontime').textContent = onTime;
      document.getElementById('kpi-openlate').textContent = openLate;
      document.getElementById('kpi-done').textContent = done;
    }

    function syncActiveStates() {
      document.querySelectorAll('.chip').forEach(c => {
        c.classList.toggle('active', c.dataset.filter === currentFilter);
      });
      const kpiUrgent = document.getElementById('kpi-card-urgent');
      const kpiOntime = document.getElementById('kpi-card-ontime');
      const kpiOpenlate = document.getElementById('kpi-card-openlate');
      if (kpiUrgent) kpiUrgent.classList.toggle('active', currentFilter === 'urgentes_7d' || currentFilter === 'foco');
      if (kpiOntime) kpiOntime.classList.toggle('active', currentFilter === 'no_prazo' || currentFilter === 'foco');
      if (kpiOpenlate) kpiOpenlate.classList.toggle('active', currentFilter === 'atrasadas_abertas');
    }

    function setFilter(f) {
      currentFilter = f;
      syncActiveStates();
      switchTab('radar');
      renderTasks();
    }

    function clearSearch() {
      const inp = document.getElementById('search-input');
      if (inp) inp.value = '';
      renderTasks();
    }

    function filterByCourse(courseId) {
      const course = DATA.cursos.find(c => c.id === courseId);
      if (!course) return;
      const inp = document.getElementById('search-input');
      if (inp) inp.value = course.fullname;
      currentFilter = 'todas_pendentes';
      syncActiveStates();
      switchTab('radar');
      renderTasks();
    }

    function toggleIntro(id) {
      const el = document.getElementById('intro-' + id);
      if (!el) return;
      const isOpen = el.classList.toggle('open');
      const btn = document.getElementById('btn-intro-' + id);
      if (btn && btn.dataset.labelOpen && btn.dataset.labelClosed) {
        btn.textContent = isOpen ? btn.dataset.labelOpen : btn.dataset.labelClosed;
      }
    }

    function handleCardClick(e, id) {
      if (e.target.closest('a, button, .intro-box')) return;
      toggleIntro(id);
    }

    function renderTasks() {
      const inp = document.getElementById('search-input');
      const rawQ = inp ? inp.value : '';
      const q = (rawQ || '').toLowerCase().trim();
      const clearBtn = document.getElementById('clear-search-btn');
      if (clearBtn) clearBtn.classList.toggle('visible', q.length > 0);

      const list = document.getElementById('radar-list');
      const enriched = DATA.todas_tarefas
        .filter(t => !t.entregue)
        .map(t => ({ ...t, _live: calcLiveStatus(t) }));

      const filtered = enriched.filter(t => {
        if (q) {
          const hay = `${t.nome} ${t.curso_nome} ${t.assign_id}`.toLowerCase();
          if (!hay.includes(q)) return false;
        }
        const cat = t._live.category;
        if (currentFilter === 'foco') {
          return cat === 'urgente' || cat === 'semana' || cat === 'no_prazo';
        } else if (currentFilter === 'urgentes_7d') {
          return cat === 'urgente' || cat === 'semana';
        } else if (currentFilter === 'no_prazo') {
          return cat === 'no_prazo';
        } else if (currentFilter === 'atrasadas_abertas') {
          return cat === 'atrasada_aberta';
        } else if (currentFilter === 'sem_prazo') {
          return cat === 'sem_prazo';
        }
        return true;
      });

      filtered.sort((a, b) => {
        const da = a.duedate > 0 ? a.duedate : 9999999999;
        const db = b.duedate > 0 ? b.duedate : 9999999999;
        return da - db;
      });

      const countEl = document.getElementById('results-count');
      if (countEl) {
        countEl.textContent = `Exibindo ${filtered.length} atividade(s)` + (q ? ` para "${rawQ.trim()}"` : '');
      }

      if (!filtered.length) {
        list.innerHTML = `<div class="empty-state">🎉 Nenhuma atividade encontrada neste filtro!</div>`;
        return;
      }

      list.innerHTML = filtered.map(t => {
        const st = t._live;
        const anexos = (t.anexos || []).map(a => a.filename).join(', ');
        const introText = t.intro || 'Sem texto adicional na descrição da atividade no Moodle.';
        return `
          <div class="card card-${st.cls} clickable-card" data-assign-id="${t.assign_id}" onclick="handleCardClick(event, '${t.assign_id}')">
            <div class="card-top">
              <span class="badge badge-${st.cls}">${escapeHtml(st.label)}</span>
              <span class="assign-id">ID: ${t.assign_id}</span>
            </div>
            <div class="course-tag">${escapeHtml(t.curso_nome)}</div>
            <div class="task-title">${escapeHtml(t.nome)}</div>
            <div class="meta-row">
              <span>📅 Prazo: <strong>${escapeHtml(t.duedate_fmt)}</strong></span>
              <span>${t.canedit !== false ? '🔓 Submissão aberta' : '🔒 Submissão bloqueada'}</span>
            </div>
            <div class="actions-row">
              <a class="btn btn-primary" href="${escapeHtml(t.url)}" target="_blank" rel="noopener">Abrir no Moodle ↗</a>
              <button type="button" class="btn btn-intro-toggle" id="btn-intro-${t.assign_id}" data-label-closed="📄 Ver Enunciado" data-label-open="🔼 Ocultar Enunciado" onclick="toggleIntro('${t.assign_id}')">📄 Ver Enunciado</button>
            </div>
            <div class="intro-box" id="intro-${t.assign_id}">
              ${anexos ? `<strong>📎 Anexos:</strong> ${escapeHtml(anexos)}\n\n` : ''}${escapeHtml(introText)}
            </div>
          </div>
        `;
      }).join('');
    }

    function renderCursos() {
      const container = document.getElementById('cursos-list');
      container.innerHTML = DATA.cursos.map(c => {
        const pdfList = (c.pdfs || []).map(p => `• ${escapeHtml(p.filename)} <span style="color:var(--muted)">(${escapeHtml(p.secao)})</span>`).join('\n');
        const courseTasks = DATA.todas_tarefas.filter(t => t.curso_id === c.id);
        const taskListHtml = courseTasks.map(t => {
          const st = calcLiveStatus(t);
          return `• <strong>${escapeHtml(t.nome)}</strong> — <span style="color:var(--accent)">${escapeHtml(st.label)}</span> (${escapeHtml(t.duedate_fmt)})`;
        }).join('\n');
        return `
          <div class="card" data-course-id="${c.id}">
            <div class="card-top">
              <span class="course-tag">Curso ID: ${c.id}</span>
              <span class="assign-id">${escapeHtml(c.shortname)}</span>
            </div>
            <div class="task-title">${escapeHtml(c.fullname)}</div>
            <div class="meta-row">
              <span>📝 ${c.tarefas_count} tarefas</span>
              <span>📄 ${c.pdfs_count} arquivos/PDFs</span>
            </div>
            <div class="actions-row">
              ${c.pdfs_count > 0 ? `<button type="button" class="btn btn-pdfs-toggle" id="btn-intro-curso-${c.id}" data-label-closed="📂 Ver ${c.pdfs_count} PDFs baixados" data-label-open="🔼 Ocultar PDFs" onclick="toggleIntro('curso-${c.id}')">📂 Ver ${c.pdfs_count} PDFs baixados</button>` : ''}
              ${c.tarefas_count > 0 ? `<button type="button" class="btn btn-tasks-toggle" id="btn-intro-curso-tasks-${c.id}" data-label-closed="📝 Ver ${c.tarefas_count} Tarefas" data-label-open="🔼 Ocultar Tarefas" onclick="toggleIntro('curso-tasks-${c.id}')">📝 Ver ${c.tarefas_count} Tarefas</button>` : ''}
              ${c.tarefas_count > 0 ? `<button type="button" class="btn btn-primary btn-filter-course" onclick="filterByCourse(${c.id})">🚨 Filtrar no Radar</button>` : ''}
            </div>
            ${c.pdfs_count > 0 ? `<div class="intro-box" id="intro-curso-${c.id}">${pdfList}</div>` : ''}
            ${c.tarefas_count > 0 ? `<div class="intro-box" id="intro-curso-tasks-${c.id}">${taskListHtml}</div>` : ''}
          </div>
        `;
      }).join('');
    }

    function renderNotas() {
      const container = document.getElementById('notas-list');
      const comNotas = DATA.cursos.filter(c => (c.notas || []).length > 0);
      container.innerHTML = comNotas.map(c => `
        <div class="card">
          <div class="task-title">${escapeHtml(c.fullname)}</div>
          <table class="grade-table">
            <thead>
              <tr><th>Avaliação</th><th style="text-align:center">Nota</th><th>Feedback</th></tr>
            </thead>
            <tbody>
              ${c.notas.map(n => `
                <tr>
                  <td>${escapeHtml(n.itemname)}</td>
                  <td class="grade-val">${escapeHtml(n.gradeformatted)} <span style="color:var(--muted);font-weight:400">/ ${n.grademax}</span></td>
                  <td style="color:var(--muted)">${escapeHtml(n.feedback || '-')}</td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        </div>
      `).join('');
    }

    function renderEntregues() {
      const container = document.getElementById('entregues-list');
      const entregues = DATA.todas_tarefas
        .filter(t => t.entregue)
        .sort((a, b) => (b.duedate || 0) - (a.duedate || 0));
      container.innerHTML = entregues.map(t => {
        const anexos = (t.anexos || []).map(a => a.filename).join(', ');
        const introText = t.intro || 'Sem texto adicional na descrição da atividade.';
        return `
        <div class="card card-ok clickable-card" onclick="handleCardClick(event, 'done-${t.assign_id}')">
          <div class="card-top">
            <span class="badge badge-ok">✅ Entregue • ${escapeHtml(t.status_avaliacao)}</span>
            <span class="assign-id">ID: ${t.assign_id}</span>
          </div>
          <div class="course-tag">${escapeHtml(t.curso_nome)}</div>
          <div class="task-title">${escapeHtml(t.nome)}</div>
          <div class="meta-row">
            <span>📅 Prazo: ${escapeHtml(t.duedate_fmt)}</span>
            <span>📎 ${escapeHtml((t.arquivos_enviados || []).join(', ') || 'Texto online')}</span>
          </div>
          <div class="actions-row">
            <a class="btn btn-primary" href="${escapeHtml(t.url)}" target="_blank" rel="noopener">Ver no Moodle ↗</a>
            <button type="button" class="btn btn-intro-toggle" id="btn-intro-done-${t.assign_id}" data-label-closed="📄 Ver Enunciado" data-label-open="🔼 Ocultar Enunciado" onclick="toggleIntro('done-${t.assign_id}')">📄 Ver Enunciado</button>
          </div>
          <div class="intro-box" id="intro-done-${t.assign_id}">
            ${anexos ? `<strong>📎 Anexos:</strong> ${escapeHtml(anexos)}\n\n` : ''}${escapeHtml(introText)}
          </div>
        </div>
      `}).join('');
    }

    function switchTab(tab) {
      document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
      document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
      const panel = document.getElementById('tab-' + tab);
      if (panel) panel.classList.add('active');
      const btn = document.querySelector(`.nav-btn[data-tab="${tab}"]`);
      if (btn) btn.classList.add('active');
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }

    updateKPIs();
    syncActiveStates();
    renderTasks();
    renderCursos();
    renderNotas();
    renderEntregues();
    setInterval(() => { updateKPIs(); renderTasks(); }, 60000);
  </script>
</body>
</html>
"""
    html_final = html_template.replace("__MOODLE_DATA_JSON__", json_inline)
    caminho_raiz = BASE_DIR / "Painel_Jarvis_Mobile.html"
    caminho_vault = VAULT_DIR / "Painel_Jarvis_Mobile.html"
    caminho_raiz.write_text(html_final, encoding="utf-8")
    caminho_vault.write_text(html_final, encoding="utf-8")
    print(f"📱 Painel Mobile HTML atualizado em: {caminho_raiz}")
    publicar_painel_github_pages(html_final)


def _obter_github_token() -> Optional[str]:
    env = carregar_env()
    if env.get("GITHUB_TOKEN"):
        return env["GITHUB_TOKEN"]
    try:
        import subprocess
        proc = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            text=True,
            capture_output=True,
            timeout=10,
        )
        creds = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
        return creds.get("password")
    except Exception:
        return None


def _github_put_file(
    session: requests.Session,
    owner: str,
    repo: str,
    path: str,
    content_str: str,
    message: str,
) -> None:
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    r_get = session.get(url, timeout=20)
    sha = r_get.json().get("sha") if r_get.status_code == 200 else None
    payload: Dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(content_str.encode("utf-8")).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha
    r_put = session.put(url, json=payload, timeout=30)
    r_put.raise_for_status()


def publicar_painel_github_pages(html_content: str) -> None:
    """Publica automaticamente o Painel Mobile HTML no GitHub Pages (PWA para Android)."""
    token = _obter_github_token()
    if not token:
        print("⚠️ Token do GitHub não encontrado; pulando publicação no GitHub Pages.")
        return

    owner = "Arthuro08"
    repo = "jarvis-moodle-painel"
    s = requests.Session()
    s.headers.update({
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    })

    try:
        r_repo = s.get(f"https://api.github.com/repos/{owner}/{repo}", timeout=20)
        if r_repo.status_code == 404:
            print(f"🚀 Criando repositório '{owner}/{repo}' no GitHub para o Painel Mobile...")
            r_create = s.post(
                "https://api.github.com/user/repos",
                json={
                    "name": repo,
                    "description": "Painel Mobile PWA — Jarvis do Moodle (IFSP SPO)",
                    "private": False,
                    "auto_init": True,
                },
                timeout=30,
            )
            r_create.raise_for_status()
            default_branch = r_create.json().get("default_branch", "main")
        else:
            r_repo.raise_for_status()
            default_branch = r_repo.json().get("default_branch", "main")

        manifest_json = json.dumps(
            {
                "name": "Jarvis do Moodle — IFSP SPO",
                "short_name": "Jarvis Moodle",
                "start_url": "./index.html",
                "display": "standalone",
                "background_color": "#0b1120",
                "theme_color": "#0b1120",
                "icons": [
                    {
                        "src": "icon.svg",
                        "sizes": "any",
                        "type": "image/svg+xml",
                        "purpose": "any maskable",
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        icon_svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">'
            '<rect width="128" height="128" rx="28" fill="#0b1120"/>'
            '<rect x="8" y="8" width="112" height="112" rx="22" fill="#0284c7" fill-opacity="0.22" stroke="#38bdf8" stroke-width="4"/>'
            '<text x="64" y="84" font-size="64" text-anchor="middle">🧠</text>'
            "</svg>"
        )

        agora_str = datetime.datetime.now().strftime("%d/%m/%Y %H:%M")
        _github_put_file(s, owner, repo, "index.html", html_content, f"Sync Jarvis Moodle ({agora_str})")
        _github_put_file(s, owner, repo, "manifest.json", manifest_json, "Configure PWA manifest")
        _github_put_file(s, owner, repo, "icon.svg", icon_svg, "Configure PWA icon")

        # Garante que o GitHub Pages está habilitado
        r_pages = s.get(f"https://api.github.com/repos/{owner}/{repo}/pages", timeout=20)
        if r_pages.status_code == 404:
            s.post(
                f"https://api.github.com/repos/{owner}/{repo}/pages",
                json={"source": {"branch": default_branch, "path": "/"}},
                timeout=20,
            )

        print(f"🌐 App Mobile publicado no GitHub Pages: https://arthuro08.github.io/{repo}/")
    except Exception as e:
        print(f"⚠️ Aviso ao publicar no GitHub Pages: {e}")


# ==============================================================================
# LEITURA DE PDF E ENTREGA DE TAREFAS (SUBMISSION ENGINE)
# ==============================================================================

def ler_pdf(caminho: str) -> str:
    path = Path(caminho)
    if not path.is_absolute():
        # Tenta localizar dentro de VAULT_DIR ou MATERIAIS_DIR se relativo
        candidato1 = BASE_DIR / caminho
        candidato2 = VAULT_DIR / caminho
        if candidato1.exists():
            path = candidato1
        elif candidato2.exists():
            path = candidato2
        else:
            # Busca pelo nome do arquivo em MATERIAIS_DIR
            matches = list(MATERIAIS_DIR.rglob(f"*{Path(caminho).name}*"))
            if matches:
                path = matches[0]

    if not path.exists():
        raise FileNotFoundError(f"PDF não encontrado: {caminho}")

    reader = PdfReader(str(path))
    paginas_texto = []
    for idx, page in enumerate(reader.pages, start=1):
        txt = page.extract_text() or ""
        paginas_texto.append(f"--- [Página {idx}/{len(reader.pages)}] ---\n{txt.strip()}")

    resultado = f"📄 ARQUIVO: {path.name}\n📍 CAMINHO: {path}\n\n" + "\n\n".join(paginas_texto)
    print(resultado)
    return resultado


def entregar_tarefa(
    assign_id: int,
    arquivos: Optional[List[str]] = None,
    texto_online: Optional[str] = None,
    finalizar: bool = False,
) -> None:
    """
    Realiza o envio de uma tarefa (Assignment) no Moodle via REST API:
      1. Faz upload do(s) arquivo(s) para a Draft Area do usuário (webservice/upload.php)
      2. Chama mod_assign_save_submission vinculando o itemid dos arquivos e/ou texto online
      3. Se finalizar=True (e a tarefa exigir confirmação de envio), chama mod_assign_submit_for_grading
    """
    client = MoodleClient()
    itemid = 0

    if arquivos:
        for arq_str in arquivos:
            caminho_arq = Path(arq_str).resolve()
            print(f"📤 Fazendo upload de '{caminho_arq.name}' para a área de rascunho do Moodle...")
            up_res = client.upload_draft_file(caminho_arq, itemid=itemid)
            itemid = int(up_res["itemid"])
            print(f"   ✔ Upload concluído! Draft ItemID={itemid}")

    params: Dict[str, Any] = {"assignmentid": assign_id}
    if itemid > 0:
        params["plugindata[files_filemanager]"] = itemid
    if texto_online is not None:
        params["plugindata[onlinetext_editor][text]"] = texto_online
        params["plugindata[onlinetext_editor][format]"] = 1
        params["plugindata[onlinetext_editor][itemid]"] = 0

    print(f"📝 Salvando submissão na tarefa AssignID={assign_id}...")
    res_save = client.call("mod_assign_save_submission", **params)
    if isinstance(res_save, dict) and "exception" in res_save:
        raise RuntimeError(f"Erro ao salvar submissão no Moodle: {res_save}")
    print(f"   ✔ Submissão salva com sucesso! Resposta API: {res_save}")

    # Verifica status atualizado no Moodle após salvar
    st = client.call("mod_assign_get_submission_status", assignid=assign_id, userid=client.userid)
    cansubmit = False
    status_atual = "unknown"
    if isinstance(st, dict) and "lastattempt" in st:
        la = st["lastattempt"]
        cansubmit = bool(la.get("cansubmit", False))
        sub = la.get("submission", {})
        status_atual = sub.get("status", "unknown")

    if finalizar:
        if cansubmit:
            print(f"🚀 Confirmando entrega final para avaliação (mod_assign_submit_for_grading)...")
            res_sub = client.call(
                "mod_assign_submit_for_grading",
                assignmentid=assign_id,
                acceptsubmissionstatement=1,
            )
            print(f"   ✔ Entrega finalizada! Resposta API: {res_sub}")
            st = client.call("mod_assign_get_submission_status", assignid=assign_id, userid=client.userid)
            if isinstance(st, dict) and "lastattempt" in st:
                status_atual = st["lastattempt"].get("submission", {}).get("status", status_atual)
        elif status_atual == "submitted":
            print("ℹ️ A tarefa possui envio direto (submissiondrafts=0): já consta como 'submitted' sem etapa extra.")

    print(f"✅ Status atual no servidor do Moodle: status='{status_atual}'")


def exibir_radar_prazos() -> None:
    if not CACHE_PATH.exists():
        dados = sincronizar_moodle(baixar_pdfs=False)
    else:
        dados = json.loads(CACHE_PATH.read_text(encoding="utf-8"))

    agora = datetime.datetime.now().timestamp()
    tarefas = sorted(
        dados["todas_tarefas"],
        key=lambda x: (x["duedate"] if x["duedate"] > 0 else 9999999999),
    )

    print("\n" + "=" * 85)
    print(f"🦾 JARVIS DO MOODLE — RADAR DE PRAZOS E ENTREGAS ({dados['ultima_sincronizacao']})")
    print("=" * 85)

    pendentes = [t for t in tarefas if not t["entregue"] and (t["duedate"] == 0 or t["duedate"] >= agora - 14 * 86400)]
    if not pendentes:
        print("🎉 Nenhuma tarefa pendente com prazo próximo!")
    else:
        print("\n📌 TAREFAS PENDENTES / ATIVAS:")
        for t in pendentes:
            print(f"  • {t['status_prazo']} | Prazo: {t['duedate_fmt']}")
            print(f"    Disciplina: {t['curso_nome']}")
            print(f"    Atividade : [AssignID: {t['assign_id']}] {t['nome']}")
            print(f"    Link      : {t['url']}\n")

    entregues = [t for t in tarefas if t["entregue"]]
    print(f"✅ Total de tarefas já entregues no histórico: {len(entregues)}")
    print("=" * 85 + "\n")


# ==============================================================================
# CLI ENTRYPOINT
# ==============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Jarvis do Moodle — IFSP SPO")
    sub = parser.add_subparsers(dest="comando")

    p_sync = sub.add_parser("sync", help="Sincroniza cursos, prazos, notas, PDFs e atualiza o Obsidian")
    p_sync.add_argument("--sem-pdfs", action="store_true", help="Não faz download de novos PDFs")

    sub.add_parser("prazos", help="Exibe o radar de prazos e entregas no terminal")
    sub.add_parser("auth", help="Força renovação do token via SUAP OAuth2")

    p_pdf = sub.add_parser("ler-pdf", help="Extrai e imprime o texto de um PDF")
    p_pdf.add_argument("caminho", help="Caminho ou nome do arquivo PDF")

    p_ent = sub.add_parser("entregar", help="Envia arquivo e/ou texto para uma tarefa no Moodle")
    p_ent.add_argument("--assign-id", type=int, required=True, help="Assign ID da tarefa no Moodle")
    p_ent.add_argument("--arquivo", action="append", help="Caminho de arquivo para enviar (pode repetir)")
    p_ent.add_argument("--texto", type=str, default=None, help="Texto online (HTML/texto) da entrega")
    p_ent.add_argument("--finalizar", action="store_true", help="Confirma envio definitivo para avaliação")

    args = parser.parse_args()

    if args.comando == "auth":
        renovar_autenticacao_suap()
    elif args.comando == "prazos":
        exibir_radar_prazos()
    elif args.comando == "ler-pdf":
        ler_pdf(args.caminho)
    elif args.comando == "entregar":
        entregar_tarefa(
            assign_id=args.assign_id,
            arquivos=args.arquivo,
            texto_online=args.texto,
            finalizar=args.finalizar,
        )
    else:
        # Padrão: sync completo
        baixar = not getattr(args, "sem_pdfs", False)
        sincronizar_moodle(baixar_pdfs=baixar)
        exibir_radar_prazos()


if __name__ == "__main__":
    main()
