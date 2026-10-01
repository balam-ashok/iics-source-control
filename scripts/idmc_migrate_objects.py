"""
IDMC: migrate connections or schedules from the Demo org to the Dfactory org on demand, without a check-in.

Run by .github/workflows/idmc_migrate_objects.yml (Actions > Run workflow). Reuses idmc_cicd.py for the
logins, the org check and the connection logic, and cicd/config.yml for connection_map,
runtime_environment_map, connection passwords/overrides and the expected org names.

Environment:
  IICS_LOGIN_URL, IICS_USERNAME / IICS_PASSWORD (Demo), UAT_IICS_USERNAME / UAT_IICS_PASSWORD (Dfactory)
  OBJECT_TYPE       connection | schedule
  NAMES             names as in Demo, comma or newline separated; * = every one in Demo
  UPDATE_EXISTING   true = overwrite Dfactory copies whose definition differs (default false: report only)
  TEST_CONNECTIONS  true = test each connection afterwards (default true)
  DRY_RUN           true = report only, change nothing
  SECRETS_JSON      JSON of connection passwords (the IDMC_CONNECTION_PASSWORDS secret)
"""

import json
import os
import sys
import urllib.parse

import requests
import yaml

import idmc_cicd as core
from idmc_cicd import DeployError, Org, env_flag, log


def load_config():
    with open(core.CONFIG_PATH, encoding='utf-8') as f:
        cfg = yaml.safe_load(f) or {}
    for k in ('connections', 'schedules', 'publish', 'tests'):
        cfg.setdefault(k, {})
    return cfg


def names_from_env():
    raw = os.environ.get('NAMES', '')
    return [n.strip() for n in raw.replace('\n', ',').split(',') if n.strip()]


def list_names(org, resource):
    r = org.v2('GET', '/' + resource)
    if r.status_code != 200:
        raise DeployError('listing ' + resource + 's in ' + org.label + ' failed: ' + r.text[:300])
    body = r.json()
    return sorted({o.get('name') for o in (body if isinstance(body, list) else []) if o.get('name')})


def schedule_by_name(org, name):
    r = org.v2('GET', '/schedule/name/' + urllib.parse.quote(name, safe=''))
    if r.status_code == 200 and isinstance(r.json(), dict) and r.json().get('id'):
        return r.json()
    return None


def migrate_schedules(src, dst, names, update_existing, dry_run):
    log('\n== Schedules')
    rows = []
    for name in names:
        s = schedule_by_name(src, name)
        row = {'name': name, 'target_hash': '-',
               'demo_hash': core.definition_hash(s, core.VOLATILE_SCHEDULE_KEYS) if s else '-'}
        if not s:
            row['action'] = 'not found in Demo'
        else:
            body = {k: v for k, v in s.items() if k not in core.VOLATILE_SCHEDULE_KEYS}
            t = schedule_by_name(dst, name)
            if not t:
                if dry_run:
                    row['action'] = 'missing (dry run: would create)'
                else:
                    c = dst.v2('POST', '/schedule', json=body)
                    row['action'] = 'created' if c.status_code == 200 else 'create failed: ' + c.text[:200]
            else:
                row['target_hash'] = core.definition_hash(t, core.VOLATILE_SCHEDULE_KEYS)
                if row['target_hash'] == row['demo_hash']:
                    row['action'] = 'exists, in sync'
                elif not update_existing:
                    row['action'] = 'exists, differs (not updated)'
                elif dry_run:
                    row['action'] = 'exists, differs (dry run: would update)'
                else:
                    u = dst.v2('POST', '/schedule/' + t['id'], json=body)
                    row['action'] = 'updated' if u.status_code == 200 else 'update failed: ' + u.text[:200]
        rows.append(row)
        log('  ' + name + ': ' + row['action'] + ' [demo ' + row['demo_hash'] + ' / dfactory ' + row['target_hash'] + ']')
    return rows


def write_summary(otype, dry_run, orgs, rows, error):
    md = '# IDMC migration: ' + otype + 's, Demo → Dfactory ' + ('❌' if error else '✅') + '\n\n'
    md += '**Mode:** ' + ('dry run' if dry_run else 'migrate')
    if orgs:
        md += '  \n**Orgs:** ' + orgs
    md += '\n\n'
    if error:
        md += '> **Failed:** ' + error + '\n\n'
    md += core.table(rows, [('Name', 'name'), ('Result', 'action'), ('Demo hash', 'demo_hash'),
                            ('Dfactory hash', 'target_hash')])
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if path:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(md)


def main():
    cfg = load_config()
    otype = os.environ.get('OBJECT_TYPE', '').strip().lower()
    names = names_from_env()
    dry_run = env_flag('DRY_RUN', False)
    update_existing = env_flag('UPDATE_EXISTING', False)
    test = env_flag('TEST_CONNECTIONS', True)
    try:
        secrets = json.loads(os.environ.get('SECRETS_JSON') or '{}')
    except ValueError:
        secrets = {}

    logins, rows, orgs, error = [], [], '', None
    try:
        if otype not in ('connection', 'schedule'):
            raise DeployError('OBJECT_TYPE must be connection or schedule, not "' + otype + '"')
        if not names:
            raise DeployError('no names given')
        log('Migrating ' + otype + 's: ' + ', '.join(names) + (' (dry run)' if dry_run else ''))
        login_url = os.environ['IICS_LOGIN_URL']
        src = Org('Demo', login_url, os.environ['IICS_USERNAME'], os.environ['IICS_PASSWORD'])
        logins.append(src)
        dst = Org('Dfactory', login_url, os.environ['UAT_IICS_USERNAME'], os.environ['UAT_IICS_PASSWORD'])
        logins.append(dst)
        orgs = src.org_name + ' → ' + dst.org_name
        core.check_orgs(src, dst, cfg)
        if names == ['*']:
            names = list_names(src, otype)
            log('All ' + otype + 's in Demo: ' + str(len(names)))

        if otype == 'connection':
            cfg['connections']['update_existing'] = update_existing
            report = {'connections': []}
            core.sync_connections(src, dst, [], cfg, dry_run, secrets, report, {}, None,
                                  wanted={n: None for n in names}, always_test=test)
            rows = report['connections']
        else:
            rows = migrate_schedules(src, dst, names, update_existing, dry_run)

        bad = [r for r in rows if r['action'].startswith('not found') or 'create failed' in r['action']
               or 'update failed' in r['action']]
        if bad:
            error = 'could not migrate: ' + ', '.join(r['name'] + ' (' + r['action'] + ')' for r in bad)
        log('\n' + ('Migration failed: ' + error if error else 'Migration ' + ('dry run ' if dry_run else '') + 'completed'))
    except (DeployError, KeyError, requests.RequestException) as e:
        error = str(e)
        log('\nMigration failed: ' + error)
    finally:
        write_summary(otype or '?', dry_run, orgs, rows, error)
        for o in logins:
            o.logout()
    sys.exit(0 if error is None else 1)


if __name__ == '__main__':
    main()
