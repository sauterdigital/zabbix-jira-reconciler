#!/usr/bin/env python3

import os
import sys
import reconciler as r

from google.cloud import firestore


DRY_RUN = os.getenv("DRY_RUN", "true").lower() in ("1", "true", "yes", "y")
MAX_RESOLVE_PER_RUN = int(os.getenv("MAX_RESOLVE_PER_RUN", "3"))

GRACE_PERIOD_MINUTES = int(
    os.getenv("GRACE_PERIOD_MINUTES", "10")
)

FIRESTORE_COLLECTION = os.getenv(
    "FIRESTORE_COLLECTION",
    "zabbix_jira_reconciler"
)

db = firestore.Client()


ASSIGNEE_ACCOUNT_ID = os.environ["JIRA_ASSIGNEE_ACCOUNT_ID"]

AUDIT_COMMENT = """Alerta encerrado automaticamente, pois o mesmo não se encontra mais ativo no Zabbix.

O registro de auditoria foi realizado e o ambiente foi considerado normalizado.

Origem: Reconciliação automática Zabbix x Jira"""


def adf_comment(text):
    paragraphs = []

    for line in text.split("\n"):
        if line:
            paragraphs.append({
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": line
                    }
                ]
            })
        else:
            paragraphs.append({
                "type": "paragraph",
                "content": []
            })

    return {
        "type": "doc",
        "version": 1,
        "content": paragraphs
    }


def assign_to_joao(issue_key):
    r.jira_request(
        "PUT",
        f"/rest/api/3/issue/{issue_key}/assignee",
        {
            "accountId": ASSIGNEE_ACCOUNT_ID
        }
    )


def comment_already_exists(issue_key):
    data = r.jira_request(
        "GET",
        f"/rest/api/3/issue/{issue_key}/comment?maxResults=100"
    )

    for comment in data.get("comments", []):
        body = comment.get("body", {})
        texts = []

        def extract(node):
            if isinstance(node, dict):
                if node.get("type") == "text":
                    texts.append(node.get("text", ""))

                for value in node.values():
                    extract(value)

            elif isinstance(node, list):
                for value in node:
                    extract(value)

        extract(body)

        full_text = "\n".join(texts)

        if "Origem: Reconciliação automática Zabbix x Jira" in full_text:
            return True

    return False


def add_audit_comment(issue_key):
    if comment_already_exists(issue_key):
        print("   ↳ comentário de auditoria já existe")
        return

    r.jira_request(
        "POST",
        f"/rest/api/3/issue/{issue_key}/comment",
        {
            "body": adf_comment(AUDIT_COMMENT)
        }
    )


def get_resolution_transition(issue_key):
    data = r.jira_request(
        "GET",
        f"/rest/api/3/issue/{issue_key}/transitions"
    )

    transitions = data.get("transitions", [])

    for transition in transitions:
        target = transition.get("to") or {}

        if target.get("name", "").strip().lower() == "resolvido":
            return transition

    return None


def find_transition(issue_key, target_status):
    data = r.jira_request(
        "GET",
        f"/rest/api/3/issue/{issue_key}/transitions"
    )

    for transition in data.get("transitions", []):
        target = transition.get("to") or {}

        if target.get("name", "").strip().lower() == target_status.lower():
            return transition

    return None


def execute_transition(issue_key, transition):
    r.jira_request(
        "POST",
        f"/rest/api/3/issue/{issue_key}/transitions",
        {
            "transition": {
                "id": transition["id"]
            }
        }
    )


def resolve_issue(issue_key):
    # Primeiro tenta resolver diretamente.
    transition = find_transition(issue_key, "Resolvido")

    if transition:
        try:
            execute_transition(issue_key, transition)
            return transition

        except Exception as direct_error:
            print(
                f"   ↳ resolução direta falhou: {direct_error}"
            )
            print(
                "   ↳ tentando fallback via status Aberto..."
            )

    # Fallback:
    # Em andamento -> Aberto -> Resolvido
    to_open = find_transition(issue_key, "Aberto")

    if not to_open:
        raise RuntimeError(
            f"{issue_key}: resolução direta falhou e "
            "não existe transição disponível para Aberto"
        )

    print(
        f"   ↳ movendo para Aberto "
        f"(transition {to_open['id']} / {to_open.get('name')})..."
    )

    execute_transition(issue_key, to_open)

    # Consulta novamente porque o workflow mudou.
    transition = find_transition(issue_key, "Resolvido")

    if not transition:
        raise RuntimeError(
            f"{issue_key}: após mover para Aberto, "
            "nenhuma transição para Resolvido foi encontrada"
        )

    print(
        f"   ↳ resolvendo a partir de Aberto "
        f"(transition {transition['id']} / {transition.get('name')})..."
    )

    execute_transition(issue_key, transition)

    return transition



def state_ref(issue_key):
    return db.collection(FIRESTORE_COLLECTION).document(issue_key)


def clear_missing_state(issue_key):
    state_ref(issue_key).delete()


def apply_grace_period(active, candidates, skipped):
    """
    Retorna:
      ready   -> candidates ausentes por >= grace period
      waiting -> candidates ainda dentro da janela de segurança

    Fail-safe:
      qualquer erro do Firestore aborta a execução antes
      de qualquer alteração no Jira.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)

    print()
    print("=" * 100)
    print("GRACE PERIOD")
    print("=" * 100)
    print("GRACE_PERIOD_MINUTES.....:", GRACE_PERIOD_MINUTES)

    # --------------------------------------------------------
    # Se voltou a ficar ACTIVE, remove qualquer ausência anterior
    # --------------------------------------------------------

    for item in active:
        ref = state_ref(item["key"])
        snap = ref.get()

        if snap.exists:
            print(
                f"{item['key']} | ACTIVE novamente "
                "→ limpando missing_since"
            )

            if not DRY_RUN:
                ref.delete()

    # SKIPPED também não deve carregar estado de ausência antigo.
    for item in skipped:
        ref = state_ref(item["key"])
        snap = ref.get()

        if snap.exists:
            print(
                f"{item['key']} | SKIPPED "
                "→ limpando estado anterior"
            )

            if not DRY_RUN:
                ref.delete()

    ready = []
    waiting = []

    # --------------------------------------------------------
    # Candidates
    # --------------------------------------------------------

    for item in candidates:
        key = item["key"]

        ref = state_ref(key)
        snap = ref.get()

        # Primeira vez que observamos como ausente
        if not snap.exists:
            waiting.append(item)

            print(
                f"{key} | FIRST MISSING "
                f"→ iniciando grace de {GRACE_PERIOD_MINUTES} min"
            )

            if not DRY_RUN:
                ref.set({
                    "issue_key": key,
                    "summary": item.get("summary", ""),
                    "status": item.get("status", ""),
                    "missing_since": now,
                    "updated_at": now,
                })

            continue

        data = snap.to_dict() or {}
        missing_since = data.get("missing_since")

        # Estado inválido = fail closed.
        # Reinicia a janela ao invés de resolver.
        if missing_since is None:
            waiting.append(item)

            print(
                f"{key} | estado sem missing_since "
                "→ reiniciando grace"
            )

            if not DRY_RUN:
                ref.set({
                    "issue_key": key,
                    "summary": item.get("summary", ""),
                    "status": item.get("status", ""),
                    "missing_since": now,
                    "updated_at": now,
                })

            continue

        # Firestore normalmente retorna datetime timezone-aware.
        if missing_since.tzinfo is None:
            missing_since = missing_since.replace(
                tzinfo=timezone.utc
            )

        elapsed = now - missing_since
        elapsed_minutes = elapsed.total_seconds() / 60

        if elapsed_minutes >= GRACE_PERIOD_MINUTES:
            ready.append(item)

            print(
                f"{key} | READY "
                f"→ ausente há {elapsed_minutes:.1f} min"
            )

        else:
            waiting.append(item)

            remaining = (
                GRACE_PERIOD_MINUTES - elapsed_minutes
            )

            print(
                f"{key} | WAITING "
                f"→ ausente há {elapsed_minutes:.1f} min "
                f"(faltam ~{remaining:.1f} min)"
            )

            if not DRY_RUN:
                ref.update({
                    "summary": item.get("summary", ""),
                    "status": item.get("status", ""),
                    "updated_at": now,
                })

    return ready, waiting


def cleanup_resolved_state(issue_key):
    if not DRY_RUN:
        clear_missing_state(issue_key)


def reconcile_now():
    print()
    print("=" * 100)
    print("ZABBIX <-> JIRA RECONCILER")
    print("=" * 100)
    print("DRY_RUN.................:", DRY_RUN)
    print("MAX_RESOLVE_PER_RUN.....:", MAX_RESOLVE_PER_RUN)
    print("GRACE_PERIOD_MINUTES....:", GRACE_PERIOD_MINUTES)
    print()

    print("Consultando Jira...")
    jira_issues = r.get_open_jira_issues()

    print("Consultando Zabbix...")
    problems = r.get_zabbix_problems()

    trigger_ids = list({
        p.get("objectid")
        for p in problems
        if p.get("objectid")
    })

    print("Consultando triggers monitored...")
    monitored_triggers = r.get_monitored_triggers(trigger_ids)

    monitored_trigger_ids = {
        trigger["triggerid"]
        for trigger in monitored_triggers
    }

    maps = r.build_zabbix_maps(
        problems,
        monitored_trigger_ids
    )

    active, candidates, skipped = r.reconcile(
        jira_issues,
        maps
    )

    print()
    print("=" * 100)
    print("RESUMO")
    print("=" * 100)
    print("Jira abertos.............:", len(jira_issues))
    print("Problems Zabbix..........:", len(problems))
    print("Triggers monitored.......:", len(monitored_trigger_ids))
    print("ACTIVE...................:", len(active))
    print("CANDIDATE................:", len(candidates))
    print("SKIPPED..................:", len(skipped))
    print()

    print("=" * 100)
    print("CANDIDATES")
    print("=" * 100)

    for item in candidates:
        print(
            f"{item['key']} | "
            f"{item['status']} | "
            f"{item['summary'][:120]}"
        )

    # --------------------------------------------------------
    # Grace period
    # --------------------------------------------------------

    ready, waiting = apply_grace_period(
        active,
        candidates,
        skipped
    )

    print()
    print("=" * 100)
    print("GRACE SUMMARY")
    print("=" * 100)
    print("READY....................:", len(ready))
    print("WAITING..................:", len(waiting))

    if DRY_RUN:
        print()
        print("=" * 100)
        print("DRY RUN ATIVO")
        print("NENHUMA ALTERAÇÃO SERÁ REALIZADA.")
        print(
            "OBS: missing_since também NÃO é gravado "
            "durante dry run."
        )
        print("=" * 100)
        return

    selected = ready[:MAX_RESOLVE_PER_RUN]

    print()
    print("=" * 100)
    print(f"EXECUTANDO {len(selected)} RESOLUÇÕES")
    print("=" * 100)

    success = 0
    failed = 0

    for number, item in enumerate(selected, start=1):
        key = item["key"]

        print()
        print(f"[{number}/{len(selected)}] {key}")

        try:
            print("   → atribuindo para João...")
            assign_to_joao(key)

            print("   → adicionando comentário...")
            add_audit_comment(key)

            print("   → procurando transição Resolvido...")
            transition = resolve_issue(key)

            print(
                f"   ✅ RESOLVIDO "
                f"(transition {transition['id']} / "
                f"{transition.get('name')})"
            )

            cleanup_resolved_state(key)
            success += 1

        except Exception as exc:
            print(f"   ❌ FALHA: {exc}")
            failed += 1

    print()
    print("=" * 100)
    print("RESULTADO FINAL")
    print("=" * 100)
    print("Resolvidos com sucesso...:", success)
    print("Falhas....................:", failed)
    print("=" * 100)

    if failed:
        sys.exit(2)


if __name__ == "__main__":
    reconcile_now()
