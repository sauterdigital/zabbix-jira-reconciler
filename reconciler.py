#!/usr/bin/env python3

import json
import os
import sys
import urllib.request
import urllib.error
import base64


# ============================================================
# CONFIG
# ============================================================

JIRA_URL = os.environ["JIRA_URL"].rstrip("/")
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]

ZABBIX_API_URL = os.environ["ZABBIX_API_URL"]
ZABBIX_TOKEN = os.environ["ZABBIX_TOKEN"]

DRY_RUN = True

JIRA_JQL = os.environ["JIRA_JQL"]

VALID_SEVERITIES = [2, 3, 4, 5]

JIRA_TAG = "__zbx_jira_requestkey"


# ============================================================
# HTTP
# ============================================================

def jira_request(method, path, body=None):
    url = f"{JIRA_URL}{path}"

    auth_raw = f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()
    auth = base64.b64encode(auth_raw).decode()

    headers = {
        "Accept": "application/json",
        "Authorization": f"Basic {auth}",
    }

    data = None

    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()

    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode()

            if not raw:
                return None

            return json.loads(raw)

    except urllib.error.HTTPError as e:
        error_body = e.read().decode(errors="replace")

        raise RuntimeError(
            f"Jira HTTP {e.code} em {url}: "
            f"{error_body or '<sem corpo de resposta>'}"
        )

    except Exception as e:
        print("\nERRO JIRA:")
        print(str(e))
        sys.exit(1)


def zabbix_request(method, params, request_id=1):
    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": request_id
    }).encode()

    req = urllib.request.Request(
        ZABBIX_API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json-rpc",
            "Authorization": f"Bearer {ZABBIX_TOKEN}"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode())

    except Exception as e:
        print("\nERRO ZABBIX:")
        print(str(e))
        sys.exit(1)

    if "error" in data:
        print("\nERRO ZABBIX:")
        print(json.dumps(
            data["error"],
            indent=2,
            ensure_ascii=False
        ))
        sys.exit(1)

    return data.get("result", [])


# ============================================================
# JIRA
# ============================================================

def get_open_jira_issues():
    issues = []
    next_page_token = None

    while True:
        body = {
            "jql": JIRA_JQL,
            "maxResults": 100,
            "fields": [
                "summary",
                "status",
                "assignee",
                "reporter",
                "created",
                "issuetype"
            ]
        }

        if next_page_token:
            body["nextPageToken"] = next_page_token

        data = jira_request(
            "POST",
            "/rest/api/3/search/jql",
            body
        )

        page_issues = data.get("issues", [])
        issues.extend(page_issues)

        if data.get("isLast", True):
            break

        next_page_token = data.get("nextPageToken")

        if not next_page_token:
            break

    return issues


# ============================================================
# ZABBIX
# ============================================================

def get_zabbix_problems():
    return zabbix_request(
        "problem.get",
        {
            "output": [
                "eventid",
                "objectid",
                "name",
                "severity",
                "clock"
            ],
            "selectTags": "extend",
            "severities": VALID_SEVERITIES,
            "recent": False
        },
        1
    )


def get_monitored_triggers(trigger_ids):
    if not trigger_ids:
        return []

    return zabbix_request(
        "trigger.get",
        {
            "triggerids": trigger_ids,
            "monitored": True,
            "output": [
                "triggerid",
                "description",
                "status",
                "value",
                "state",
                "priority"
            ],
            "selectHosts": [
                "hostid",
                "host",
                "name",
                "status"
            ]
        },
        2
    )


# ============================================================
# CORRELATION
# ============================================================

def normalize_problem_name(value):
    """
    Normaliza summary Jira e nome do problem Zabbix
    para permitir correlação segura de alertas Kubernetes
    que ainda não possuem __zbx_jira_requestkey.
    """
    return " ".join((value or "").strip().casefold().split())


def extract_jira_keys(problem):
    keys = []

    for tag in problem.get("tags", []):
        if tag.get("tag") == JIRA_TAG:
            value = (tag.get("value") or "").strip()

            if value:
                keys.append(value)

    return keys


def build_zabbix_maps(problems, monitored_trigger_ids):
    monitored_jira_keys = set()
    historical_jira_keys = set()

    # Nomes completos dos problems cujo trigger continua monitored.
    # Importante para Kubernetes/Autopilot, onde a Jira key pode
    # ainda não existir na tag.
    monitored_problem_names = set()

    for problem in problems:
        trigger_id = problem.get("objectid")
        is_monitored = trigger_id in monitored_trigger_ids

        if is_monitored:
            name = normalize_problem_name(problem.get("name"))
            if name:
                monitored_problem_names.add(name)

        jira_keys = extract_jira_keys(problem)

        if not jira_keys:
            continue

        for jira_key in jira_keys:
            historical_jira_keys.add(jira_key)

            if is_monitored:
                monitored_jira_keys.add(jira_key)

    return {
        "historical_jira_keys": historical_jira_keys,
        "monitored_jira_keys": monitored_jira_keys,
        "monitored_problem_names": monitored_problem_names,
    }


# ============================================================
# CLASSIFICAÇÃO DE ALERTAS EFÊMEROS
# ============================================================

def is_kubernetes_alert(summary):
    """
    Fallback para os alerts Kubernetes/Autopilot.

    Esses alerts podem perder completamente a correlação histórica
    no Zabbix quando objetos descobertos via LLD desaparecem.
    """

    text = (summary or "").lower()

    kubernetes_signatures = [
        "kubernetes:",
        "kubernetes nodes:",
        "kubernetes cluster state:",
        "kubernetes pod",
        "kubelet",
        "pod [",
    ]

    return any(
        signature in text
        for signature in kubernetes_signatures
    )


# ============================================================
# RECONCILIATION
# ============================================================

def reconcile(jira_issues, zabbix_maps):
    active = []
    candidates = []
    skipped = []

    historical = zabbix_maps["historical_jira_keys"]
    monitored = zabbix_maps["monitored_jira_keys"]
    monitored_problem_names = zabbix_maps["monitored_problem_names"]

    for issue in jira_issues:
        key = issue.get("key")
        fields = issue.get("fields", {})

        summary = fields.get("summary") or ""

        status = (
            fields.get("status") or {}
        ).get("name", "Sem status")

        assignee = (
            fields.get("assignee") or {}
        ).get("displayName", "Sem responsável")

        issue_type = (
            fields.get("issuetype") or {}
        ).get("name", "Desconhecido")

        item = {
            "key": key,
            "summary": summary,
            "status": status,
            "assignee": assignee,
            "issue_type": issue_type,
        }

        # ----------------------------------------------------
        # 1. Trigger continua efetivamente monitorado
        #    e possui Jira key explícita
        # ----------------------------------------------------

        if key in monitored:
            item["reason"] = (
                "Existe problem associado via __zbx_jira_requestkey "
                "e o trigger continua monitored no Zabbix"
            )

            active.append(item)
            continue

        # ----------------------------------------------------
        # 1B. Kubernetes/Autopilot ativo mas ainda sem Jira tag
        #
        # O Jira já pode ter sido criado enquanto o problem
        # monitored ainda não recebeu __zbx_jira_requestkey.
        # Nesse caso fazemos correspondência EXATA pelo nome.
        # ----------------------------------------------------

        if (
            is_kubernetes_alert(summary)
            and normalize_problem_name(summary) in monitored_problem_names
        ):
            item["reason"] = (
                "Alerta Kubernetes possui problem atualmente monitored "
                "com o mesmo nome/summary, embora ainda não exista "
                "__zbx_jira_requestkey"
            )

            active.append(item)
            continue

        # ----------------------------------------------------
        # 2. Correlação histórica normal
        # ----------------------------------------------------

        if key in historical:
            item["reason"] = (
                "Existe vínculo histórico "
                "__zbx_jira_requestkey, porém o trigger "
                "não está mais monitored"
            )

            candidates.append(item)
            continue

        # ----------------------------------------------------
        # 3. Fallback Kubernetes / Autopilot / LLD
        #
        # Aqui está a correção importante.
        #
        # Passou pelo JQL controlado:
        # - reporter Snowdon
        # - projeto ID
        # - issuetype Alert
        # - não resolvido
        # - filtros adicionais definidos no JQL
        #
        # E possui assinatura Kubernetes.
        #
        # Nesse cenário, a ausência do vínculo Zabbix é
        # justamente uma característica do problema.
        # ----------------------------------------------------

        if is_kubernetes_alert(summary):
            item["reason"] = (
                "Alerta Kubernetes/Autopilot sem vínculo "
                "histórico remanescente no Zabbix; "
                "tratado como objeto efêmero/LLD removido"
            )

            candidates.append(item)
            continue

        # ----------------------------------------------------
        # 4. Qualquer outra situação fica protegida
        # ----------------------------------------------------

        item["reason"] = (
            "Sem trigger monitored, sem vínculo histórico "
            "e não identificado como alerta Kubernetes"
        )

        skipped.append(item)

    return active, candidates, skipped


# ============================================================
# OUTPUT
# ============================================================

def print_section(title, items):
    print()
    print("=" * 100)
    print(title)
    print("=" * 100)

    if not items:
        print("Nenhum.")
        return

    for item in items:
        print()

        print(
            f"{item['key']} | "
            f"{item['status']} | "
            f"{item['assignee']} | "
            f"{item['issue_type']}"
        )

        print(f"  {item['summary']}")
        print(f"  Motivo: {item['reason']}")

        if title.startswith("CANDIDATE"):
            print(
                "  Ação futura: "
                "assign João -> comment -> resolve"
            )


# ============================================================
# MAIN
# ============================================================

def main():
    print()
    print("=" * 100)
    print("ZABBIX <-> JIRA RECONCILIATION")
    print("=" * 100)
    print(f"DRY_RUN: {DRY_RUN}")
    print()

    print("Consultando Jira...")
    jira_issues = get_open_jira_issues()

    print("Consultando Zabbix problems...")
    problems = get_zabbix_problems()

    trigger_ids = list({
        p.get("objectid")
        for p in problems
        if p.get("objectid")
    })

    print("Consultando triggers monitored...")
    monitored_triggers = get_monitored_triggers(
        trigger_ids
    )

    monitored_trigger_ids = {
        t["triggerid"]
        for t in monitored_triggers
    }

    zabbix_maps = build_zabbix_maps(
        problems,
        monitored_trigger_ids
    )

    active, candidates, skipped = reconcile(
        jira_issues,
        zabbix_maps
    )

    print()
    print("=" * 100)
    print("RESUMO")
    print("=" * 100)

    print(
        f"Tickets Jira abertos...............: "
        f"{len(jira_issues)}"
    )

    print(
        f"Problems Zabbix API................: "
        f"{len(problems)}"
    )

    print(
        f"Triggers monitored.................: "
        f"{len(monitored_trigger_ids)}"
    )

    print(
        f"Jira keys históricas no Zabbix.....: "
        f"{len(zabbix_maps['historical_jira_keys'])}"
    )

    print(
        f"Jira keys atualmente monitored.....: "
        f"{len(zabbix_maps['monitored_jira_keys'])}"
    )

    print()
    print(
        f"ACTIVE..............................: "
        f"{len(active)}"
    )

    print(
        f"CANDIDATE...........................: "
        f"{len(candidates)}"
    )

    print(
        f"SKIPPED.............................: "
        f"{len(skipped)}"
    )

    print_section(
        "ACTIVE - NÃO ALTERAR",
        active
    )

    print_section(
        "CANDIDATE - SERIAM RESOLVIDOS",
        candidates
    )

    print_section(
        "SKIPPED - PROTEÇÃO MANUAL",
        skipped
    )

    print()
    print("=" * 100)

    if DRY_RUN:
        print(
            "DRY RUN ATIVO - "
            "NENHUMA ALTERAÇÃO FOI REALIZADA."
        )
    else:
        print(
            "ATENÇÃO: DRY_RUN DESABILITADO."
        )

    print("=" * 100)
    print()


if __name__ == "__main__":
    main()
