# bb-jira-component-sync

Agent běžící na Railway, který synchronizuje Bitbucket repozitáře jako komponenty do Jira projektů.

Byte díky tomu ví, do kterého repozitáře patří kód daného Jira tiketu.

## Jak to funguje

Agent se pravidelně spouští a pro každý Jira projekt provede diff:

- **repo existuje v BB i v Jiře** → beze změny
- **repo je v BB ale ne v Jiře** → komponenta se přidá
- **komponenta je v Jiře ale repo v BB neexistuje** → komponenta se smaže

Název komponenty = BB repo slug (např. `preciosacomponents-admin`)  
Popis komponenty = URL do repozitáře (např. `https://bitbucket.org/netdirect-custom-solution/preciosacomponents-admin/src`)

## Mapování projektů

BB projekty jsou mapovány na Jira projekty podle klíče (viz `BB_TO_JIRA` v `jira_bb_sync.py`).  
Konvence: BB projekt `CS_Xxx` odpovídá Jira projektu `FC_Xxx`.

## Railway Variables

| Variable | Popis | Příklad |
|---|---|---|
| `BB_CLIENT_ID` | Bitbucket OAuth2 client ID | `xxxxxx` |
| `BB_CLIENT_SECRET` | Bitbucket OAuth2 client secret | `xxxxxx` |
| `BB_WORKSPACE` | BB workspace slug | `netdirect-custom-solution` |
| `JIRA_BASE_URL` | Jira instance URL | `https://netdirect.atlassian.net` |
| `JIRA_EMAIL` | Přihlašovací email do Jiry | `jan@netdirect.cz` |
| `JIRA_API_TOKEN` | Atlassian API token | `xxxxxx` |
| `SYNC_PROJECT` | `all` nebo konkrétní Jira klíč | `PRE` nebo `all` |
| `SYNC_INTERVAL_MIN` | Interval spouštění v minutách | `60` |
| `BB_BLACKLIST` | Čárkou oddělené repo slugy které se přeskočí | `pre-e2e-tests,nde-e2e-tests` |

## Blacklist

Repozitáře na blacklistu agent přeskočí — nevytvoří pro ně komponentu v Jiře.  
Typicky sem patří testovací a deprecated repozitáře:

```
pre-e2e-tests
nde-e2e-tests
nde-test-agent
elima-jakub-e2e-tests
emt-e2e-tests
jip-e2e-tests
perfectdistribution-automaticke-testy
united-bakeries-admin-deprecated
united-bakeries-shop-deprecated
```

## Lokální spuštění

```bash
pip install -r requirements.txt

export BB_CLIENT_ID=xxx
export BB_CLIENT_SECRET=xxx
export JIRA_BASE_URL=https://netdirect.atlassian.net
export JIRA_EMAIL=jan@netdirect.cz
export JIRA_API_TOKEN=xxx
export SYNC_PROJECT=PRE
export SYNC_INTERVAL_MIN=60
export BB_BLACKLIST=pre-e2e-tests,nde-e2e-tests

python main.py
```

## Struktura repozitáře

```
bb-jira-component-sync/
├── jira_bb_sync.py   # hlavní agent
├── requirements.txt  # závislosti
└── README.md         # tento soubor
```
