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

Secrets necessários no repositório:
  ZELO_USER, ZELO_PASS   -> login de um usuário do Zelo (de preferência só leitura)
  FIREBASE_SA_VOOX       -> JSON da conta de serviço do projeto voox-1956

Rodar local para teste (sem gravar no Firebase):
  ZELO_USER=... ZELO_PASS=... python robo/zelo_sync.py --dry-run
"""
import os
import re
import sys
import json
import time
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

CAMPANHAS = {
    # chave: (nome, regex do que conta como "já faz", regex de quem é público, ou None = todas)
    "facial":    ("Estética facial", r"LIMPEZA DE PELE|PEELING|MICROAGULH|DRENAGEM FACIAL|NUTRI[ÇC][ÃA]O +FACIAL|HYDRA|RADIOFREQ|MASSAGEM +FACIAL|TRATAMENTO", None),
    "corporal":  ("Estética corporal", r"DRENAGEM LINF|MASSAGEM|LIPO|CORRENTE RUSSA|REIKI|ESFOLIA|BRONZE", None),
    "podologia": ("Podologia", r"PODOLOG|PODOPROF|ONICO|UNGUEAL|ESPICULO", r"PEDICURE"),
    "cabelo":    ("Cabelo", r"CORTE|ESCOVA|MECHAS|RETOQUE|TINTURA +\(|COLORA|HIDRATA|SELAGEM|PROGRESSIVA|BABY ?LISS|LAVADO|TONALIZ", None),
}
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
    """celular por cliente, buscando no Zelo só uma vez por execução"""
    def __init__(self, z, conhecidos=None):
        self.z, self.c = z, dict(conhecidos or {})

    def __call__(self, cid):
        cid = str(cid)
        if cid not in self.c:
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
        vivos = set()
        for cid, x in por.items():
            nome = limpa_nome(x["nomeZelo"])
            doc_id = f"{dia.isoformat()}_{cid}"
            vivos.add(doc_id)
            batch.set(col.document(doc_id), {
                "data": dia.isoformat(), "cliente": cid, "nome": nome,
                "primeiro": nome.split(" ")[0].capitalize() if nome else "", "cel": fones(cid),
                "itens": x["itens"], "confZelo": x["confZelo"], "ativo": True, "atualizadoEm": agora}, merge=True)
            ops += 1; total += 1
            if ops >= 400:
                batch.commit(); batch, ops = db.batch(), 0
        # quem desmarcou no Zelo some da lista
        for doc in col.where("data", "==", dia.isoformat()).stream():
            if doc.id not in vivos and (doc.to_dict() or {}).get("ativo"):
                batch.set(doc.reference, {"ativo": False, "atualizadoEm": agora}, merge=True); ops += 1
    batch.commit()
    log(f"Confirmações: {total} clientes em {', '.join(x.strftime('%d/%m') for x in dias_conf)}")

    # 2) pós-atendimento: atendidas hoje e no último dia aberto antes de hoje
    col = db.collection("voox_posatend")
    datas = sorted({(a.get("dataHora") or "")[:10] for a in d["pagos"]
                    if (a.get("dataHora") or "")[:10] < hoje.isoformat()})[-1:] + [hoje.isoformat()]
    batch, ops, total = db.batch(), 0, 0
    for data_iso in datas:
        for cid, x in _agrupar_por_cliente(d["pagos"], data_iso).items():
            if EH_CRIANCA.search(x["nomeZelo"]):
                continue
            nome = limpa_nome(x["nomeZelo"])
            batch.set(col.document(f"{data_iso}_{cid}"), {
                "data": data_iso, "cliente": cid, "nome": nome,
                "primeiro": nome.split(" ")[0].capitalize() if nome else "", "cel": fones(cid),
                "itens": x["itens"], "ativo": True, "atualizadoEm": agora}, merge=True)
            ops += 1; total += 1
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


def montar_oportunidades(d, sumidas):
    """clientes frequentes (3+ serviços em 120 dias, vieram nos últimos 60) que nunca fizeram cada tipo"""
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
    out = {}
    for cid, x in por.items():
        n = len(x["datas"])
        if n < 3 or cid in sumidas or EH_CRIANCA.search(x["nome"]) or EXCLUIR_NOMES.search(x["nome"]):
            continue
        if (hoje - dt.date.fromisoformat(max(x["datas"]))).days > 60:
            continue
        servs = " | ".join(x["serv"])
        alvo = [k for k, (_, faz, publico) in CAMPANHAS.items()
                if not re.search(faz, servs) and (publico is None or re.search(publico, servs))]
        if not alvo:
            continue
        nome = limpa_nome(x["nome"])
        out[cid] = {"nome": nome, "primeiro": nome.split(" ")[0].capitalize() if nome else "", "n": n,
                    "ult": max(x["datas"]), "servicos": [k for k, _ in x["serv"].most_common(3)], "campanhas": alvo}
    return out


def gravar_oportunidades(db, oport, fones):
    col = db.collection("voox_oportunidades")
    agora = dt.datetime.now(TZ).isoformat(timespec="minutes")
    existentes = {doc.id for doc in col.where("ativo", "==", True).select([]).stream()}
    batch, ops, gravadas = db.batch(), 0, 0
    for cid, o in oport.items():
        cel = fones(cid)
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
    log(f"Oportunidades: {gravadas} clientes · " + ", ".join(f"{CAMPANHAS[k][0]} {v}" for k, v in por_camp.items()))


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


# ---------------------------------------------------------------- main

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
    conhecidos = {doc.id: (doc.to_dict() or {}).get("cel") for doc in db.collection("voox_reativacao").select(["cel"]).stream()}
    fones = Fones(z, {k: v for k, v in conhecidos.items() if v})
    for etapa, fn in (("Mensagens do dia", lambda: gravar_mensagens_do_dia(z, db, d, fones)),
                      ("Oportunidades", lambda: gravar_oportunidades(db, montar_oportunidades(d, set(candidatas)), fones))):
        try:
            fn()
        except Exception as e:
            log(f"{etapa} falhou (o resto foi gravado): {e}")
    atualizar_diario(z, db, d["hoje"], d["precos"])


if __name__ == "__main__":
    main()
