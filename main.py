"""
Jira ↔ Bitbucket Component Sync
================================
Synchronizuje BB repozitáře jako komponenty do Jira projektů.

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

BLACKLIST = {
    s.strip()
    for s in os.environ.get("BB_BLACKLIST", "").split(",")
    if s.strip()
}

# ── BB projekt → Jira projekt mapování ───────────────────────────────────────
# BB_Project_Key : Jira_Key
BB_TO_JIRA = {
    "ABX":    "ABX",
    "ADT":    "ADT",
    "NAA":    "NAA",
    "ASK":    "ASK",
    "ATE":    "ATE",
    "BCT":    "BCT",
    "BIE":    "BIE",
    "BOH":    "BOH",
    "FAS":    "FAS",
    "DRZ":    "DRZ",
    "ELI":    "ELI",
    "EMP":    "EMP",
    "EMT":    "EMT",
    "CSTRAN": "CSTRAN",
    "ENA":    "ENA",
    "FLEX":   "FLEX",
    "GME":    "GME",
    "GSB":    "GSB",
    "IFTKRA": "IFTKRA",
    "JIP":    "JIP",
    "KKE":    "KKE",
    "KAN":    "KAN",
    "KRK":    "KRK",
    "LAS":    "LAS",
    "MAR":    "MAR",
    "NDE":    "NDE",
    "NWE":    "NWE",
    "OKT":    "OKT",
    "PNM":    "PNM",
    "PFD":    "PFD",
    "PIN":    "PIN",
    "PRE":    "PRE",
    "SNP":    "SNP",
    "SUP":    "SUP",
    "SSWI":   "SSWI",
    "SYK":    "SYK",
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

# ── Jira helpers ──────────────────────────────────────────────────────────────

def jira_auth() -> HTTPBasicAuth:
    return HTTPBasicAuth(JIRA_EMAIL, JIRA_API_TOKEN)


def jira_resolve_project_id(key_or_id: str) -> str:
    """Přeloží Jira klíč (PRE) nebo ID (10036) na numerické ID."""
    resp = requests.get(
        f"{JIRA_API}/project/{key_or_id}",
        auth=jira_auth(),
        timeout=30,
    )
    resp.raise_for_status()
    return str(resp.json()["id"])


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
    resp.raise_for_status()
    return resp.json()

# ── Hlavní sync logika ────────────────────────────────────────────────────────

def sync_project(jira_key: str, repos: list) -> None:
    """Synchronizuje komponenty pro jeden Jira projekt."""
    log.info("━━━  %s  ━━━", jira_key)

    # Repozitáře pro tento projekt (mimo blacklist)
    # jira_key může být numerické ID — načteme klíč z projektu
    resp = requests.get(f"{JIRA_API}/project/{jira_key}", auth=jira_auth(), timeout=30)
    resp.raise_for_status()
    real_jira_key = resp.json()["key"]

    bb_slugs = {
        r["slug"]
        for r in repos
        if r.get("project", {}).get("key") in
           {k for k, v in BB_TO_JIRA.items() if v == real_jira_key}
        and r["slug"] not in BLACKLIST
    }
    log.info("  Jira klíč     : %s (ID: %s)", real_jira_key, jira_key)

    if not bb_slugs:
        log.info("  Žádné repozitáře pro tento projekt, přeskakuji.")
        return

    # Existující komponenty v Jiře
    existing = jira_get_components(jira_key)
    existing_by_name = {c["name"]: c for c in existing}
    existing_names = set(existing_by_name.keys())

    to_add    = bb_slugs - existing_names       # v BB ale ne v Jiře
    to_delete = existing_names - bb_slugs       # v Jiře ale ne v BB
    unchanged = bb_slugs & existing_names       # v obou → nic

    log.info("  BB repozitářů : %d", len(bb_slugs))
    log.info("  Beze změny    : %d", len(unchanged))
    log.info("  Přidat        : %d", len(to_add))
    log.info("  Smazat        : %d", len(to_delete))

    for name in sorted(to_delete):
        jira_delete_component(existing_by_name[name]["id"])
        log.info("    ✖ smazána: %s", name)

    for slug in sorted(to_add):
        comp = jira_create_component(jira_key, slug)
        log.info("    ✔ vytvořena: %s → %s", comp["name"], comp.get("description", ""))


def run_sync() -> None:
    log.info("╔══════════════════════════════════════════════╗")
    log.info("║   Jira ↔ BB Component Sync                  ║")
    log.info("╚══════════════════════════════════════════════╝")
    log.info("  SYNC_PROJECT  : %s", SYNC_PROJECT)
    log.info("  BB_BLACKLIST  : %s", BLACKLIST or "(prázdný)")
    log.info("  JIRA_EMAIL    : %s", JIRA_EMAIL)
    log.info("  JIRA_BASE_URL : %s", JIRA_BASE_URL)

    # BB token + repozitáře
    token = bb_get_token()
    repos = bb_get_repos(token)
    log.info("  BB repozitářů : %d", len(repos))

    # Které Jira projekty synchronizovat
    if SYNC_PROJECT.lower() == "all":
        jira_keys = list(set(BB_TO_JIRA.values()))
    else:
        jira_keys = [k.strip().upper() for k in SYNC_PROJECT.split(",")]

    # Přelož všechny klíče/ID na numerická Jira ID (API funguje spolehlivě s ID)
    resolved = {}
    log.info("  Překládám projekty: %s", jira_keys)
    for k in jira_keys:
        project_id = jira_resolve_project_id(k)
        resolved[k] = project_id
        log.info("  Přeloženo %s → ID %s", k, project_id)
    jira_keys = list(resolved.values())

    log.info("  Jira projektů : %d\n", len(jira_keys))

    ok = 0
    errors = 0
    for jira_key in sorted(jira_keys):
        try:
            sync_project(jira_key, repos)
            ok += 1
        except Exception as e:
            log.error("  CHYBA při synchronizaci %s: %s", jira_key, e)
            errors += 1

    log.info("\n  Hotovo — OK: %d, Chyby: %d", ok, errors)


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    while True:
        try:
            run_sync()
        except Exception as e:
            log.error("Kritická chyba: %s", e)
        log.info("Čekám %d minut do dalšího běhu...\n", SYNC_INTERVAL // 60)
        time.sleep(SYNC_INTERVAL)