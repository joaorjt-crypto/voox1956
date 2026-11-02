"""
Robô Voox 1956 — sincroniza o Zelo com o Firebase (projeto voox-1956).

Roda no GitHub Actions (.github/workflows/zelo-sync.yml). Faz login no Zelo com um
usuário de leitura, puxa vendas e agendamentos, agrega e grava no Firestore:

  voox_painel/resumo        -> indicadores do painel (faturamento por mês, serviços, etc.)
  voox_reativacao/{cliente} -> lista de clientes sumidas (status de envio é do app)

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


def gravar(z, db, resumo, candidatas, ultima, futuros):
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

    batch.set(db.collection("voox_painel").document("resumo"), resumo)
    ops += 1
    batch.commit()
    log("Firestore atualizado")


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
        return
    db = firestore_client()
    gravar(z, db, resumo, candidatas, ultima, futuros)


if __name__ == "__main__":
    main()
