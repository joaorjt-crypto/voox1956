"""
Robô Voox 1956 — sincroniza o Zelo com o Firebase (projeto voox-1956).

Roda no GitHub Actions (.github/workflows/zelo-sync.yml). Faz login no Zelo com um
usuário de leitura, puxa vendas e agendamentos, agrega e grava no Firestore:

  voox_painel/resumo        -> indicadores do painel (faturamento por mês, serviços, etc.)
  voox_reativacao/{cliente} -> lista de clientes sumidas (status de envio é do app)
  voox_dias/{AAAA-MM}       -> movimento dia a dia (filtro de datas do painel)
  voox_confirmacoes/{...}   -> agenda dos próximos dias abertos, para confirmar por WhatsApp
  voox_posatend/{...}       -> quem foi atendida ontem/hoje, para agradecer e pedir avaliação
  voox_oportunidades/{cli}  -> clientes frequentes que nunca fizeram um tipo de serviço
  voox_clientes/{parte}     -> resumo de 12 meses por cliente (aba Clientes), uma vez por dia

Secrets necessários no repositório:
  ZELO_USER, ZELO_PASS   -> login de um usuário do Zelo (de preferência só leitura)
  FIREBASE_SA_VOOX       -> JSON da conta de serviço do projeto voox-1956

Rodar local para teste (sem gravar no Firebase):
  ZELO_USER=... ZELO_PASS=... python robo/zelo_sync.py --dry-run
"""
import os
import re
import sys
import hashlib
import json
import time
import unicodedata
import datetime as dt
from collections import Counter, defaultdict
from zoneinfo import ZoneInfo

import requests

BASE = "https://app.zelo.tec.br"
TZ = ZoneInfo("America/Sao_Paulo")
DRY = "--dry-run" in sys.argv

# Regras da reativação (as mesmas da planilha de novembro/2026)
JANELA_DIAS = 120          # histórico considerado
MIN_DIAS_SUMIDA = 30       # nunca antes de 30 dias sem vir
FATOR_INTERVALO = 1.5      # ...e mais que 1,5x o intervalo habitual da cliente
SERV_EVENTO = re.compile(r"MAQUIAGEM|PENTEADO", re.I)   # quem só vem para evento não entra
EXCLUIR_NOMES = re.compile(os.environ.get("EXCLUIR_NOMES", r"CASA PAGA"), re.I)

STATUS_PAGO = "PAGO"
STATUS_FUTURO = "AGENDADO,AGUARDANDO,EM_ATENDIMENTO"
STATUS_PERDA = "CANCELADO,FALTOU"


def log(*a):
    print(dt.datetime.now(TZ).strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- Zelo

class Zelo:
    def __init__(self, user, pwd):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": "Mozilla/5.0 (VooxRobo; +https://voox1956.com.br)",
            "Accept": "application/json, text/plain, */*",
        })
        self.user, self.pwd = user, pwd

    def login(self):
        self.s.get(BASE + "/login", timeout=30)
        r = self.s.post(BASE + "/api/login", json={"username": self.user, "password": self.pwd}, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"Login no Zelo falhou: HTTP {r.status_code} {r.text[:200]}")
        body = r.json() if r.text else {}
        err = (body.get("content") or {}).get("error")
        if err:
            raise RuntimeError(f"Login no Zelo recusado: {err}")
        redirect = (body.get("content") or {}).get("redirect")
        if redirect:
            self.s.get(redirect if redirect.startswith("http") else BASE + redirect, timeout=30)
        if not self._sessao_ok():
            # usuários com mais de um módulo precisam escolher o módulo do profissional
            self.s.post(BASE + "/selecionar/selecionarmodulo", json={"modulo": "PROFISSIONAL"}, timeout=30)
            if not self._sessao_ok():
                raise RuntimeError("Logou no Zelo, mas a sessão não tem acesso aos dados (verifique o perfil do usuário).")
        log("Login no Zelo ok")

    def _sessao_ok(self):
        r = self.s.get(BASE + "/api/indicador/resumovendas",
                       params={"dataInicial": "2026-01-01T00:00:00", "dataFinal": "2026-01-01T23:59:59"}, timeout=30)
        return r.status_code == 200 and "content" in r.text

    def get(self, path, params=None, tries=4):
        for i in range(tries):
            try:
                r = self.s.get(BASE + path, params=params, timeout=60)
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 401 and i == 0:
                    self.login()
                    continue
                raise RuntimeError(f"HTTP {r.status_code} em {path}")
            except (requests.RequestException, ValueError, RuntimeError) as e:
                if i == tries - 1:
                    raise
                log(f"  tentativa {i + 1} falhou em {path}: {e}; repetindo")
                time.sleep(3 * (i + 1))

    def resumo_vendas(self, ini, fim):
        c = self.get("/api/indicador/resumovendas", {
            "dataInicial": ini.strftime("%Y-%m-%dT00:00:00"),
            "dataFinal": fim.strftime("%Y-%m-%dT23:59:59"),
        })["content"]
        fat = money(c.get("valorVendaTotalFormatado"))
        ticket = money(c.get("mediaComandaFormatado"))
        return {
            "fat": fat,
            "servicos": money((c.get("servicos") or {}).get("valorVendaFormatado")),
            "servQtd": num((c.get("servicos") or {}).get("quantidadeVendida")),
            "produtos": money((c.get("produtos") or {}).get("valorVendaFormatado")),
            "pacotes": money((c.get("pacotesVendidos") or {}).get("valorVendaFormatado")),
            "comissao": money(c.get("custoComissaTotaloFormatado")),
            "custoProdutos": money(c.get("custoProdutosTotalFormatado")),
            "resultado": float(c.get("resultadoGeral") or 0),
            "ticket": ticket,
            "comandas": round(fat / ticket) if ticket else 0,
        }

    def agendamentos(self, ini, fim, status, fields):
        out, page = [], 1
        while True:
            c = self.get("/api/agendamentos/horarios", {
                "clienteId": 0, "profissionalId": 0, "servicoId": 0,
                "data": ini.strftime("%Y-%m-%dT00:00:00"),
                "dataFinal": fim.strftime("%Y-%m-%dT23:59:59"),
                "fields": fields, "statusList": status,
                "ocultarFilaEspera": "true", "ocultarHorarios": "false",
                "rpp": 250, "page": page,
            })["content"]
            lote = c.get("resultList") or []
            if not lote:
                break
            out.extend(lote)
            page += 1
            if page > 400:
                raise RuntimeError("Paginação dos agendamentos não terminou (limite de segurança)")
        return out

    def cliente(self, cid):
        return (self.get(f"/api/clientes/{cid}") or {}).get("content") or {}

    def catalogo(self):
        """preço de tabela por serviço (inclui os marcados como não utilizados, para o histórico)"""
        precos = {}
        for nao_usado in ("N", "S"):
            page = 1
            while page < 30:
                c = self.get("/api/servicos", {"page": page, "rpp": 100, "q": "", "servicoNaoUtilizado": nao_usado,
                                               "fields": "id,valor"})["content"]
                for x in c.get("resultList") or []:
                    precos[str(x.get("id"))] = float(x.get("valor") or 0)
                if not c.get("more"):
                    break
                page += 1
        return precos

    def horario(self):
        """minutos de funcionamento por dia da semana (segunda=0 ... domingo=6)"""
        c = (self.get("/api/configuracoes/0") or {}).get("content") or {}
        nomes = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
        out = [{"aberto": False, "ini": 0, "fim": 0, "min": 0} for _ in range(7)]
        for h in c.get("horarioFuncionamentos") or []:
            nome = (h.get("diaSemanaStr") or "").strip()
            if nome not in nomes:
                continue
            ini = h.get("abertura") or "T00:00"
            fim = h.get("fechamento") or "T00:00"
            a = int(ini[11:13]) * 60 + int(ini[14:16])
            f = int(fim[11:13]) * 60 + int(fim[14:16])
            out[nomes.index(nome)] = {"aberto": bool(h.get("aberto")), "ini": a, "fim": f,
                                      "min": max(0, f - a) if h.get("aberto") else 0}
        return out

    def aniversariantes(self):
        ids, page = [], 1
        while page < 20:
            c = self.get("/api/clientes/aniversariantessemana", {"page": page, "rpp": 50, "fields": "id"})["content"]
            ids += [str(x.get("id")) for x in c.get("resultList") or [] if x.get("id")]
            if not c.get("more"):
                break
            page += 1
        out = []
        for cid in ids:
            try:
                c = self.cliente(cid)
            except Exception as e:
                log(f"  aniversariante {cid} falhou: {e}")
                continue
            nasc = c.get("dataNascimentoFormatado") or c.get("aniversarioFormatado") or ""
            out.append({"id": cid, "nome": limpa_nome(c.get("nome")), "cel": celular_br(c.get("celular")),
                        "dia": nasc[:5]})
        return out


def money(txt):
    if txt is None:
        return 0.0
    if isinstance(txt, (int, float)):
        return float(txt)
    t = re.sub(r"[^\d.,-]", "", str(txt))
    # o Zelo formata em padrão americano: 250,986.12
    t = t.replace(",", "")
    try:
        return float(t)
    except ValueError:
        return 0.0


def num(txt):
    try:
        return int(re.sub(r"\D", "", str(txt or "0")) or 0)
    except ValueError:
        return 0


def celular_br(raw):
    d = re.sub(r"\D", "", raw or "")
    if d.startswith("55") and len(d) in (12, 13):
        d = d[2:]
    if re.fullmatch(r"[1-9][1-9][6-9]\d{7}", d):     # número antigo sem o 9
        d = d[:2] + "9" + d[2:]
    if not re.fullmatch(r"[1-9][1-9]9\d{8}", d) or re.search(r"(\d)\1{5,}", d):
        return ""
    return d


def limpa_nome(n):
    n = re.sub(r"\*.*$", "", n or "")          # observações da recepção depois de *
    n = re.sub(r"[*,.]+", "", n)
    return re.sub(r"\s+", " ", n).strip()


def limpa_serv(n):
    n = re.sub(r"\*+", "", n or "")
    n = re.sub(r"\s*-\s*$", "", n)
    return re.sub(r"\s+", " ", n).strip()


# ---------------------------------------------------------------- coleta

def coletar(z):
    hoje = dt.datetime.now(TZ).date()
    dados = {"hoje": hoje}

    # Faturamento por mês: 25 meses (12 + mesmo período do ano anterior + mês atual)
    meses = {}
    primeiro = date_add_months(hoje.replace(day=1), -24)
    m = primeiro
    while m <= hoje:
        fim = min(date_add_months(m, 1) - dt.timedelta(days=1), hoje)
        meses[m.strftime("%Y-%m")] = z.resumo_vendas(m, fim)
        m = date_add_months(m, 1)
    dados["meses"] = meses
    # mesmo período do mês atual no ano anterior (para comparar mês em curso)
    ini_ant = date_add_months(hoje.replace(day=1), -12)
    fim_ant = ini_ant.replace(day=min(hoje.day, month_days(ini_ant)))
    dados["mesAtualAnoAnteriorAteHoje"] = z.resumo_vendas(ini_ant, fim_ant)
    log(f"Vendas: {len(meses)} meses")

    ini = hoje - dt.timedelta(days=JANELA_DIAS)
    dados["pagos"] = z.agendamentos(
        ini, hoje, STATUS_PAGO,
        "dataHora,agendamentoMobile,cliente(id,nome),servico(id,nome),profissional(id,nomeAgenda)")
    log(f"Atendimentos pagos ({JANELA_DIAS} dias): {len(dados['pagos'])}")
    dados["precos"] = z.catalogo()
    dados["horario"] = z.horario()
    log(f"Catálogo: {len(dados['precos'])} serviços · horário de funcionamento lido")

    dados["perdas"] = z.agendamentos(hoje - dt.timedelta(days=30), hoje, STATUS_PERDA, "dataHora,status,cliente(id)")
    dados["futuros"] = z.agendamentos(hoje, hoje + dt.timedelta(days=150), STATUS_FUTURO,
                                      "dataHora,status,cliente(id)")
    log(f"Cancelados/faltas 30d: {len(dados['perdas'])} · agendados à frente: {len(dados['futuros'])}")
    return dados


def date_add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    return d.replace(year=d.year + y, month=m + 1, day=1)


def month_days(d):
    return (date_add_months(d, 1) - dt.timedelta(days=1)).day


# ---------------------------------------------------------------- agregação

def montar_resumo(d):
    hoje = d["hoje"]
    pagos = [p for p in d["pagos"] if p.get("dataHora")]
    def dia(p): return dt.date.fromisoformat(p["dataHora"][:10])

    ult30 = [p for p in pagos if (hoje - dia(p)).days < 30]
    ult90 = [p for p in pagos if (hoje - dia(p)).days < 90]

    serv = Counter(limpa_serv((p.get("servico") or {}).get("nome")) for p in ult90 if p.get("servico"))
    prof = Counter(((p.get("profissional") or {}).get("nomeAgenda") or "—").strip() for p in ult30)
    sem = [0] * 7   # segunda=0 ... domingo=6
    for p in ult90:
        sem[dia(p).weekday()] += 1

    clientes30 = {(p.get("cliente") or {}).get("id") for p in ult30 if p.get("cliente")}
    perdas = Counter(p.get("status") for p in d["perdas"])
    futuros7 = [f for f in d["futuros"] if (dt.date.fromisoformat(f["dataHora"][:10]) - hoje).days < 7]

    return {
        "atualizadoEm": dt.datetime.now(TZ).isoformat(timespec="minutes"),
        "hoje": hoje.isoformat(),
        "meses": d["meses"],
        "mesAtualAnoAnteriorAteHoje": d["mesAtualAnoAnteriorAteHoje"],
        "kpis30": {
            "atendimentos": len(ult30),
            "clientes": len(clientes30),
            "online": sum(1 for p in ult30 if p.get("agendamentoMobile")),
            "cancelados": perdas.get("CANCELADO", 0),
            "faltas": perdas.get("FALTOU", 0),
            "agendados7d": len(futuros7),
        },
        "servicos90": [{"nome": k, "qtd": v} for k, v in serv.most_common(15)],
        "profissionais30": [{"nome": k, "qtd": v} for k, v in prof.most_common(20)],
        "diasSemana90": sem,
        "horario": d.get("horario") or [],
    }


def montar_reativacao(d):
    hoje = d["hoje"]
    futuros = {(f.get("cliente") or {}).get("id") for f in d["futuros"] if f.get("cliente")}
    por_cli = defaultdict(lambda: {"datas": [], "serv": Counter(), "nome": ""})
    for p in d["pagos"]:
        c = p.get("cliente")
        if not c or not p.get("dataHora"):
            continue
        x = por_cli[c["id"]]
        x["nome"] = c.get("nome") or x["nome"]
        x["datas"].append(p["dataHora"][:10])
        x["serv"][limpa_serv((p.get("servico") or {}).get("nome"))] += 1

    ultima = {}
    candidatas = {}
    for cid, x in por_cli.items():
        ds = sorted(x["datas"])
        ult = dt.date.fromisoformat(ds[-1])
        ultima[cid] = ds[-1]
        n = len(ds)
        if n < 2 or cid in futuros or EXCLUIR_NOMES.search(x["nome"]):
            continue
        principal = x["serv"].most_common(1)[0][0]
        if SERV_EVENTO.search(principal):
            continue
        span = (ult - dt.date.fromisoformat(ds[0])).days
        intervalo = max(7, round(span / (n - 1)))
        dias = (hoje - ult).days
        if dias < max(MIN_DIAS_SUMIDA, FATOR_INTERVALO * intervalo):
            continue
        nome = limpa_nome(x["nome"])
        candidatas[str(cid)] = {
            "nome": nome,
            "primeiro": nome.split(" ")[0].capitalize() if nome else "",
            "ult": ds[-1],
            "n": n,
            "servico": principal,
            "intervalo": intervalo,
            "prioridade": 1 if n >= 3 else 2,
        }
    return candidatas, ultima, futuros


# ---------------------------------------------------------------- Firestore

def firestore_client():
    import firebase_admin
    from firebase_admin import credentials, firestore
    sa = json.loads(os.environ["FIREBASE_SA_VOOX"])
    firebase_admin.initialize_app(credentials.Certificate(sa))
    return firestore.client()


def retorno_reativacao(pagos, precos, enviado_em):
    """atendimentos e valor (preço de tabela) da cliente a partir do dia da mensagem"""
    ini = (enviado_em or "")[:10]
    n, v = 0, 0.0
    for a in pagos:
        if (a.get("dataHora") or "")[:10] >= ini:
            n += 1
            v += precos.get(str((a.get("servico") or {}).get("id")), 0.0)
    return n, round(v, 2)


def gravar(z, db, resumo, candidatas, ultima, futuros, pagos_por_cli=None, precos=None):
    from firebase_admin import firestore
    agora = dt.datetime.now(TZ).isoformat(timespec="minutes")
    col = db.collection("voox_reativacao")
    existentes = {doc.id: doc.to_dict() for doc in col.stream()}
    log(f"Reativação: {len(candidatas)} candidatas · {len(existentes)} já no Firestore")

    # telefones: só busca no Zelo quem ainda não tem celular salvo
    for cid, c in candidatas.items():
        cel = (existentes.get(cid) or {}).get("cel")
        if not cel:
            try:
                cli = z.cliente(cid)
                cel = celular_br(cli.get("celular"))
            except Exception as e:
                log(f"  telefone da cliente {cid} falhou: {e}")
                cel = ""
        c["cel"] = cel

    batch, ops = db.batch(), 0

    def commit_if_full():
        nonlocal batch, ops
        if ops >= 400:
            batch.commit()
            batch, ops = db.batch(), 0

    for cid, c in candidatas.items():
        if not c["cel"]:
            continue
        ex = existentes.get(cid)
        doc = dict(c, ativo=True, atualizadoEm=agora)
        if ex and ex.get("voltou") and not ex.get("ativo"):
            # voltou antes e sumiu de novo: novo ciclo, guarda o histórico
            doc.update({"ciclos": (ex.get("ciclos") or 0) + 1, "enviado": False, "enviadoEm": None,
                        "voltou": False, "voltouEm": None, "voltouTipo": None})
        elif not ex:
            doc.update({"enviado": False, "voltou": False, "descartado": False, "criadoEm": agora})
        batch.set(col.document(cid), doc, merge=True)
        ops += 1
        commit_if_full()

    # quem estava ativo e saiu da lista: voltou (veio ou agendou) ou deixou de se encaixar
    for cid, ex in existentes.items():
        if cid in candidatas or not ex.get("ativo"):
            continue
        upd = {"ativo": False, "atualizadoEm": agora}
        try:
            cid_int = int(cid)
        except ValueError:
            cid_int = cid
        nova = ultima.get(cid_int) or ultima.get(cid)
        if nova and nova > (ex.get("ult") or ""):
            upd.update({"voltou": True, "voltouEm": nova, "voltouTipo": "veio"})
        elif cid_int in futuros or cid in futuros:
            upd.update({"voltou": True, "voltouEm": agora[:10], "voltouTipo": "agendou"})
        else:
            upd["saiuMotivo"] = "fora do critério"
        batch.set(col.document(cid), upd, merge=True)
        ops += 1
        commit_if_full()

    # quanto voltou em atendimentos/R$ quem recebeu mensagem
    if pagos_por_cli is not None:
        for cid, ex in existentes.items():
            if not ex.get("enviado") or not ex.get("enviadoEm"):
                continue
            n, v = retorno_reativacao(pagos_por_cli.get(cid, []), precos or {}, ex.get("enviadoEm"))
            if n != ex.get("retornoAtend") or v != ex.get("retornoValor"):
                batch.set(col.document(cid), {"retornoAtend": n, "retornoValor": v}, merge=True)
                ops += 1
                commit_if_full()

    batch.set(db.collection("voox_painel").document("resumo"), resumo)
    ops += 1
    batch.commit()
    log("Firestore atualizado")


def gravar_aniversariantes(z, db, hoje):
    col = db.collection("voox_aniversarios")
    lista = z.aniversariantes()
    semana = (hoje - dt.timedelta(days=hoje.weekday())).isoformat()
    agora = dt.datetime.now(TZ).isoformat(timespec="minutes")
    atuais = set()
    batch = db.batch()
    for a in lista:
        if not a["nome"]:
            continue
        atuais.add(a["id"])
        batch.set(col.document(a["id"]), {"nome": a["nome"], "primeiro": a["nome"].split(" ")[0].capitalize(),
                                          "cel": a["cel"], "dia": a["dia"], "semana": semana, "ativo": True,
                                          "atualizadoEm": agora}, merge=True)
    for doc in col.where("ativo", "==", True).stream():
        if doc.id not in atuais:
            batch.set(col.document(doc.id), {"ativo": False, "atualizadoEm": agora}, merge=True)
    batch.commit()
    log(f"Aniversariantes da semana: {len(atuais)}")


# ---------------------------------------------------------------- mensagens do dia

def sem_acento(t):
    return unicodedata.normalize("NFD", str(t or "")).encode("ascii", "ignore").decode().upper()


CAMPANHAS = {
    # chave: (nome, regex do que conta como "já faz", regex de quem é público ou None = todas) — sem acento
    "facial":      ("Estética facial", r"LIMPEZA DE PELE|PEELING|MICROAGULH|DRENAGEM FACIAL|NUTRICAO +FACIAL|HYDRA|RADIOFREQ|MASSAGEM +FACIAL|TRATAMENTO FACIAL|DERMAPLAN|REVITALIZ", None),
    "corporal":    ("Estética corporal", r"DRENAGEM LINF|MASSAGEM|LIPO|CORRENTE RUSSA|REIKI|ESFOLIA|BRONZE|MODELADORA|RELAXANTE|PEDRAS QUENTES", None),
    "podologia":   ("Podologia", r"PODOLOG|PODOPROF|ONICO|UNGUEAL|ESPICULO", r"PEDICURE"),
    "cabelo":      ("Cabelo", r"CORTE|ESCOVA|MECHAS|RETOQUE|TINTURA|COLORA|PROGRESSIVA|BABY ?LISS|LAVADO|TONALIZ|LUZES|BALAYAGE|PENTEADO", None),
    "capilar":     ("Tratamento capilar", r"HIDRATA|NUTRICAO CAPILAR|RECONSTRU|CRONOGRAMA|CAUTERIZ|BOTOX|SELAGEM|ETAPA|ACIDIFIC|AMPOLA|TRATAMENTO CAPILAR",
                    r"ESCOVA|CORTE|MECHAS|COLORA|RETOQUE|TINTURA|LUZES|PROGRESSIVA"),
    "sobrancelha": ("Sobrancelha e cílios", r"SOBRANCEL|DESIGN|HENNA|CILIO|LASH|BROW|LAMINACAO", None),
    "depilacao":   ("Depilação", r"DEPILA|CERA |BUCO|AXILA|VIRILHA", None),
    "maos":        ("Manicure e pedicure", r"MANICURE|PEDICURE|ESMALT|SPA DOS PES", None),
    "gel":         ("Unhas em gel", r"GEL|FIBRA|ALONGAMENTO|BLINDAGEM|POSTICA|ACRIL", r"MANICURE|ESMALT"),
    "maquiagem":   ("Maquiagem", r"MAQUIA|MAKE", r"ESCOVA|PENTEADO|SOBRANCEL|CILIO|MECHAS|COLORA"),
}
# serviços que não fazem sentido sugerir (cortesias, retornos, taxas, infantil, masculino)
NAO_SUGERIR = re.compile(r"RETORNO|AVALIA|TAXA|CORTESIA|BRINDE|PACOTE|CREDITO|MANUTEN|INFANTIL|MASCULIN|KIDS|NOIVA|TESTE|^—$")
TAMANHOS = re.compile(r"\b(CURTA|CURTO|MEDIA|MEDIO|LONGA|LONGO|EXTRA|GRANDE|PEQUEN[AO]|FEMININ[AO]|SIMPLES|COMPLET[AO]|[PMG]|\d+)\b")


def base_servico(n):
    """'ESCOVA MEDIA' e 'ESCOVA LONGA' são a mesma coisa para efeito de sugestão"""
    return re.sub(r"\s+", " ", TAMANHOS.sub(" ", sem_acento(n).replace("/", " "))).strip()


EH_CRIANCA = re.compile(r"FILH[OA]|INFANTIL|SOBRINH|AFILHAD", re.I)


def proximos_dias_abertos(hoje, horario, n=2):
    out, d = [], hoje
    while len(out) < n and (d - hoje).days < 10:
        d += dt.timedelta(days=1)
        h = horario[d.weekday()] if horario and len(horario) == 7 else {"aberto": d.weekday() != 6}
        if h.get("aberto"):
            out.append(d)
    return out


class Fones:
    """celular por cliente, buscando no Zelo só uma vez por execução (com limite de buscas novas por rodada)"""
    LIMITE_NOVOS = 250

    def __init__(self, z, conhecidos=None):
        self.z, self.c, self.novos, self.avisou = z, dict(conhecidos or {}), 0, False

    def __call__(self, cid, essencial=True):
        cid = str(cid)
        if cid not in self.c:
            if not essencial and self.novos >= self.LIMITE_NOVOS:
                if not self.avisou:
                    log(f"  limite de {self.LIMITE_NOVOS} buscas de telefone atingido; o resto fica para a próxima rodada")
                    self.avisou = True
                return ""
            self.novos += 1
            try:
                self.c[cid] = celular_br(self.z.cliente(cid).get("celular"))
            except Exception as e:
                log(f"  telefone da cliente {cid} falhou: {e}")
                self.c[cid] = ""
        return self.c[cid]


def _agrupar_por_cliente(ags, data_iso):
    por = {}
    for a in sorted(ags, key=lambda x: x.get("dataHora") or ""):
        c = a.get("cliente") or {}
        if not c.get("id") or (a.get("dataHora") or "")[:10] != data_iso:
            continue
        x = por.setdefault(str(c["id"]), {"nomeZelo": c.get("nome") or "", "itens": [], "confZelo": False})
        x["itens"].append({"hora": a["dataHora"][11:16], "servico": limpa_serv((a.get("servico") or {}).get("nome")),
                           "prof": ((a.get("profissional") or {}).get("nomeAgenda") or "").strip()})
        if a.get("agendamentoConfirmado"):
            x["confZelo"] = True
    return por


def token_nota(doc_id):
    """link da nota de satisfação: fixo para o atendimento e impossível de adivinhar"""
    try:
        seg = json.loads(os.environ.get("FIREBASE_SA_VOOX") or "{}").get("private_key_id") or ""
    except Exception:
        seg = ""
    seg = seg or os.environ.get("ZELO_PASS", "")
    return hashlib.sha256(f"nota|{seg}|{doc_id}".encode()).hexdigest()[:22]


def gravar_mensagens_do_dia(z, db, d, fones):
    hoje = d["hoje"]
    agora = dt.datetime.now(TZ).isoformat(timespec="minutes")
    limite = (hoje - dt.timedelta(days=7)).isoformat()

    # 1) confirmações: próximos 2 dias abertos
    col = db.collection("voox_confirmacoes")
    dias_conf = proximos_dias_abertos(hoje, d.get("horario"))
    batch, ops, total = db.batch(), 0, 0
    for dia in dias_conf:
        ags = z.agendamentos(dia, dia, "AGENDADO,AGUARDANDO",
                             "dataHora,agendamentoConfirmado,cliente(id,nome),servico(nome),profissional(nomeAgenda)")
        por = _agrupar_por_cliente(ags, dia.isoformat())
        existentes = {doc.id: (doc.to_dict() or {}) for doc in col.where("data", "==", dia.isoformat()).stream()}
        for k, v in existentes.items():
            if v.get("cel") and v.get("cliente"):
                fones.c.setdefault(str(v["cliente"]), v["cel"])
        vivos = set()
        for cid, x in por.items():
            nome = limpa_nome(x["nomeZelo"])
            doc_id = f"{dia.isoformat()}_{cid}"
            vivos.add(doc_id)
            total += 1
            novo = {"data": dia.isoformat(), "cliente": cid, "nome": nome,
                    "primeiro": nome.split(" ")[0].capitalize() if nome else "", "cel": fones(cid),
                    "itens": x["itens"], "confZelo": x["confZelo"], "ativo": True}
            velho = existentes.get(doc_id)
            if velho and all(velho.get(k) == v for k, v in novo.items()):
                continue      # nada mudou: não gasta gravação
            batch.set(col.document(doc_id), dict(novo, atualizadoEm=agora), merge=True)
            ops += 1
            if ops >= 400:
                batch.commit(); batch, ops = db.batch(), 0
        # quem desmarcou no Zelo some da lista
        for k, v in existentes.items():
            if k not in vivos and v.get("ativo"):
                batch.set(col.document(k), {"ativo": False, "atualizadoEm": agora}, merge=True); ops += 1
    batch.commit()
    log(f"Confirmações: {total} clientes em {', '.join(x.strftime('%d/%m') for x in dias_conf)}")

    # 2) pós-atendimento: atendidas hoje e no último dia aberto antes de hoje
    col = db.collection("voox_posatend")
    col_notas = db.collection("voox_notas")
    try:
        link_google = ((db.collection("voox_config").document("mensagem").get().to_dict() or {}).get("linkAvaliacao") or "").strip()
    except Exception:
        link_google = ""
    datas = sorted({(a.get("dataHora") or "")[:10] for a in d["pagos"]
                    if (a.get("dataHora") or "")[:10] < hoje.isoformat()})[-1:] + [hoje.isoformat()]
    batch, ops, total = db.batch(), 0, 0
    for data_iso in datas:
        existentes = {doc.id: (doc.to_dict() or {}) for doc in col.where("data", "==", data_iso).stream()}
        for k, v in existentes.items():
            if v.get("cel") and v.get("cliente"):
                fones.c.setdefault(str(v["cliente"]), v["cel"])
        for cid, x in _agrupar_por_cliente(d["pagos"], data_iso).items():
            if EH_CRIANCA.search(x["nomeZelo"]):
                continue
            nome = limpa_nome(x["nomeZelo"])
            doc_id = f"{data_iso}_{cid}"
            tk = token_nota(doc_id)
            primeiro = nome.split(" ")[0].capitalize() if nome else ""
            total += 1
            novo = {"data": data_iso, "cliente": cid, "nome": nome, "primeiro": primeiro, "cel": fones(cid),
                    "itens": x["itens"], "tk": tk, "ativo": True}
            velho = existentes.get(doc_id)
            if velho and all(velho.get(k) == v for k, v in novo.items()) and velho.get("notaOk") == link_google:
                continue      # nada mudou: não gasta gravação
            batch.set(col.document(doc_id), dict(novo, atualizadoEm=agora, notaOk=link_google), merge=True)
            # ficha da nota de satisfação (a cliente responde pelo link; o robô nunca mexe na nota)
            batch.set(col_notas.document(tk), {
                "data": data_iso, "pid": doc_id, "c": cid, "nm": nome, "n": primeiro,
                "p": sorted({i["prof"] for i in x["itens"] if i.get("prof")}),
                "s": sorted({i["servico"] for i in x["itens"] if i.get("servico")}),
                "g": link_google}, merge=True)
            ops += 2
            if ops >= 400:
                batch.commit(); batch, ops = db.batch(), 0
    batch.commit()
    log(f"Pós-atendimento: {total} clientes ({', '.join(datas)})")

    # limpeza: listas com mais de 7 dias
    for nome_col in ("voox_confirmacoes", "voox_posatend"):
        velhos = list(db.collection(nome_col).where("data", "<", limite).limit(400).stream())
        if velhos:
            b = db.batch()
            for doc in velhos:
                b.delete(doc.reference)
            b.commit()


def modelo_sugestoes(conjuntos, min_clientes=25):
    """quem faz A também costuma fazer B: P(B | A) contada sobre as clientes (24 meses quando disponível)"""
    cnt, co = Counter(), defaultdict(Counter)
    for S in conjuntos:
        L = [x for x in S if x and not NAO_SUGERIR.search(sem_acento(x))][:25]
        for a in L:
            cnt[a] += 1
            for b in L:
                if a != b:
                    co[a][b] += 1
    populares = {x for x, n in cnt.items() if n >= min_clientes}
    return cnt, co, populares


def sugerir(S, cnt, co, populares, n=2):
    bases = {base_servico(x) for x in S}
    melhor = {}
    for a in S:
        if cnt.get(a, 0) < 25:
            continue
        for t, k in co[a].items():
            if t not in populares or t in S or k < 10 or base_servico(t) in bases:
                continue
            p = k / cnt[a]
            if p > melhor.get(t, (0, ""))[0]:
                melhor[t] = (p, a)
    top = sorted(melhor.items(), key=lambda x: -x[1][0])[:n]
    return [{"s": t, "p": round(p * 100), "a": a} for t, (p, a) in top if p >= 0.15]


def montar_oportunidades(d, sumidas, hist=None):
    """clientes frequentes (3+ serviços em 120 dias, vieram nos últimos 60): campanhas de tipos que nunca fizeram
    (olhando 24 meses quando o histórico está disponível) e sugestão do serviço que clientes parecidas fazem"""
    hist = hist or {}
    hoje = d["hoje"]
    por = defaultdict(lambda: {"nome": "", "datas": [], "serv": Counter()})
    for a in d["pagos"]:
        c = a.get("cliente")
        if not c or not a.get("dataHora"):
            continue
        x = por[str(c["id"])]
        x["nome"] = c.get("nome") or x["nome"]
        x["datas"].append(a["dataHora"][:10])
        x["serv"][limpa_serv((a.get("servico") or {}).get("nome"))] += 1
    todos = dict(hist)
    for cid, x in por.items():
        todos[cid] = set(todos.get(cid, set())) | set(x["serv"])
    cnt, co, populares = modelo_sugestoes(todos.values())
    out = {}
    for cid, x in por.items():
        n = len(x["datas"])
        if n < 3 or cid in sumidas or EH_CRIANCA.search(x["nome"]) or EXCLUIR_NOMES.search(x["nome"]):
            continue
        if (hoje - dt.date.fromisoformat(max(x["datas"]))).days > 60:
            continue
        S = todos[cid]
        servs = " | ".join(sem_acento(v) for v in S)
        alvo = [k for k, (_, faz, publico) in CAMPANHAS.items()
                if not re.search(faz, servs) and (publico is None or re.search(publico, servs))]
        sug = sugerir(S, cnt, co, populares)
        if not alvo and not sug:
            continue
        nome = limpa_nome(x["nome"])
        out[cid] = {"nome": nome, "primeiro": nome.split(" ")[0].capitalize() if nome else "", "n": n,
                    "ult": max(x["datas"]), "servicos": [k for k, _ in x["serv"].most_common(3)], "campanhas": alvo,
                    "sug": sug}
    log(f"Sugestões: modelo com {len(todos)} clientes e {len(populares)} serviços frequentes")
    return out


def retornos_campanhas(docs, d):
    """para cada mensagem enviada, procura se a cliente fez o serviço oferecido depois (até 90 dias)"""
    pagos = defaultdict(list)
    for a in d["pagos"]:
        c = a.get("cliente")
        if c and a.get("dataHora"):
            pagos[str(c["id"])].append(a)
    precos = d["precos"]
    novos = {}
    for cid, x in docs.items():
        env = x.get("enviados") or {}
        ret = dict(x.get("ret") or {})
        mudou = False
        for k, quando in env.items():
            if k in ret or not quando:
                continue
            ini = str(quando)[:10]
            fim = (dt.date.fromisoformat(ini) + dt.timedelta(days=90)).isoformat()
            if k == "sug":
                alvo = base_servico(x.get("sugEnviada") or "")
                ok = (lambda nome, alvo=alvo: alvo and base_servico(nome) == alvo)
            elif k in CAMPANHAS:
                faz = CAMPANHAS[k][1]
                ok = (lambda nome, faz=faz: re.search(faz, sem_acento(nome)))
            else:
                continue
            achou = [a for a in pagos.get(cid, []) if ini < a["dataHora"][:10] <= fim
                     and ok(limpa_serv((a.get("servico") or {}).get("nome")))]
            if achou:
                achou.sort(key=lambda a: a["dataHora"])
                ret[k] = {"d": achou[0]["dataHora"][:10], "s": limpa_serv((achou[0].get("servico") or {}).get("nome")),
                          "v": round(sum(precos.get(str((a.get("servico") or {}).get("id")), 0.0) for a in achou), 2)}
                mudou = True
        if mudou:
            novos[cid] = ret
    return novos


def gravar_oportunidades(db, oport, fones, d=None):
    col = db.collection("voox_oportunidades")
    agora = dt.datetime.now(TZ).isoformat(timespec="minutes")
    todos_docs = {doc.id: (doc.to_dict() or {}) for doc in col.select(["ativo", "enviados", "ret", "sugEnviada", "temEnv"]).stream()}
    existentes = {k for k, v in todos_docs.items() if v.get("ativo")}
    batch, ops, gravadas = db.batch(), 0, 0
    # retorno das mensagens já enviadas (vale também para quem saiu da lista por já ter feito o serviço)
    if d:
        rets = retornos_campanhas({k: v for k, v in todos_docs.items() if v.get("enviados")}, d)
        for cid, ret in rets.items():
            batch.set(col.document(cid), {"ret": ret, "temRet": True, "temEnv": True}, merge=True)
            ops += 1
        for cid, v in todos_docs.items():
            if v.get("enviados") and not v.get("temEnv") and cid not in rets:
                batch.set(col.document(cid), {"temEnv": True}, merge=True)
                ops += 1
        if rets:
            log(f"Oportunidades: {len(rets)} cliente(s) fizeram o serviço oferecido depois da mensagem")
    for cid, o in oport.items():
        if ops >= 400:
            batch.commit(); batch, ops = db.batch(), 0
        cel = fones(cid, essencial=False)
        if not cel:
            continue
        batch.set(col.document(cid), dict(o, cel=cel, ativo=True, atualizadoEm=agora), merge=True)
        ops += 1; gravadas += 1
        if ops >= 400:
            batch.commit(); batch, ops = db.batch(), 0
    for cid in existentes - set(oport):
        batch.set(col.document(cid), {"ativo": False, "atualizadoEm": agora}, merge=True)
        ops += 1
        if ops >= 400:
            batch.commit(); batch, ops = db.batch(), 0
    batch.commit()
    por_camp = Counter(c for o in oport.values() for c in o["campanhas"])
    por_camp["sug"] = sum(1 for o in oport.values() if o.get("sug"))
    log(f"Oportunidades: {gravadas} clientes · " + ", ".join(f"{(CAMPANHAS.get(k) or ('Sugestão',))[0]} {v}" for k, v in por_camp.items()))


# ---------------------------------------------------------------- clientes (12 meses)

POR_PARTE = 400   # clientes por documento (cada um fica bem abaixo do limite de 1 MB, mesmo com 24 meses de visitas)
DIAS_CLI = 730    # histórico de visitas por cliente (24 meses) — o painel filtra por data em cima disso
VERSAO_CLI = 4    # sobe quando o formato muda; força refazer no mesmo dia


def gravar_clientes(z, db, d, resumo, celulares):
    hoje = d["hoje"]
    meta_ref = db.collection("voox_clientes").document("meta")
    meta = meta_ref.get()
    md = (meta.to_dict() or {}) if meta.exists else {}
    if md.get("data") == hoje.isoformat() and md.get("v") == VERSAO_CLI and "--clientes" not in sys.argv:
        log("Clientes: já atualizado hoje")
        return
    desde = hoje - dt.timedelta(days=DIAS_CLI)
    ags = z.agendamentos(desde, hoje, STATUS_PAGO,
                         "dataHora,cliente(id,nome),servico(id,nome),profissional(nomeAgenda)")
    precos = d["precos"]
    # por cliente e por dia: valor de tabela, serviço principal (o mais caro) e profissional dele
    por = defaultdict(lambda: {"nome": "", "todos": set(), "dias": defaultdict(lambda: {"v": 0.0, "top": -1.0, "s": "", "p": ""})})
    tabela_mes = defaultdict(float)
    for a in ags:
        c = a.get("cliente")
        if not c or not a.get("dataHora"):
            continue
        x = por[str(c["id"])]
        x["nome"] = c.get("nome") or x["nome"]
        dia = a["dataHora"][:10]
        preco = precos.get(str((a.get("servico") or {}).get("id")), 0.0)
        x["todos"].add(limpa_serv((a.get("servico") or {}).get("nome")) or "")
        v = x["dias"][dia]
        v["v"] += preco
        tabela_mes[dia[:7]] += preco
        if preco > v["top"]:
            v["top"] = preco
            v["s"] = limpa_serv((a.get("servico") or {}).get("nome")) or ""
            v["p"] = ((a.get("profissional") or {}).get("nomeAgenda") or "").strip()
    # ajuste mês a mês: o preço de tabela vira o faturamento real de serviços daquele mês
    fator_mes = {}
    for m, tab in tabela_mes.items():
        real = (resumo["meses"].get(m) or {}).get("servicos", 0)
        fator_mes[m] = (real / tab) if tab > 0 and real > 0 else 1.0
    serv_ix, prof_ix = {}, {}

    def ix(tab, nome):
        if nome not in tab:
            tab[nome] = len(tab)
        return tab[nome]

    corte12 = (hoje - dt.timedelta(days=365)).isoformat()
    lista = []
    for cid, x in por.items():
        if EXCLUIR_NOMES.search(x["nome"]):
            continue
        ds = sorted(x["dias"])
        vs, v12, q12, s12, p12, d12 = [], 0.0, 0, Counter(), Counter(), []
        for dia in ds:
            o = x["dias"][dia]
            val = o["v"] * fator_mes.get(dia[:7], 1.0)
            # visita compacta: AAMMDD,valor,serviço,profissional (índices nas listas do documento meta)
            vs.append(f"{dia[2:4]}{dia[5:7]}{dia[8:10]},{round(val)},{ix(serv_ix, o['s'])},{ix(prof_ix, o['p'])}")
            if dia >= corte12:
                v12 += val; q12 += 1; d12.append(dia)
                if o["s"]: s12[o["s"]] += 1
                if o["p"]: p12[o["p"]] += 1
        item = {"i": cid, "n": limpa_nome(x["nome"]), "u": ds[-1], "vs": vs,
                "sv": sorted(ix(serv_ix, t) for t in x["todos"] if t),
                # resumo dos últimos 12 meses (compatível com a versão anterior do painel)
                "q": q12, "v": round(v12, 2), "s": s12.most_common(1)[0][0] if s12 else "",
                "r": p12.most_common(1)[0][0] if p12 else ""}
        if d12:
            item["pr"] = d12[0]
            if q12 > 1:
                item["iv"] = round((dt.date.fromisoformat(d12[-1]) - dt.date.fromisoformat(d12[0])).days / (q12 - 1))
        if celulares.get(cid):
            item["c"] = celulares[cid]
        lista.append(item)
    lista.sort(key=lambda o: (-o["v"], -len(o["vs"])))
    partes = [lista[i:i + POR_PARTE] for i in range(0, len(lista), POR_PARTE)] or [[]]
    batch = db.batch()
    for i, parte in enumerate(partes):
        batch.set(db.collection("voox_clientes").document(f"p{i:02d}"), {"itens": parte})
    for i in range(len(partes), md.get("partes", 0)):
        batch.delete(db.collection("voox_clientes").document(f"p{i:02d}"))
    batch.set(meta_ref, {"data": hoje.isoformat(), "atualizadoEm": dt.datetime.now(TZ).isoformat(timespec="minutes"),
                         "partes": len(partes), "total": len(lista), "v": VERSAO_CLI,
                         "sd": [n for n, _ in sorted(serv_ix.items(), key=lambda t: t[1])],
                         "pd": [n for n, _ in sorted(prof_ix.items(), key=lambda t: t[1])],
                         "desde": desde.isoformat()})
    batch.commit()
    log(f"Clientes: {len(lista)} clientes em 24 meses ({len(partes)} partes), {len(ags)} atendimentos")
    # índice leve (id|nome|celular) para a recepção buscar clientes em Pacotes e Indicações — sem valores
    idx = sorted(lista, key=lambda o: o["n"])
    db.collection("voox_cli_idx").document("lista").set({
        "c": [f"{o['i']}|{o['n']}|{o.get('c') or ''}" for o in idx],
        "s": sorted(n for n in serv_ix if n), "atualizadoEm": dt.datetime.now(TZ).isoformat(timespec="minutes")})


# ---------------------------------------------------------------- histórico dia a dia

MESES_HIST = 25          # meses guardados dia a dia (12 + mesmo período do ano anterior + atual)
MESES_POR_RODADA = 5     # meses antigos preenchidos por execução; o resto vem nas próximas


VERSAO_DIAS = 2     # sobe quando o formato muda; meses gravados em versão antiga são refeitos


def _min(a):
    try:
        return int(a.get("duracao") or 0)
    except (TypeError, ValueError):
        return 0


def montar_mes(z, mes_ini, hoje, precos=None):
    precos = precos or {}
    fim = min(date_add_months(mes_ini, 1) - dt.timedelta(days=1), hoje)
    dias = {}
    d = mes_ini
    while d <= fim:
        r = z.resumo_vendas(d, d)
        dias[d.day] = {"fat": r["fat"], "serv": r["servicos"], "prod": r["produtos"], "pac": r["pacotes"],
                       "com": r["comissao"], "res": r["resultado"], "cmd": r["comandas"],
                       "at": 0, "on": 0, "canc": 0, "falt": 0, "cli": set(), "s": Counter(),
                       "h": [0] * 24, "_p": defaultdict(lambda: {"q": 0, "m": 0, "b": 0, "v": 0.0, "c": 0, "f": 0})}
        d += dt.timedelta(days=1)

    def prof(a):
        return ((a.get("profissional") or {}).get("nomeAgenda") or "—").strip()

    def dia_de(a):
        return dias.get(int((a.get("dataHora") or "0000-00-00")[8:10]))

    for a in z.agendamentos(mes_ini, fim, STATUS_PAGO,
                            "dataHora,duracao,agendamentoMobile,cliente(id),servico(id,nome),profissional(nomeAgenda)"):
        x = dia_de(a)
        if not x or not a.get("dataHora"):
            continue
        x["at"] += 1
        if a.get("agendamentoMobile"):
            x["on"] += 1
        if a.get("cliente"):
            x["cli"].add(str(a["cliente"].get("id")))
        x["s"][limpa_serv((a.get("servico") or {}).get("nome")) or "—"] += 1
        x["h"][int(a["dataHora"][11:13])] += 1
        p = x["_p"][prof(a)]
        p["q"] += 1
        p["m"] += _min(a)
        p["v"] += precos.get(str((a.get("servico") or {}).get("id")), 0.0)
    for a in z.agendamentos(mes_ini, fim, STATUS_PERDA, "dataHora,status,profissional(nomeAgenda)"):
        x = dia_de(a)
        if not x:
            continue
        k = "canc" if a.get("status") == "CANCELADO" else "falt"
        x[k] += 1
        x["_p"][prof(a)]["c" if k == "canc" else "f"] += 1
    for a in z.agendamentos(mes_ini, fim, "BLOQUEIO_AGENDA", "dataHora,duracao,profissional(nomeAgenda)"):
        x = dia_de(a)
        if x:
            x["_p"][prof(a)]["b"] += _min(a)

    saida = {}
    for dia, x in dias.items():
        x["cli"] = sorted(x["cli"])
        # listas de {n, ...}: nome pode ter "/" ou "." (ruim como chave) e o Firestore não aceita lista dentro de lista
        x["s"] = [{"n": k, "q": v} for k, v in x["s"].most_common()]
        # valor por profissional: preço de tabela, ajustado para somar o faturamento real de serviços do dia
        tabela = sum(p["v"] for p in x["_p"].values())
        fator = (x["serv"] / tabela) if tabela > 0 else 0
        x["p"] = sorted([{"n": n, "q": p["q"], "m": p["m"], "b": p["b"], "v": round(p["v"] * fator, 2),
                          "c": p["c"], "f": p["f"]} for n, p in x["_p"].items()], key=lambda o: -o["q"])
        del x["_p"]
        saida[str(dia).zfill(2)] = x
    return {"mes": mes_ini.strftime("%Y-%m"), "completo": fim < hoje, "v": VERSAO_DIAS,
            "atualizadoEm": dt.datetime.now(TZ).isoformat(timespec="minutes"), "dias": saida}


def meses_a_atualizar(hoje, existentes):
    atual = hoje.replace(day=1)
    alvo = [atual]
    anterior = date_add_months(atual, -1)
    if hoje.day <= 7 or not existentes.get(anterior.strftime("%Y-%m")):
        alvo.append(anterior)          # no começo do mês o anterior ainda recebe ajustes
    faltando = []
    for i in range(1, MESES_HIST):
        m = date_add_months(atual, -i)
        if m in alvo:
            continue
        if not existentes.get(m.strftime("%Y-%m")):
            faltando.append(m)
    return alvo + faltando[:MESES_POR_RODADA]


def atualizar_diario(z, db, hoje, precos):
    col = db.collection("voox_dias")
    existentes = {}
    for d in col.select(["completo", "v"]).stream():
        x = d.to_dict() or {}
        existentes[d.id] = bool(x.get("completo")) and (x.get("v") or 1) >= VERSAO_DIAS
    meses = meses_a_atualizar(hoje, existentes)
    for m in meses:
        doc = montar_mes(z, m, hoje, precos)
        col.document(doc["mes"]).set(doc)
        log(f"Dia a dia: {doc['mes']} gravado ({len(doc['dias'])} dias)")
    faltam = sum(1 for i in range(1, MESES_HIST)
                 if not existentes.get(date_add_months(hoje.replace(day=1), -i).strftime("%Y-%m"))) - (len(meses) - 1)
    if faltam > 0:
        log(f"Dia a dia: faltam ~{faltam} meses antigos; entram nas próximas execuções")


# ---------------------------------------------------------------- serviços feitos (detalhe por atendimento)

VERSAO_SERV = 1


def montar_servicos_mes(z, mes_ini, hoje, precos, resumo):
    fim = min(date_add_months(mes_ini, 1) - dt.timedelta(days=1), hoje)
    ags = z.agendamentos(mes_ini, fim, STATUS_PAGO,
                         "dataHora,cliente(id,nome),servico(id,nome),profissional(nomeAgenda)")
    # agrupa por dia + cliente + profissional + serviço (q = quantas vezes o serviço foi feito)
    grupos = defaultdict(lambda: {"v": 0.0, "q": 0})
    tabela = 0.0
    for a in ags:
        if not a.get("dataHora"):
            continue
        c = a.get("cliente") or {}
        sv = a.get("servico") or {}
        nome_c = limpa_nome(c.get("nome") or "") or "(sem cliente)"
        k = (a["dataHora"][8:10], nome_c, ((a.get("profissional") or {}).get("nomeAgenda") or "—").strip(),
             limpa_serv(sv.get("nome")) or "—")
        preco = precos.get(str(sv.get("id")), 0.0)
        grupos[k]["v"] += preco
        grupos[k]["q"] += 1
        tabela += preco
    real = (resumo["meses"].get(mes_ini.strftime("%Y-%m")) or {}).get("servicos", 0)
    fator = (real / tabela) if tabela > 0 and real > 0 else 1.0
    sd, cd, pd = {}, {}, {}

    def ix(t, n):
        if n not in t:
            t[n] = len(t)
        return t[n]

    itens = []
    for (dia, cli, prof, serv), g in sorted(grupos.items()):
        # "DD,serviço,cliente,profissional,valor,quantidade" (índices nas listas sd/cd/pd)
        itens.append(f"{dia},{ix(sd, serv)},{ix(cd, cli)},{ix(pd, prof)},{round(g['v'] * fator, 2)},{g['q']}")

    def lista(t):
        return [n for n, _ in sorted(t.items(), key=lambda x: x[1])]

    return {"mes": mes_ini.strftime("%Y-%m"), "completo": fim < hoje, "v": VERSAO_SERV,
            "atualizadoEm": dt.datetime.now(TZ).isoformat(timespec="minutes"),
            "sd": lista(sd), "cd": lista(cd), "pd": lista(pd), "it": itens, "fator": round(fator, 4)}


def gravar_servicos(z, db, hoje, precos, resumo):
    col = db.collection("voox_servicos")
    existentes = {}
    for doc in col.select(["completo", "v"]).stream():
        x = doc.to_dict() or {}
        existentes[doc.id] = bool(x.get("completo")) and (x.get("v") or 0) >= VERSAO_SERV
    for m in meses_a_atualizar(hoje, existentes):
        doc = montar_servicos_mes(z, m, hoje, precos, resumo)
        col.document(doc["mes"]).set(doc)
        log(f"Serviços: {doc['mes']} gravado ({len(doc['it'])} linhas, {len(doc['sd'])} serviços)")


# ---------------------------------------------------------------- horários vagos

GAP_MIN = 45            # buraco mínimo na agenda (minutos)
CANDS_POR_PROF = 12


def _hm(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def montar_vagas(z, d, dia, fones, cands_antigas=None):
    hoje = d["hoje"]
    h = (d.get("horario") or [{}] * 7)[dia.weekday()] or {}
    abre, fecha = h.get("ini") or 8 * 60, h.get("fim") or 20 * 60
    if dia == hoje:
        agora = dt.datetime.now(TZ)
        abre = max(abre, agora.hour * 60 + agora.minute + 60)
    ags = z.agendamentos(dia, dia, STATUS_FUTURO + ",PAGO", "dataHora,duracao,profissional(nomeAgenda),cliente(id)")
    blq = z.agendamentos(dia, dia, "BLOQUEIO_AGENDA", "dataHora,duracao,profissional(nomeAgenda)")
    ocup = defaultdict(list)
    for a in ags + blq:
        if not a.get("dataHora"):
            continue
        p = ((a.get("profissional") or {}).get("nomeAgenda") or "").strip()
        if not p:
            continue
        ini = int(a["dataHora"][11:13]) * 60 + int(a["dataHora"][14:16])
        ocup[p].append((ini, ini + max(15, _min(a) or 30), "cliente" in a))
    # quem trabalha no dia = tem pelo menos um atendimento marcado
    trabalham = {p for p, xs in ocup.items() if any(c for _, _, c in xs)}
    # histórico (120 dias) por cliente: visitas, última vez, e com quem / o quê
    hist = defaultdict(lambda: {"dias": set(), "nome": "", "com": Counter(), "serv": defaultdict(Counter)})
    for a in d["pagos"]:
        c = a.get("cliente") or {}
        if not c.get("id") or not a.get("dataHora"):
            continue
        x = hist[str(c["id"])]
        x["nome"] = c.get("nome") or x["nome"]
        x["dias"].add(a["dataHora"][:10])
        p = ((a.get("profissional") or {}).get("nomeAgenda") or "").strip()
        if p:
            x["com"][p] += 1
            x["serv"][p][limpa_serv((a.get("servico") or {}).get("nome"))] += 1
    marcadas = {str((a.get("cliente") or {}).get("id")) for a in d["futuros"] if a.get("cliente")}
    profs = []
    for p in sorted(trabalham):
        xs = sorted(ocup[p])
        gaps, cur = [], abre
        for i, (a, b, _) in enumerate(xs):
            if a - cur >= GAP_MIN and cur < fecha:
                gaps.append({"i": _hm(cur), "f": _hm(min(a, fecha)), "min": min(a, fecha) - cur, "borda": i == 0})
            cur = max(cur, b)
        if fecha - cur >= GAP_MIN:
            gaps.append({"i": _hm(cur), "f": _hm(fecha), "min": fecha - cur, "borda": True})
        gaps = [g for g in gaps if g["min"] >= GAP_MIN]
        cands = []
        if gaps and cands_antigas is not None:
            lista = [c for c in cands_antigas.get(p, []) if str(c.get("id")) not in marcadas]
            profs.append({"p": p, "gaps": gaps, "cands": lista})
            continue
        if gaps:
            for cid, x in hist.items():
                if not x["com"].get(p) or cid in marcadas or EH_CRIANCA.search(x["nome"]) or EXCLUIR_NOMES.search(x["nome"]):
                    continue
                ds = sorted(x["dias"])
                ult = dt.date.fromisoformat(ds[-1])
                desde = (dia - ult).days
                iv = round((ult - dt.date.fromisoformat(ds[0])).days / (len(ds) - 1)) if len(ds) > 1 else None
                if desde < max(14, round(0.8 * iv) if iv else 21):
                    continue
                atraso = desde / iv if iv else 1.0
                cands.append((x["com"][p] * min(atraso, 3), cid, x, desde, iv))
            cands.sort(key=lambda t: -t[0])
        lista = []
        for _, cid, x, desde, iv in cands[:CANDS_POR_PROF * 2]:
            cel = fones(cid, essencial=False)
            if not cel:
                continue
            nome = limpa_nome(x["nome"])
            item = {"id": cid, "n": nome, "primeiro": nome.split(" ")[0].capitalize() if nome else "", "cel": cel,
                    "s": x["serv"][p].most_common(1)[0][0] if x["serv"][p] else "", "dias": desde, "vezes": x["com"][p]}
            if iv:
                item["iv"] = iv
            lista.append(item)
            if len(lista) >= CANDS_POR_PROF:
                break
        profs.append({"p": p, "gaps": gaps, "cands": lista})
    profs.sort(key=lambda x: -sum(g["min"] for g in x["gaps"]))
    return profs


def gravar_vagas(z, db, d, fones, rapido=False):
    hoje = d["hoje"]
    col = db.collection("voox_vagas")
    dias = [hoje] + proximos_dias_abertos(hoje, d.get("horario"))
    for dia in dias:
        h = (d.get("horario") or [{}] * 7)[dia.weekday()] or {}
        if h and not h.get("aberto", True):
            continue
        antigas = None
        if rapido:
            snap = col.document(dia.isoformat()).get()
            antigas = {pp.get("p"): pp.get("cands") or [] for pp in ((snap.to_dict() or {}).get("profs") or [])} if snap.exists else {}
        profs = montar_vagas(z, d, dia, fones, antigas)
        col.document(dia.isoformat()).set({"data": dia.isoformat(), "profs": profs,
                                           "atualizadoEm": dt.datetime.now(TZ).isoformat(timespec="minutes")}, merge=True)
        log(f"Horários vagos {dia.strftime('%d/%m')}: {sum(1 for p in profs if p['gaps'])} profissionais com buraco, "
            f"{sum(len(p['cands']) for p in profs)} clientes sugeridas")
    for doc in col.where("data", "<", hoje.isoformat()).limit(50).stream():
        doc.reference.delete()


# ---------------------------------------------------------------- comissão de cada profissional por serviço (cadastro de Usuários do Zelo)

def _vinculos(z, inativos):
    lista, page = [], 1
    while page < 40:
        c = z.get("/api/usuarios/vinculos", {"apenasComAcessoSistema": "false", "apenasInativos": "true" if inativos else "false",
                                             "fields": "id,nomeAgenda,apelido,profissional,ativo", "page": page,
                                             "q": "", "rpp": 100})["content"]
        lista += c.get("resultList") or []
        if not c.get("more"):
            break
        page += 1
    return lista


def gravar_comissoes_zelo(z, db):
    usuarios = _vinculos(z, False)
    # Profissional marcada como inativa no Zelo, mas que ainda aparece nos serviços dos últimos 13 meses:
    # também precisa do cadastro de comissão (ex.: vínculo desligado e ela segue atendendo).
    ativos = {(u.get("nomeAgenda") or "").strip() for u in usuarios}
    recentes = set()
    for doc in db.collection("voox_servicos").select(["pd"]).stream():
        recentes.update(n.strip() for n in ((doc.to_dict() or {}).get("pd") or []))
    extras = [u for u in _vinculos(z, True)
              if u.get("profissional") and (u.get("nomeAgenda") or "").strip() in recentes
              and (u.get("nomeAgenda") or "").strip() not in ativos]
    if extras:
        log("Comissões do Zelo: inativas no Zelo mas com serviços recentes: " + ", ".join(u.get("nomeAgenda") or "" for u in extras))
    usuarios += extras
    profs, n_serv = {}, 0
    for u in usuarios:
        if not u.get("profissional"):
            continue
        d = (z.get(f"/api/usuarios/{u['id']}") or {}).get("content") or {}
        serv = {}
        for x in d.get("servicos") or []:
            nome = limpa_serv((x.get("servico") or {}).get("nome"))
            if not nome:
                continue
            item = {"id": str((x.get("servico") or {}).get("id") or "")}
            if x.get("valorFixoComissao") is not None:
                item["fixo"] = float(x["valorFixoComissao"])
            item["pct"] = float(x["percentualComissao"]) if x.get("percentualComissao") is not None else (None if "fixo" in item else 0.0)
            if x.get("possuiComissaoFormaPagamento"):
                item["formaPgto"] = True
            serv[nome] = item
            n_serv += 1
        chave = (u.get("nomeAgenda") or d.get("nome") or "").strip()
        if not chave:
            continue
        profs[chave] = {"id": str(u["id"]), "apelido": u.get("apelido") or "", "ativo": bool(u.get("ativo")),
                        "naoRecebe": bool(d.get("naoRecebeComissao")), "todos": bool(d.get("possuiTodosServicos")),
                        "serv": serv}
    db.collection("voox_fin_cfg").document("comissoes_zelo").set({
        "profs": profs, "atualizadoEm": dt.datetime.now(TZ).isoformat(timespec="minutes")})
    log(f"Comissões do Zelo: {len(profs)} profissionais, {n_serv} serviços com % cadastrado")


# ---------------------------------------------------------------- base completa de clientes (varredura e duplicidades)

BASE_POR_PARTE = 4000


def gravar_base_clientes(z, db, hoje):
    meta_ref = db.collection("voox_base").document("meta")
    md = meta_ref.get().to_dict() or {}
    if md.get("data") == hoje.isoformat() and "--base" not in sys.argv:
        log("Base de clientes: já atualizada hoje")
        return
    campos = ("id,nome,apelido,celular,telefoneFixo,email,dataNascimento,codigoInterno,clienteVip,"
              "clienteDesejaReceberAcaoMarketing,clienteComplemento(cpf,sexo)")
    todos, page = [], 1
    while page <= 80:
        c = z.get("/api/clientes", {"clienteOuProfissionais": "TODOS", "fields": campos, "page": page,
                                    "rpp": 1000, "tipoBuscaCodigo": "CT"})["content"]
        todos += c.get("resultList") or []
        if not c.get("more"):
            break
        page += 1

    def limpo(v):
        return re.sub(r"[|\r\n]+", " ", str(v or "")).strip()

    linhas = []
    for o in todos:
        comp = (o.get("clienteComplemento") or [{}])[0] or {}
        linhas.append("|".join([
            limpo(o.get("id")), limpo(o.get("nome")), limpo(o.get("apelido")),
            re.sub(r"\D", "", str(o.get("celular") or "")), re.sub(r"\D", "", str(o.get("telefoneFixo") or "")),
            limpo(o.get("email")).lower(), limpo(o.get("dataNascimento"))[:10],
            re.sub(r"\D", "", str(comp.get("cpf") or "")), limpo(comp.get("sexo"))[:1],
            "1" if o.get("clienteVip") else "", "1" if o.get("clienteDesejaReceberAcaoMarketing") else "",
            limpo(o.get("codigoInterno"))]))
    partes = [linhas[i:i + BASE_POR_PARTE] for i in range(0, len(linhas), BASE_POR_PARTE)] or [[]]
    batch = db.batch()
    for i, parte in enumerate(partes):
        batch.set(db.collection("voox_base").document(f"p{i:02d}"), {"c": parte})
    for i in range(len(partes), md.get("partes", 0)):
        batch.delete(db.collection("voox_base").document(f"p{i:02d}"))
    batch.set(meta_ref, {"data": hoje.isoformat(), "atualizadoEm": dt.datetime.now(TZ).isoformat(timespec="minutes"),
                         "partes": len(partes), "total": len(linhas),
                         "campos": "id|nome|apelido|celular|fixo|email|nascimento|cpf|sexo|vip|marketing|codigo"})
    batch.commit()
    log(f"Base de clientes: {len(linhas)} cadastros ({len(partes)} partes)")


# ---------------------------------------------------------------- agenda de hoje (tablet da pesquisa de satisfação)

def gravar_agenda_hoje(z, db, hoje):
    ags = z.agendamentos(hoje, hoje, STATUS_FUTURO + "," + STATUS_PAGO,
                         "dataHora,cliente(id,nome),servico(nome),profissional(nomeAgenda)")
    itens = sorted([{"h": a["dataHora"][11:16], "ci": str((a.get("cliente") or {}).get("id")),
                     "c": limpa_nome((a.get("cliente") or {}).get("nome") or ""),
                     "s": limpa_serv((a.get("servico") or {}).get("nome")),
                     "p": ((a.get("profissional") or {}).get("nomeAgenda") or "").strip()}
                    for a in ags if a.get("dataHora") and (a.get("cliente") or {}).get("id")
                    and not EXCLUIR_NOMES.search((a.get("cliente") or {}).get("nome") or "")],
                   key=lambda o: (o["h"], o["c"]))
    ref = db.collection("voox_hoje").document("agenda")
    velho = ref.get().to_dict() or {}
    if velho.get("data") == hoje.isoformat() and velho.get("itens") == itens:
        return
    ref.set({"data": hoje.isoformat(), "itens": itens, "atualizadoEm": dt.datetime.now(TZ).isoformat(timespec="minutes")})
    log(f"Agenda de hoje (tablet): {len(itens)} atendimentos")


# ---------------------------------------------------------------- pacotes (sessões usadas) e indicações

def _nome_chave(n):
    return re.sub(r"\s+", " ", sem_acento(limpa_nome(n or ""))).strip()


def gravar_pacotes(db, pagos, ini_janela):
    """cada atendimento pago de um serviço do pacote, depois da compra, desconta uma sessão.
    Só recalcula dentro da janela de atendimentos lidos; o que ficou antes dela é mantido."""
    pacs = [(doc.id, doc.to_dict() or {}) for doc in db.collection("voox_pacotes").where("ativo", "==", True).stream()]
    if not pacs:
        return
    ini = ini_janela.isoformat()
    por_cli = defaultdict(list)
    for a in pagos:
        c = a.get("cliente") or {}
        if c.get("id") and a.get("dataHora"):
            por_cli[str(c["id"])].append(a)
    agora = dt.datetime.now(TZ).isoformat(timespec="minutes")
    n_mud = 0
    for pid, p in pacs:
        servs = {sem_acento(limpa_serv(x)) for x in (p.get("servs") or [])}
        desde, ate = (p.get("desde") or p.get("compra") or "9999"), (p.get("validade") or "9999-12-31")
        novos = []
        for a in por_cli.get(str(p.get("ci")), []):
            dia = a["dataHora"][:10]
            sv = limpa_serv((a.get("servico") or {}).get("nome"))
            if dia >= ini and desde <= dia <= ate and sem_acento(sv) in servs:
                novos.append({"d": dia, "h": a["dataHora"][11:16], "s": sv,
                              "p": ((a.get("profissional") or {}).get("nomeAgenda") or "").strip()})
        antigos = [x for x in (p.get("auto") or []) if (x.get("d") or "") < ini]
        auto = sorted(antigos + novos, key=lambda x: (x["d"], x.get("h") or "", x["s"]))
        if auto != (p.get("auto") or []):
            db.collection("voox_pacotes").document(pid).update({"auto": auto, "autoEm": agora})
            n_mud += 1
        elif not p.get("autoEm") or p.get("autoEm", "")[:10] != agora[:10]:
            db.collection("voox_pacotes").document(pid).update({"autoEm": agora})
    log(f"Pacotes: {len(pacs)} ativos, {n_mud} com sessão nova")


def gravar_indicacoes(db, pagos):
    """amiga indicada que foi atendida (pago no Zelo) depois do registro vira 'veio'"""
    pend = [(doc.id, doc.to_dict() or {}) for doc in db.collection("voox_indicacoes").where("st", "==", "aguardando").stream()]
    if not pend:
        return
    vis = []
    for a in pagos:
        c = a.get("cliente") or {}
        if c.get("id") and a.get("dataHora"):
            vis.append((a["dataHora"][:10], str(c["id"]), _nome_chave(c.get("nome"))))
    vis.sort()
    n = 0
    for iid, x in pend:
        alvo = _nome_chave(x.get("nome"))
        if not alvo:
            continue
        t = alvo.split(" ")
        desde = str(x.get("criadoEm") or "")[:10]
        for dia, cid, nm in vis:
            if dia < desde or cid == str(x.get("ici")):
                continue
            u = nm.split(" ")
            # mesmo nome, ou mesmo primeiro e último nome
            if nm == alvo or (len(t) >= 2 and len(u) >= 2 and t[0] == u[0] and t[-1] == u[-1]):
                db.collection("voox_indicacoes").document(iid).update(
                    {"st": "veio", "veioEm": dia, "veioCi": cid, "veioNome": nm, "stPor": "robô"})
                n += 1
                break
    log(f"Indicações: {len(pend)} aguardando, {n} vieram agora")


# ---------------------------------------------------------------- clientes faltosas (180 dias, uma vez por dia)

def gravar_faltosas(z, db, d, fones):
    hoje = d["hoje"]
    ref = db.collection("voox_faltosas").document("lista")
    snap = ref.get()
    if snap.exists and (snap.to_dict() or {}).get("data") == hoje.isoformat() and "--faltosas" not in sys.argv:
        log("Faltosas: já atualizado hoje")
        return
    ini = hoje - dt.timedelta(days=180)
    perdas = z.agendamentos(ini, hoje, "FALTOU,CANCELADO", "dataHora,status,cliente(id,nome),servico(nome)")
    pagos = z.agendamentos(ini, hoje, STATUS_PAGO, "dataHora,cliente(id)")
    por = defaultdict(lambda: {"nome": "", "f": set(), "cx": set(), "a": set(), "s": Counter()})
    for a in perdas:
        c = a.get("cliente") or {}
        if not c.get("id") or not a.get("dataHora"):
            continue
        x = por[str(c["id"])]
        x["nome"] = c.get("nome") or x["nome"]
        (x["f"] if a.get("status") == "FALTOU" else x["cx"]).add(a["dataHora"][:10])
        x["s"][limpa_serv((a.get("servico") or {}).get("nome"))] += 1
    for a in pagos:
        c = a.get("cliente") or {}
        if c.get("id") and str(c["id"]) in por and a.get("dataHora"):
            por[str(c["id"])]["a"].add(a["dataHora"][:10])
    prox = {}
    for a in d["futuros"]:
        c = a.get("cliente") or {}
        if c.get("id") and a.get("dataHora"):
            k = str(c["id"])
            if k not in prox or a["dataHora"] < prox[k]:
                prox[k] = a["dataHora"][:16]
    itens = []
    for cid, x in por.items():
        f, a = len(x["f"]), len(x["a"])
        if not f or EXCLUIR_NOMES.search(x["nome"]) or EH_CRIANCA.search(x["nome"]):
            continue
        taxa = f / (f + a)
        if f < 2 and taxa < 0.34:
            continue
        nome = limpa_nome(x["nome"])
        item = {"i": cid, "n": nome, "primeiro": nome.split(" ")[0].capitalize() if nome else "", "f": f, "cx": len(x["cx"]),
                "a": a, "uf": max(x["f"]), "s": x["s"].most_common(1)[0][0] if x["s"] else ""}
        if prox.get(cid):
            item["prox"] = prox[cid]
        itens.append(item)
    itens.sort(key=lambda o: (-o["f"], -(o["f"] / (o["f"] + o["a"]))))
    itens = itens[:400]
    for o in itens:
        o["c"] = fones(o["i"], essencial=bool(o.get("prox")))
    ref.set({"data": hoje.isoformat(), "atualizadoEm": dt.datetime.now(TZ).isoformat(timespec="minutes"), "itens": itens})
    log(f"Faltosas: {len(itens)} clientes · {sum(1 for o in itens if o.get('prox'))} com horário marcado")


# ---------------------------------------------------------------- painel de cada profissional (só para quem tem acesso)

def slug_prof(p):
    return re.sub(r"[^a-z0-9]+", "-", sem_acento(p).lower()).strip("-") or "prof"


def gravar_paineis_prof(z, db, d):
    acessos = {doc.id: (doc.to_dict() or {}) for doc in db.collection("voox_prof_acesso").stream()}
    profs = sorted({v.get("prof") for v in acessos.values() if v.get("prof")})
    if not profs:
        log("Painel das profissionais: nenhuma profissional com acesso cadastrado")
        return
    hoje = d["hoje"]
    m0 = hoje.strftime("%Y-%m")
    m1 = date_add_months(hoje.replace(day=1), -1).strftime("%Y-%m")
    serv = {}
    for m in (m0, m1):
        snap = db.collection("voox_servicos").document(m).get()
        serv[m] = snap.to_dict() if snap.exists else {}
    regras = ((db.collection("voox_fin_cfg").document("comissoes_zelo").get().to_dict() or {}).get("profs") or {})

    def comissao(p, s, v, q):
        r = regras.get(p)
        if not r:
            return None, ""
        if r.get("naoRecebe"):
            return 0.0, "não recebe"
        x = (r.get("serv") or {}).get(s)
        if not x:
            return None, ""
        if x.get("fixo") is not None:
            return float(x["fixo"]) * q, f"R$ {x['fixo']:.0f} fixo"
        pc = float(x.get("pct") or 0)
        return v * pc / 100, f"{pc:g}%"

    ags = z.agendamentos(hoje, hoje + dt.timedelta(days=7), STATUS_FUTURO,
                         "dataHora,duracao,cliente(id,nome),servico(nome),profissional(nomeAgenda)")
    anot = {}
    try:
        ids = sorted({str((a.get("cliente") or {}).get("id")) for a in ags if (a.get("cliente") or {}).get("id")})
        refs = [db.collection("voox_anotacoes").document(i) for i in ids]
        for k in range(0, len(refs), 200):
            for sn in db.get_all(refs[k:k + 200]):
                if sn.exists:
                    fx = [x.get("t") for x in ((sn.to_dict() or {}).get("itens") or []) if x.get("fixa") and x.get("t")]
                    if fx:
                        anot[sn.id] = " · ".join(fx)[:300]
    except Exception as e:
        log(f"Anotações para a agenda das profissionais: {e}")
    corte = (hoje - dt.timedelta(days=90)).isoformat()
    notas = [x.to_dict() for x in db.collection("voox_notas").where("data", ">=", corte).stream()]
    vagas = [x.to_dict() for x in db.collection("voox_vagas").where("data", ">=", hoje.isoformat()).stream()]
    agora = dt.datetime.now(TZ).isoformat(timespec="minutes")
    # metas do mês: realizado de todas (para o ranking) pela mesma base do painel
    metas = (((db.collection("voox_config").document("metas_prof").get().to_dict() or {}).get("m") or {}).get(m0) or {})
    real = defaultdict(float)
    sd0, pd0 = serv[m0].get("sd") or [], serv[m0].get("pd") or []
    for t in serv[m0].get("it") or []:
        a = t.split(",")
        if len(a) >= 6 and int(a[3]) < len(pd0):
            real[pd0[int(a[3])]] += float(a[4])
    pcts = sorted(((real.get(p, 0) / float(x["meta"]), p) for p, x in metas.items() if x.get("meta")), reverse=True)
    posicao = {p: i + 1 for i, (_, p) in enumerate(pcts)}
    restam = sum(1 for i in range(1, 32) if (hoje + dt.timedelta(days=i)).month == hoje.month
                 and (hoje + dt.timedelta(days=i)).weekday() != 6)
    for p in profs:
        meses = {}
        for m, doc in serv.items():
            sd, cd, pd = doc.get("sd") or [], doc.get("cd") or [], doc.get("pd") or []
            itens, tot = [], {"q": 0, "v": 0.0, "com": 0.0, "sem": 0}
            for t in doc.get("it") or []:
                a = t.split(",")
                if len(a) < 6 or int(a[3]) >= len(pd) or pd[int(a[3])] != p:
                    continue
                sv, v, q = sd[int(a[1])], float(a[4]), int(a[5])
                cm, regra = comissao(p, sv, v, q)
                itens.append({"d": f"{m}-{a[0]}", "c": cd[int(a[2])], "s": sv, "v": round(v, 2), "q": q,
                              "cm": round(cm, 2) if cm is not None else None, "r": regra})
                tot["q"] += q; tot["v"] += v
                if cm is None:
                    tot["sem"] += q
                else:
                    tot["com"] += cm
            tot["v"], tot["com"] = round(tot["v"], 2), round(tot["com"], 2)
            meses[m] = {"tot": tot, "itens": sorted(itens, key=lambda o: o["d"], reverse=True)}
        agenda = sorted([{"d": a["dataHora"][:10], "h": a["dataHora"][11:16], "c": limpa_nome((a.get("cliente") or {}).get("nome") or ""),
                          "s": limpa_serv((a.get("servico") or {}).get("nome")), "min": _min(a),
                          **({"an": anot[str((a.get("cliente") or {}).get("id"))]} if anot.get(str((a.get("cliente") or {}).get("id"))) else {})}
                         for a in ags if ((a.get("profissional") or {}).get("nomeAgenda") or "").strip() == p and a.get("dataHora")],
                        key=lambda o: (o["d"], o["h"]))
        nts = sorted([{"d": x.get("data"), "nota": x.get("nota"), "com": x.get("comentario") or "", "s": x.get("s") or [], "n": x.get("n") or ""}
                      for x in notas if p in (x.get("p") or []) and x.get("nota") is not None], key=lambda o: o["d"], reverse=True)[:60]
        vg = []
        for v in vagas:
            for pp in v.get("profs") or []:
                if pp.get("p") == p and pp.get("gaps"):
                    vg.append({"data": v.get("data"), "gaps": pp["gaps"], "cands": pp.get("cands") or []})
        mt = metas.get(p) or {}
        meta = {"m": m0, "meta": float(mt.get("meta") or 0), "real": round(real.get(p, 0), 2), "bonus": float(mt.get("bonus") or 0),
                "superMeta": float(mt.get("superMeta") or 0), "bonusSuper": float(mt.get("bonusSuper") or 0),
                "pos": posicao.get(p, 0), "de": len(pcts), "restam": restam}
        db.collection("voox_prof").document(slug_prof(p)).set({
            "prof": p, "atualizadoEm": agora, "meses": meses, "agenda": agenda, "notas": nts,
            "vagas": sorted(vg, key=lambda o: o["data"]), "meta": meta})
    log(f"Painel das profissionais: {len(profs)} atualizado(s)")


# ---------------------------------------------------------------- modo rápido (a cada 15 min no horário de funcionamento)

def main_rapido():
    """só o que muda durante o dia: faturamento de hoje, confirmações, pós-atendimento e horários vagos"""
    user, pwd = os.environ.get("ZELO_USER"), os.environ.get("ZELO_PASS")
    if not user or not pwd:
        sys.exit("Faltam os secrets ZELO_USER / ZELO_PASS")
    z = Zelo(user, pwd)
    z.login()
    hoje = dt.datetime.now(TZ).date()
    d = {"hoje": hoje, "horario": z.horario(), "precos": z.catalogo()}
    d["pagos"] = z.agendamentos(hoje - dt.timedelta(days=5), hoje, STATUS_PAGO,
                                "dataHora,agendamentoMobile,cliente(id,nome),servico(id,nome),profissional(id,nomeAgenda)")
    d["futuros"] = z.agendamentos(hoje, hoje + dt.timedelta(days=30), STATUS_FUTURO, "dataHora,status,cliente(id)")
    if DRY:
        log(f"--rapido --dry-run: {len(d['pagos'])} atendimentos em 5 dias, {len(d['futuros'])} marcados em 30 dias")
        return
    db = firestore_client()
    agora = dt.datetime.now(TZ).isoformat(timespec="minutes")
    m0 = hoje.replace(day=1)
    # faturamento do mês atual (gráfico) e o dia de hoje (painel)
    try:
        db.collection("voox_painel").document("resumo").set({
            "atualizadoEm": agora, "hoje": hoje.isoformat(), "meses": {m0.strftime("%Y-%m"): z.resumo_vendas(m0, hoje)}}, merge=True)
        dia = montar_mes(z, hoje, hoje, d["precos"])
        db.collection("voox_dias").document(dia["mes"]).set({"dias": dia["dias"], "atualizadoEm": agora}, merge=True)
        log(f"Hoje: R$ {(dia['dias'].get(hoje.strftime('%d')) or {}).get('fat', 0):,.2f}")
    except Exception as e:
        log(f"Faturamento de hoje falhou: {e}")
    fones = Fones(z, {})
    Fones.LIMITE_NOVOS = 40
    for etapa, fn in (("Mensagens do dia", lambda: gravar_mensagens_do_dia(z, db, d, fones)),
                      ("Horários vagos", lambda: gravar_vagas(z, db, d, fones, rapido=True)),
                      ("Agenda de hoje", lambda: gravar_agenda_hoje(z, db, hoje)),
                      ("Pacotes", lambda: gravar_pacotes(db, d["pagos"], hoje - dt.timedelta(days=5))),
                      ("Indicações", lambda: gravar_indicacoes(db, d["pagos"]))):
        try:
            fn()
        except Exception as e:
            log(f"{etapa} falhou: {e}")
    log(f"Rápido: fim em {(time.time() - INICIO):.0f}s · {fones.novos} telefones buscados")


# ---------------------------------------------------------------- main

INICIO = time.time()
LIMITE_MIN = 28     # depois disso as etapas extras ficam para a próxima rodada (o GitHub corta em 40)


def main():
    user, pwd = os.environ.get("ZELO_USER"), os.environ.get("ZELO_PASS")
    if not user or not pwd:
        sys.exit("Faltam os secrets ZELO_USER / ZELO_PASS")
    z = Zelo(user, pwd)
    z.login()
    d = coletar(z)
    resumo = montar_resumo(d)
    candidatas, ultima, futuros = montar_reativacao(d)
    m = resumo["meses"][d["hoje"].strftime("%Y-%m")]
    log(f"Mês atual: R$ {m['fat']:,.2f} · ticket R$ {m['ticket']:,.2f} · "
        f"{resumo['kpis30']['atendimentos']} atendimentos em 30d · {len(candidatas)} sumidas")
    if DRY:
        log("--dry-run: nada gravado no Firebase")
        print(json.dumps({"kpis30": resumo["kpis30"], "servicos90": resumo["servicos90"][:5],
                          "exemploSumida": next(iter(candidatas.values()), None)}, ensure_ascii=False, indent=2))
        mes = montar_mes(z, d["hoje"].replace(day=1), d["hoje"], d["precos"])
        oport = montar_oportunidades(d, set(candidatas))
        log(f"Oportunidades (teste): {len(oport)} clientes · "
            + ", ".join(f"{CAMPANHAS[k][0]} {v}" for k, v in Counter(c for o in oport.values() for c in o['campanhas']).items()))
        log(f"Próximos dias abertos para confirmação: {[x.isoformat() for x in proximos_dias_abertos(d['hoje'], d['horario'])]}")
        anv = z.aniversariantes()
        log(f"Aniversariantes da semana: {len(anv)} · com celular: {sum(1 for a in anv if a['cel'])}")
        hoje_d = mes["dias"].get(str(d["hoje"].day - 1).zfill(2)) or {}
        log(f"Dia a dia (teste do mês atual): {len(mes['dias'])} dias · ontem: R$ {hoje_d.get('fat', 0):,.2f}, "
            f"{hoje_d.get('at', 0)} atendimentos")
        return
    db = firestore_client()
    pagos_por_cli = defaultdict(list)
    for a in d["pagos"]:
        if a.get("cliente"):
            pagos_por_cli[str(a["cliente"].get("id"))].append(a)
    gravar(z, db, resumo, candidatas, ultima, futuros, pagos_por_cli, d["precos"])
    try:
        gravar_aniversariantes(z, db, d["hoje"])
    except Exception as e:
        log(f"Aniversariantes falharam (o resto foi gravado): {e}")
    # o dia a dia alimenta o Painel: roda logo, antes das etapas mais demoradas
    try:
        atualizar_diario(z, db, d["hoje"], d["precos"])
    except Exception as e:
        log(f"Dia a dia falhou (o resto segue): {e}")
    conhecidos = {doc.id: (doc.to_dict() or {}).get("cel") for doc in db.collection("voox_reativacao").select(["cel"]).stream()}
    # celulares já conhecidos da lista de clientes (evita buscar de novo no Zelo, que é a parte lenta)
    hist = {}
    try:
        docs_cli = {doc.id: (doc.to_dict() or {}) for doc in db.collection("voox_clientes").stream()}
        sd = (docs_cli.get("meta") or {}).get("sd") or []
        for k, dd in docs_cli.items():
            if k == "meta":
                continue
            for it in dd.get("itens") or []:
                if it.get("c") and it.get("i") and not conhecidos.get(str(it["i"])):
                    conhecidos[str(it["i"])] = it["c"]
                if it.get("i") and it.get("sv"):
                    hist[str(it["i"])] = {sd[j] for j in it["sv"] if 0 <= j < len(sd)}
    except Exception as e:
        log(f"Celulares da lista de clientes não carregaram: {e}")
    fones = Fones(z, {k: v for k, v in conhecidos.items() if v})
    for etapa, fn in (("Mensagens do dia", lambda: gravar_mensagens_do_dia(z, db, d, fones)),
                      ("Oportunidades", lambda: gravar_oportunidades(db, montar_oportunidades(d, set(candidatas), hist), fones, d)),
                      ("Clientes", lambda: gravar_clientes(z, db, d, resumo, {k: v for k, v in fones.c.items() if v})),
                      ("Serviços", lambda: gravar_servicos(z, db, d["hoje"], d["precos"], resumo)),
                      ("Horários vagos", lambda: gravar_vagas(z, db, d, fones)),
                      ("Comissões do Zelo", lambda: gravar_comissoes_zelo(z, db)),
                      ("Faltosas", lambda: gravar_faltosas(z, db, d, fones)),
                      ("Agenda de hoje", lambda: gravar_agenda_hoje(z, db, d["hoje"])),
                      ("Base de clientes", lambda: gravar_base_clientes(z, db, d["hoje"])),
                      ("Pacotes", lambda: gravar_pacotes(db, d["pagos"], d["hoje"] - dt.timedelta(days=JANELA_DIAS))),
                      ("Indicações", lambda: gravar_indicacoes(db, d["pagos"])),
                      ("Painel das profissionais", lambda: gravar_paineis_prof(z, db, d))):
        minutos = (time.time() - INICIO) / 60
        if minutos > LIMITE_MIN and etapa not in ("Mensagens do dia",):
            log(f"{etapa}: pulado para não estourar o tempo ({minutos:.0f} min); roda na próxima execução")
            continue
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            log(f"{etapa} falhou (o resto foi gravado): {e}")
        log(f"  {etapa}: {time.time() - t0:.0f}s")
    log(f"Fim · {(time.time() - INICIO) / 60:.1f} min · {fones.novos} telefones buscados no Zelo")


if __name__ == "__main__":
    if "--rapido" in sys.argv:
        main_rapido()
    else:
        main()
