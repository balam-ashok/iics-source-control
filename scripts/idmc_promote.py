#!/usr/bin/env python3
"""
IDMC promote: checks and the manual-migration package for a promotion PR (QA branch -> Production branch).

Used by .github/workflows/idmc-promote.yml. It reads Git and the QA org only. It never logs in to the
Production org and never calls an import, pull or deploy API: Production is migrated by hand.

  validate  Work out the promotion package (the IDMC assets the PR changes) and check it:
              - the QA login reaches the expected org
              - no connection or schedule objects are in the package (they are not promoted)
              - every asset the package uses is checked in (in the package or already on the
                Production branch); connections, runtime environments and schedules are listed
                as things that must already exist in Production
            Writes plan.md (posted on the PR) and plan.json. Exit code 1 when a check fails.

  package   Run the same checks, then export the package from the QA org (an IDMC export .zip the
            developer imports by hand) and write manifest.json, MIGRATION.md and COMMENT.md.

Environment: IICS_LOGIN_URL, QA_IICS_USERNAME, QA_IICS_PASSWORD (from the platform's secret store).
"""

import argparse
import json
import os
import subprocess
import sys
import time

import requests
import yaml

DEFAULTS = {
    'branches': {'qa': 'Demo-Central-Branch', 'production': 'Dfactory-Branch'},
    'orgs': {'qa': ''},
    'labels': {'ready': 'Ready to Deploy'},
    'notify': [],
    'validation': {
        'max_depth': 5,
        # an asset whose type contains one of these words may not be in a promotion package
        'blocked_type_words': ['CONNECTION', 'SCHEDULE'],
        # dependencies of these kinds are never in Git; they must already exist in Production
        'environment_type_words': ['CONNECTION', 'SCHEDULE', 'RUNTIME', 'AGENT'],
    },
}

# names the "uses" API gives some types, mapped to the names in Git file names
TYPE_ALIASES = {'MAPPING': 'DTEMPLATE', 'MAPPING_TASK': 'MTT', 'MAPPINGTASK': 'MTT', 'MCT': 'MTT',
                'SAAS_BSERVICES': 'BSERVICE', 'SAAS_BSERVICE': 'BSERVICE',
                'SYNCHRONIZATION_TASK': 'DSS', 'REPLICATION_TASK': 'DRS'}
CONTAINERS = {'PROJECT', 'FOLDER'}
NOT_ASSETS = CONTAINERS | {'USER', 'USERGROUP', 'ROLE'}
MARKER = '<!-- idmc-promote:plan -->'


class Fail(Exception):
    pass


def log(msg):
    print(msg, flush=True)


def norm_type(t):
    t = str(t or '').upper()
    return TYPE_ALIASES.get(t, t)


def load_config(path):
    cfg = json.loads(json.dumps(DEFAULTS))
    if path and os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            user = yaml.safe_load(f) or {}
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


# ---------------------------------------------------------------------------------------------
# Git: what the PR promotes
# ---------------------------------------------------------------------------------------------

def git(*args):
    return subprocess.run(['git'] + list(args), check=True, capture_output=True, text=True).stdout.strip()


def parse_asset_file(path):
    """Explore/<project>/<folder>/<name>.<TYPE>.<ext> -> (asset path, TYPE), or None."""
    parts = path.split('/')
    if len(parts) < 3 or parts[0] != 'Explore':
        return None
    fname = parts[-1].lstrip('.')
    for suffix in ('.vc.json', '.zip', '.xml', '.json', '.dat'):
        if fname.endswith(suffix):
            fname = fname[:-len(suffix)]
            break
    if '.' not in fname:
        return None
    name, obj_type = fname.rsplit('.', 1)
    if obj_type.upper() in NOT_ASSETS:
        return None
    return '/'.join(parts[1:-1] + [name]), norm_type(obj_type)


def changed_assets(base, head):
    """Assets the PR changes: [{'path', 'type', 'change' (added / modified / deleted), 'files'}]."""
    out = {}
    for line in git('diff', '--name-status', '--no-renames', base + '...' + head).splitlines():
        status, f = line.split('\t', 1)
        key = parse_asset_file(f)
        if not key:
            continue
        a = out.setdefault(key, {'path': key[0], 'type': key[1], 'statuses': set(), 'files': []})
        a['statuses'].add(status[0])
        a['files'].append(f)
    rows = []
    for a in out.values():
        st = a.pop('statuses')
        a['change'] = 'deleted' if st == {'D'} else 'added' if st == {'A'} else 'modified'
        if a['change'] != 'deleted':
            a['commit'] = git('log', '-1', '--format=%h', head, '--', *a['files']) or '-'
        else:
            a['commit'] = '-'
        rows.append(a)
    return sorted(rows, key=lambda r: (r['path'], r['type']))


def assets_in_git(commit):
    """(set of (path, TYPE), set of paths) of every asset in Git at commit."""
    keys = set()
    for f in git('-c', 'core.quotePath=false', 'ls-tree', '-r', '--name-only', commit, '--', 'Explore').splitlines():
        k = parse_asset_file(f)
        if k:
            keys.add(k)
    return keys, {k[0] for k in keys}


# ---------------------------------------------------------------------------------------------
# QA org (read and export only)
# ---------------------------------------------------------------------------------------------

class QAOrg:
    def __init__(self, login_url, username, password):
        self.http = requests.Session()
        r = self.http.post(login_url.rstrip('/') + '/saas/public/core/v3/login',
                           json={'username': username, 'password': password},
                           headers={'Content-Type': 'application/json', 'Accept': 'application/json'}, timeout=120)
        if r.status_code != 200:
            raise Fail('QA org login failed: ' + r.text[:300])
        body = r.json()
        self.session_id = body['userInfo']['sessionId']
        self.base = body['products'][0]['baseApiUrl'].rstrip('/')
        self.org_name = (body.get('userInfo') or {}).get('orgName') or '?'
        self._folders, self._refs = {}, {}
        log('Logged in to the QA org "' + self.org_name + '"')

    def v3(self, method, path, retries=4, **kw):
        headers = kw.pop('headers', {})
        headers.setdefault('INFA-SESSION-ID', self.session_id)
        headers.setdefault('Accept', 'application/json')
        if 'json' in kw:
            headers.setdefault('Content-Type', 'application/json')
        for attempt in range(retries + 1):
            r = self.http.request(method, self.base + '/public/core/v3' + path, headers=headers, timeout=180, **kw)
            if r.status_code in (429, 502, 503, 504) and attempt < retries:
                time.sleep(5 * (attempt + 1))
                continue
            return r

    def folder(self, folder):
        """{(path, TYPE): object} for one Explore project or folder."""
        if folder not in self._folders:
            found, skip = {}, 0
            while True:
                r = self.v3('GET', '/objects', params={'q': "location=='" + folder + "'", 'limit': 200, 'skip': skip})
                if r.status_code != 200:
                    log('  ! could not list ' + folder + ': ' + r.text[:200])
                    break
                objs = r.json().get('objects') or []
                for o in objs:
                    found[(clean_path(o.get('path')), norm_type(o.get('type')))] = o
                if len(objs) < 200:
                    break
                skip += 200
            self._folders[folder] = found
        return self._folders[folder]

    def find(self, path, obj_type):
        listing = self.folder(path.rsplit('/', 1)[0] if '/' in path else path)
        if (path, obj_type) in listing:
            return listing[(path, obj_type)]
        same_path = [o for k, o in listing.items() if k[0] == path]
        return same_path[0] if len(same_path) == 1 else None

    def uses(self, obj_id):
        if obj_id not in self._refs:
            out, skip = [], 0
            while True:
                r = self.v3('GET', '/objects/' + obj_id + '/references', params={'refType': 'Uses', 'limit': 50, 'skip': skip})
                if r.status_code != 200:
                    log('  ! could not read what ' + obj_id + ' uses: ' + r.text[:200])
                    break
                refs = r.json().get('references') or []
                out.extend(refs)
                if len(refs) < 50:
                    break
                skip += 50
            self._refs[obj_id] = out
        return self._refs[obj_id]

    def export(self, object_ids, name, out_file, timeout=900):
        """IDMC export of exactly these objects (no dependencies added) to a .zip file."""
        r = self.v3('POST', '/export', json={'name': name, 'objects': [{'id': i, 'includeDependencies': False}
                                                                       for i in object_ids]})
        if r.status_code != 200:
            raise Fail('export from the QA org failed: ' + r.text[:300])
        export_id = r.json()['id']
        waited, state = 0, None
        while waited <= timeout:
            state = ((self.v3('GET', '/export/' + export_id).json() or {}).get('status') or {}).get('state')
            if str(state).upper() in ('SUCCESSFUL', 'FAILED'):
                break
            time.sleep(5)
            waited += 5
        if str(state).upper() != 'SUCCESSFUL':
            raise Fail('export ' + export_id + ' ended ' + str(state) + ': ' +
                       self.v3('GET', '/export/' + export_id + '/log').text[:500])
        pkg = self.v3('GET', '/export/' + export_id + '/package', headers={'Accept': 'application/zip'})
        if pkg.status_code != 200:
            raise Fail('downloading the export package failed: ' + pkg.text[:300])
        with open(out_file, 'wb') as f:
            f.write(pkg.content)
        return export_id


def clean_path(path):
    parts = list(path) if isinstance(path, list) else str(path or '').strip('/').split('/')
    if parts and parts[0] == 'Explore':
        parts = parts[1:]
    return '/'.join(p for p in parts if p)


def ref_key(ref):
    """(asset path, TYPE) of a reference from the "uses" API."""
    t = norm_type(ref.get('documentType') or ref.get('type'))
    parts = [p for p in clean_path(ref.get('path')).split('/') if p]
    name = ref.get('name')
    if name and (not parts or parts[-1] != name):
        parts.append(name)
    return '/'.join(parts), t


# ---------------------------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------------------------

def has_word(t, words):
    return any(w in t for w in words)


def build_plan(cfg, base, head, qa):
    v = cfg['validation']
    blocked_words, env_words = v['blocked_type_words'], v['environment_type_words']
    package = changed_assets(base, head)
    in_git, git_paths = assets_in_git(head)
    plan = {'base': base, 'head': head, 'qa_org': qa.org_name, 'assets': package, 'errors': [], 'warnings': [],
            'needs_in_production': {}, 'checked_dependencies': 0}

    expected = (cfg.get('orgs') or {}).get('qa')
    if expected and qa.org_name != expected:
        plan['errors'].append('The QA login reached the org "' + qa.org_name + '", not "' + expected +
                              '". Check the QA secrets.')
        return plan
    if not package:
        plan['warnings'].append('This PR changes no IDMC assets.')
        return plan

    keys = {(a['path'], a['type']) for a in package}
    for a in package:
        label = a['type'] + ' ' + a['path']
        if has_word(a['type'], blocked_words):
            a['check'] = 'not allowed'
            plan['errors'].append(label + ': connection and schedule objects are not promoted. Remove it from '
                                  'the promotion and set it up in Production by hand.')
            continue
        if a['change'] == 'deleted':
            a['check'] = 'deleted in QA'
            plan['warnings'].append(label + ' was deleted in QA. Delete it in Production by hand if it should go.')
            continue
        obj = qa.find(a['path'], a['type'])
        if not obj or not obj.get('id'):
            a['check'] = 'not found in QA'
            plan['warnings'].append(label + ' is in Git but not in the QA org (renamed or deleted since the '
                                    'check-in?). Its dependencies could not be checked.')
            continue
        a['qa_id'] = obj['id']
        missing, seen, queue, tree = [], set(), [(obj['id'], 0)], set()
        while queue:
            obj_id, depth = queue.pop(0)
            if not obj_id or obj_id in seen or depth >= int(v['max_depth']):
                continue
            seen.add(obj_id)
            for r in qa.uses(obj_id):
                dep = ref_key(r)
                if not dep[0] or dep[1] in NOT_ASSETS or dep in tree or dep == (a['path'], a['type']):
                    continue
                tree.add(dep)
                if has_word(dep[1], env_words):
                    plan['needs_in_production'].setdefault(dep[1] + ' ' + dep[0], set()).add(a['path'])
                    continue            # environment objects are not followed further
                queue.append((r.get('id'), depth + 1))
                if dep in keys or dep in in_git or dep[0] in git_paths:
                    continue
                missing.append(dep)
        plan['checked_dependencies'] += len(tree)
        if missing:
            a['check'] = 'dependency not checked in'
            plan['errors'].append(label + ' uses assets that are not checked in: ' +
                                  ', '.join(t + ' ' + p for p, t in missing[:8]) + (' ...' if len(missing) > 8 else '') +
                                  '. Check them in from QA so they are part of the promotion.')
        else:
            a['check'] = 'ok'
    plan['needs_in_production'] = {k: sorted(v) for k, v in sorted(plan['needs_in_production'].items())}
    return plan


def render_plan(plan, cfg, pr=None):
    ok = not plan['errors']
    md = [MARKER, '## IDMC promotion check: ' + ('passed ✅' if ok else 'failed ❌'), '']
    md.append('`' + cfg['branches']['qa'] + '` → `' + cfg['branches']['production'] + '` · head `' +
              plan['head'][:7] + '` · QA org: ' + plan.get('qa_org', '?') + ' · Production org is not contacted.')
    md.append('')
    if plan['errors']:
        md.append('**Must be fixed before this can be approved and migrated:**')
        md += ['- ' + e for e in plan['errors']]
        md.append('')
    if plan['warnings']:
        md.append('**Warnings:**')
        md += ['- ' + w for w in plan['warnings']]
        md.append('')
    md.append('### Promotion package (' + str(len(plan['assets'])) + ' assets)')
    if plan['assets']:
        md.append('| Asset | Type | Change | Last commit | Check |')
        md.append('|---|---|---|---|---|')
        for a in plan['assets']:
            md.append('| ' + ' | '.join([a['path'], a['type'], a['change'], a.get('commit', '-'),
                                          a.get('check', '-')]) + ' |')
    else:
        md.append('_none_')
    md.append('')
    md.append('### Must already exist in Production (not promoted, set up by hand)')
    if plan['needs_in_production']:
        md.append('| Object | Used by |')
        md.append('|---|---|')
        for k, users in plan['needs_in_production'].items():
            md.append('| ' + k + ' | ' + ', '.join(users) + ' |')
    else:
        md.append('_none_')
    md.append('')
    md.append('Dependencies checked: ' + str(plan['checked_dependencies']) + '. Next: ' +
              ('a reviewer approves this PR; the package for manual migration is then attached to the run and the '
               'PR is labelled **' + cfg['labels']['ready'] + '**.' if ok else 'fix the items above and check in again.'))
    return '\n'.join(md) + '\n'


# ---------------------------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------------------------

def qa_login():
    try:
        return QAOrg(os.environ['IICS_LOGIN_URL'], os.environ['QA_IICS_USERNAME'], os.environ['QA_IICS_PASSWORD'])
    except KeyError as e:
        raise Fail('missing environment variable ' + str(e) + ' (set it from the platform secret store)')


def write(path, text):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def summary(text):
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as f:
            f.write(text + '\n')


def cmd_validate(args, cfg):
    os.makedirs(args.out, exist_ok=True)
    plan = build_plan(cfg, args.base, args.head, qa_login())
    md = render_plan(plan, cfg, args.pr)
    write(os.path.join(args.out, 'plan.md'), md)
    write(os.path.join(args.out, 'plan.json'), json.dumps(plan, indent=2, default=list))
    summary(md)
    log(md)
    return 1 if plan['errors'] else 0


def cmd_package(args, cfg):
    os.makedirs(args.out, exist_ok=True)
    qa = qa_login()
    plan = build_plan(cfg, args.base, args.head, qa)
    if plan['errors']:
        write(os.path.join(args.out, 'plan.md'), render_plan(plan, cfg, args.pr))
        raise Fail('the promotion checks fail; nothing was packaged:\n- ' + '\n- '.join(plan['errors']))
    short = args.head[:7]
    zip_name = 'idmc-export-pr' + str(args.pr) + '-' + short + '.zip'
    ids = [a['qa_id'] for a in plan['assets'] if a.get('qa_id')]
    if ids:
        log('Exporting ' + str(len(ids)) + ' asset(s) from the QA org')
        export_id = qa.export(ids, 'promotion-pr' + str(args.pr) + '-' + short, os.path.join(args.out, zip_name))
    else:
        zip_name, export_id = None, None
    manifest = {
        'pr': args.pr, 'qa_branch': cfg['branches']['qa'], 'production_branch': cfg['branches']['production'],
        'base_commit': args.base, 'head_commit': args.head, 'tag': args.tag, 'qa_org': qa.org_name,
        'export_package': zip_name, 'qa_export_id': export_id,
        'assets': [{k: a.get(k) for k in ('path', 'type', 'change', 'commit')} for a in plan['assets']],
        'needs_in_production': plan['needs_in_production'], 'warnings': plan['warnings'],
        'run': args.run_url, 'created': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    write(os.path.join(args.out, 'manifest.json'), json.dumps(manifest, indent=2))
    write(os.path.join(args.out, 'plan.md'), render_plan(plan, cfg, args.pr))
    write(os.path.join(args.out, 'MIGRATION.md'), migration_steps(manifest, cfg))
    write(os.path.join(args.out, 'COMMENT.md'), handover_comment(manifest, cfg, args))
    summary(migration_steps(manifest, cfg))
    log('Package ready in ' + args.out)
    return 0


def migration_steps(m, cfg):
    lines = ['# Manual migration: PR #' + str(m['pr']), '',
             'Approved commit `' + m['head_commit'] + '` (tag `' + str(m['tag']) + '`) from `' + m['qa_branch'] +
             '` to `' + m['production_branch'] + '`. Nothing has been changed in Production.', '',
             '## Before you start', '']
    if m['needs_in_production']:
        lines.append('These must already exist in the Production org (they are not part of the package):')
        lines += ['- ' + k + ' (used by ' + ', '.join(v) + ')' for k, v in m['needs_in_production'].items()]
    else:
        lines.append('- No connections, runtime environments or schedules are used.')
    steps = []
    if m['export_package']:
        steps += ['In the Production org, open **Explore → Import** and choose `' + m['export_package'] + '`.',
                  'Map each connection and runtime environment to the Production one, then import.',
                  'Publish the imported processes, guides and taskflows if they are used in Production.']
    else:
        steps.append('This promotion only deletes assets; there is nothing to import.')
    deleted = [a for a in m['assets'] if a['change'] == 'deleted']
    if deleted:
        steps.append('Delete by hand in Production: ' + ', '.join(a['type'] + ' ' + a['path'] for a in deleted) + '.')
    steps.append('Merge the PR so `' + m['production_branch'] + '` matches what is in Production.')
    lines += ['', '## Migrate', ''] + [str(i) + '. ' + s for i, s in enumerate(steps, 1)]
    lines += ['', '## Assets in this promotion', '', '| Asset | Type | Change | Last commit |', '|---|---|---|---|']
    lines += ['| ' + ' | '.join([a['path'], a['type'], a['change'], a['commit'] or '-']) + ' |' for a in m['assets']]
    return '\n'.join(lines) + '\n'


def handover_comment(m, cfg, args):
    who = ' '.join('@' + u.lstrip('@') for u in ([args.author] if args.author else []) + list(cfg.get('notify') or []))
    lines = [who + ' this promotion is approved and **' + cfg['labels']['ready'] + '**. Please migrate it to '
             'Production by hand; the workflow has not changed Production.', '',
             '- **Package:** artifact `promotion-pr-' + str(m['pr']) + '-' + m['head_commit'][:7] + '` on the run: ' +
             str(args.run_url),
             '- **Contains:** ' + (('`' + m['export_package'] + '` (IDMC export from QA), ') if m['export_package'] else '') +
             '`MIGRATION.md` (steps), `manifest.json` (asset list and commit)',
             '- **Commit / tag:** `' + m['head_commit'] + '` / `' + str(m['tag']) + '`',
             '- **Assets:** ' + str(len(m['assets'])) + ', listed in the check comment above', '']
    if m['needs_in_production']:
        lines.append('Check these exist in Production first: ' + ', '.join(m['needs_in_production']) + '.')
    return '\n'.join(lines) + '\n'


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('command', choices=['validate', 'package'])
    p.add_argument('--base', required=True, help='Production branch commit the PR is based on')
    p.add_argument('--head', required=True, help='PR head commit (QA branch)')
    p.add_argument('--pr', type=int, default=0)
    p.add_argument('--config', default='cicd/config.yml')
    p.add_argument('--out', default='out')
    p.add_argument('--tag', default='')
    p.add_argument('--run-url', default='')
    p.add_argument('--author', default='')
    args = p.parse_args()
    cfg = load_config(args.config)
    try:
        sys.exit(cmd_validate(args, cfg) if args.command == 'validate' else cmd_package(args, cfg))
    except Fail as e:
        log('ERROR: ' + str(e))
        summary('**IDMC promote failed:** ' + str(e))
        sys.exit(1)


if __name__ == '__main__':
    main()
