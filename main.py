"""
Jira ↔ Bitbucket Component Sync + Webhook Sync
================================================
Synchronizuje BB repozitáře jako komponenty do Jira projektů
a zajišťuje přítomnost Claude Code Review webhooku ve všech repozitářích.

Railway Variables:
    # Bitbucket
    BB_CLIENT_ID        OAuth2 client ID
    BB_CLIENT_SECRET    OAuth2 client secret
    BB_WORKSPACE        workspace slug (default: netdirect-custom-solution)

    # Jira
    JIRA_BASE_URL       např. https://netdirect.atlassian.net
    JIRA_EMAIL          přihlašovací email
    JIRA_API_TOKEN      Atlassian API token

    # Chování
    SYNC_PROJECT        "all" = všechny projekty, nebo Jira klíč např. "PRE"
    SYNC_INTERVAL_MIN   interval spouštění v minutách (default: 60)
    BB_BLACKLIST        čárkou oddělené repo slugy které se přeskočí
                        např. "pre-e2e-tests,nde-e2e-tests,nde-test-agent"

    # Webhook sync (volitelné)
    WEBHOOK_SYNC        "true" zapne sync webhooků (default: true)
"""

import os
import time
import logging
import requests
from requests.auth import HTTPBasicAuth

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Konfigurace ───────────────────────────────────────────────────────────────

BB_CLIENT_ID     = os.environ["BB_CLIENT_ID"]
BB_CLIENT_SECRET = os.environ["BB_CLIENT_SECRET"]
BB_WORKSPACE     = os.environ.get("BB_WORKSPACE", "netdirect-custom-solution")
BB_API           = "https://api.bitbucket.org/2.0"
BB_REPO_URL      = f"https://bitbucket.org/{BB_WORKSPACE}"

JIRA_BASE_URL    = os.environ["JIRA_BASE_URL"].rstrip("/")
JIRA_EMAIL       = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN   = os.environ["JIRA_API_TOKEN"]
JIRA_API         = f"{JIRA_BASE_URL}/rest/api/3"

SYNC_PROJECT     = os.environ.get("SYNC_PROJECT", "all").strip()
SYNC_INTERVAL    = int(os.environ.get("SYNC_INTERVAL_MIN", "60")) * 60

WEBHOOK_SYNC     = os.environ.get("WEBHOOK_SYNC", "true").lower() == "true"

# Seznam webhooků — každý má vlastní URL, popis a eventy
WEBHOOKS = [
    {
        "url":    "https://agent-code-review-production.up.railway.app/webhook/bitbucket",
        "desc":   "Claude Code Review",
        "events": ["pullrequest:created", "pullrequest:updated"],
    },
]

# Webhooky k odstranění — identifikace podle description
WEBHOOKS_TO_REMOVE = [
    "Byte PR Learning",
]

BLACKLIST = {
    s.strip()
    for s in os.environ.get("BB_BLACKLIST", "").split(",")
    if s.strip()
}

# ── Bitbucket helpers ─────────────────────────────────────────────────────────

def bb_get_token() -> str:
    resp = requests.post(
        "https://bitbucket.org/site/oauth2/access_token",
        auth=(BB_CLIENT_ID, BB_CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def bb_paginated(url: str, token: str, params: dict = None) -> list:
    headers = {"Authorization": f"Bearer {token}"}
    results = []
    next_url = url
    first = True
    while next_url:
        resp = requests.get(
            next_url,
            headers=headers,
            params=params if first else None,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get("values", []))
        next_url = data.get("next")
        first = False
    return results


def bb_get_repos(token: str) -> list:
    """Vrátí všechny repozitáře workspace."""
    return bb_paginated(
        f"{BB_API}/repositories/{BB_WORKSPACE}",
        token,
        {"pagelen": 100},
    )


def bb_get_project_keys(token: str) -> set:
    """Vrátí množinu všech BB projekt klíčů ve workspace."""
    projects = bb_paginated(
        f"{BB_API}/workspaces/{BB_WORKSPACE}/projects",
        token,
        {"pagelen": 100},
    )
    return {p["key"] for p in projects}

# ── Webhook helpers ───────────────────────────────────────────────────────────

def bb_ensure_webhook(token: str, slug: str) -> None:
    """
    Zkontroluje zda každý webhook ze seznamu WEBHOOKS existuje v repozitáři.
    Pokud ne, přidá ho. Pokud ano, nic nedělá.
    """
    hooks_url = f"{BB_API}/repositories/{BB_WORKSPACE}/{slug}/hooks"

    existing = bb_paginated(hooks_url, token, {"pagelen": 100})
    existing_by_desc = {h.get("description"): h for h in existing}

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    for wh in WEBHOOKS:
        existing_hook = existing_by_desc.get(wh["desc"])

        if existing_hook:
            if existing_hook.get("url") == wh["url"]:
                log.debug("    webhook již existuje: %s (%s)", slug, wh["desc"])
                continue
            # URL se změnila — smaž starý
            del_resp = requests.delete(
                f"{hooks_url}/{existing_hook['uuid']}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            if not del_resp.ok:
                log.error("  Webhook DELETE %s (%s) → %d: %s", slug, wh["desc"], del_resp.status_code, del_resp.text[:300])
                del_resp.raise_for_status()
            log.info("    🗑 webhook smazán (stará URL): %s (%s)", slug, wh["desc"])

        resp = requests.post(
            hooks_url,
            headers=headers,
            json={
                "description": wh["desc"],
                "url":         wh["url"],
                "active":      True,
                "events":      wh["events"],
            },
            timeout=30,
        )
        if not resp.ok:
            log.error("  Webhook POST %s → %d: %s", slug, resp.status_code, resp.text[:300])
            resp.raise_for_status()

        log.info("    🔗 webhook přidán: %s (%s)", slug, wh["desc"])


def sync_webhooks(token: str, repos: list) -> None:
    """Projde všechny repozitáře mimo blacklist a zajistí přítomnost webhooků."""
    log.info("Synchronizuji webhooky (celkem repozitářů: %d)...", len(repos))

    skipped = 0
    errors  = 0

    for repo in repos:
        slug = repo["slug"]
        if slug in BLACKLIST:
            log.debug("  SKIP (blacklist): %s", slug)
            skipped += 1
            continue
        try:
            bb_ensure_webhook(token, slug)
        except Exception as e:
            log.error("  CHYBA webhook %s: %s", slug, e)
            errors += 1

    log.info("Webhooky dokončeny — přeskočeno (blacklist): %d, chyby: %d", skipped, errors)

# ── Jira helpers ──────────────────────────────────────────────────────────────

def jira_auth() -> HTTPBasicAuth:
    return HTTPBasicAuth(JIRA_EMAIL, JIRA_API_TOKEN)


def jira_get_all_projects() -> list:
    """Vrátí všechny Jira projekty (klíč + id)."""
    results = []
    start_at = 0
    max_results = 50
    while True:
        resp = requests.get(
            f"{JIRA_API}/project/search",
            auth=jira_auth(),
            params={"startAt": start_at, "maxResults": max_results},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get("values", []))
        if data.get("isLast", True):
            break
        start_at += max_results
    return results


def jira_get_components(project_key: str) -> list:
    resp = requests.get(
        f"{JIRA_API}/project/{project_key}/components",
        auth=jira_auth(),
        timeout=30,
    )
    if not resp.ok:
        log.error("  Jira GET components %s → %d: %s", project_key, resp.status_code, resp.text[:300])
    resp.raise_for_status()
    return resp.json()


def jira_delete_component(component_id: str) -> None:
    resp = requests.delete(
        f"{JIRA_API}/component/{component_id}",
        auth=jira_auth(),
        timeout=30,
    )
    resp.raise_for_status()


def jira_create_component(project_key: str, slug: str) -> dict:
    resp = requests.post(
        f"{JIRA_API}/component",
        auth=jira_auth(),
        json={
            "name":        slug,
            "description": f"{BB_REPO_URL}/{slug}/src",
            "project":     project_key,
        },
        timeout=30,
    )
    if not resp.ok:
        log.error("  Jira POST component → %d: %s", resp.status_code, resp.text[:300])
    resp.raise_for_status()
    return resp.json()

# ── Mapování BB ↔ Jira ────────────────────────────────────────────────────────

def build_project_mapping(bb_token: str) -> dict:
    """
    Dynamicky sestaví mapování {jira_project_key: jira_project_id}.

    Předpoklad: BB klíč == Jira klíč (např. PRE → PRE).
    Průnik BB projektů a Jira projektů = projekty k synchronizaci.
    """
    log.info("Načítám projekty z Bitbucket...")
    bb_keys = bb_get_project_keys(bb_token)
    log.info("  BB projekty (%d): %s", len(bb_keys), sorted(bb_keys))

    log.info("Načítám projekty z Jira...")
    jira_projects = jira_get_all_projects()
    jira_key_to_id = {p["key"]: str(p["id"]) for p in jira_projects}
    log.info("  Jira projekty (%d): %s", len(jira_key_to_id), sorted(jira_key_to_id.keys()))

    # Průnik — klíče existující v obou systémech
    common_keys = bb_keys & set(jira_key_to_id.keys())
    only_bb = bb_keys - set(jira_key_to_id.keys())
    only_jira = set(jira_key_to_id.keys()) - bb_keys

    if only_bb:
        log.info("  Pouze v BB (přeskakuji): %s", sorted(only_bb))
    if only_jira:
        log.debug("  Pouze v Jira (přeskakuji): %s", sorted(only_jira))

    log.info("  Společné projekty k sync (%d): %s", len(common_keys), sorted(common_keys))
    return {k: jira_key_to_id[k] for k in common_keys}

# ── Hlavní sync logika ────────────────────────────────────────────────────────

def sync_project(jira_key: str, repos: list) -> None:
    """Synchronizuje komponenty pro jeden Jira projekt."""
    bb_slugs = {
        r["slug"]
        for r in repos
        if r.get("project", {}).get("key") == jira_key
        and r["slug"] not in BLACKLIST
    }

    if not bb_slugs:
        log.info("  [%s] Žádné repozitáře pro tento projekt, přeskakuji.", jira_key)
        return

    # Existující komponenty v Jiře
    existing = jira_get_components(jira_key)
    existing_by_name = {c["name"]: c for c in existing}
    existing_names = set(existing_by_name.keys())

    to_add    = bb_slugs - existing_names
    to_delete = existing_names - bb_slugs
    unchanged = bb_slugs & existing_names

    log.info("  [%s] repozitářů: %d, přidat: %d, smazat: %d, beze změny: %d",
             jira_key, len(bb_slugs), len(to_add), len(to_delete), len(unchanged))

    for name in sorted(to_delete):
        jira_delete_component(existing_by_name[name]["id"])
        log.info("    ✖ smazána: %s", name)

    for slug in sorted(to_add):
        comp = jira_create_component(jira_key, slug)
        log.info("    ✔ vytvořena: %s → %s", comp["name"], comp.get("description", ""))


def run_sync() -> None:
    log.info("Spouštím sync — projekt: %s, interval: %d min", SYNC_PROJECT, SYNC_INTERVAL // 60)

    # BB token + repozitáře (sdílené pro obě části syncu)
    token = bb_get_token()
    repos = bb_get_repos(token)

    # ── 1. Webhook sync ───────────────────────────────────────────────────────
    if WEBHOOK_SYNC:
        log.info("── Webhook sync ──────────────────────────────────────────")
        try:
            sync_webhooks(token, repos)
        except Exception as e:
            log.error("Kritická chyba při webhook syncu: %s", e)
    else:
        log.info("Webhook sync je vypnutý (WEBHOOK_SYNC != true)")

    # ── 2. Jira component sync ────────────────────────────────────────────────
    log.info("── Jira component sync ───────────────────────────────────")

    # Dynamické mapování BB ↔ Jira
    project_mapping = build_project_mapping(token)  # {jira_key: jira_id}

    # Filtrování na konkrétní projekt(y) pokud není "all"
    if SYNC_PROJECT.lower() != "all":
        requested = {k.strip().upper() for k in SYNC_PROJECT.split(",")}
        project_mapping = {k: v for k, v in project_mapping.items() if k in requested}
        missing = requested - set(project_mapping.keys())
        if missing:
            log.warning("  Požadované projekty nenalezeny (chybí v BB nebo Jiře): %s", missing)

    log.info("Synchronizuji %d projektů", len(project_mapping))

    ok = 0
    errors = 0
    for jira_key in sorted(project_mapping.keys()):
        try:
            sync_project(jira_key, repos)
            ok += 1
        except Exception as e:
            log.error("  CHYBA při synchronizaci %s: %s", jira_key, e)
            errors += 1

    log.info("Sync dokončen — OK: %d, Chyby: %d", ok, errors)


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    while True:
        try:
            run_sync()
        except Exception as e:
            log.error("Kritická chyba: %s", e)
        log.info("Další běh za %d minut", SYNC_INTERVAL // 60)
        time.sleep(SYNC_INTERVAL)
