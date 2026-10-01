"""
IDMC CI/CD: promote a Dfactory-Branch commit from the Demo org to the Dfactory org, end to end.

Steps
  1. Resolve the commit to deploy and the assets it changed (git diff on Dfactory-Branch)
  2. Log in to both orgs
  3. Connections   - find the connections the changed assets use (in Demo) and create any that are
                     missing in Dfactory (export/import); report drift with a hash of each definition;
                     optionally update existing ones and set passwords from GitHub secrets
  4. Schedules     - create schedules used by changed mapping tasks if Dfactory lacks them
  5. Pull          - pullByCommitHash into Dfactory (falls back to the individual merged commits)
  6. Schedules     - re-link mapping tasks to their schedule if the pull left them unscheduled
  7. Publish       - publish changed taskflows and Application Integration assets
  8. Test          - run the changed mapping tasks in Dfactory
  9. Summary       - printed to the log and written to the GitHub Actions run summary

Configuration: cicd/config.yml. Credentials come from environment variables (GitHub secrets):
  IICS_LOGIN_URL, IICS_USERNAME, IICS_PASSWORD (Demo org), UAT_IICS_USERNAME, UAT_IICS_PASSWORD (Dfactory org)
Optional: COMMIT_HASH (else the head of the target branch), WAIT_FOR_SHA (a source-branch commit that
must be merged first), DRY_RUN=true, RUN_TESTS=false, SECRETS_JSON (JSON of GitHub secrets for passwords).
"""

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.parse

import requests
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.environ.get('CICD_CONFIG', os.path.join(ROOT, 'cicd', 'config.yml'))

PUBLISH_ORDER = ['AI_CONNECTION', 'AI_SERVICE_CONNECTOR', 'PROCESS', 'GUIDE', 'TASKFLOW']
VOLATILE_CONNECTION_KEYS = {'id', 'orgId', 'createTime', 'updateTime', 'createdBy', 'updatedBy',
                            'federatedId', 'majorUpdateTime', 'agentId', 'runtimeEnvironmentId',
                            'password', 'securityToken', 'internal', 'retryNetworkError'}
VOLATILE_SCHEDULE_KEYS = {'id', 'orgId', 'createTime', 'updateTime', 'createdBy', 'updatedBy'}
# IDMC uses different names for the same type depending on the API (git file suffix vs. v3 APIs)
TYPE_ALIASES = {'MAPPING': 'DTEMPLATE', 'MAPPING_TASK': 'MTT', 'MAPPINGTASK': 'MTT',
                'SYNCHRONIZATION_TASK': 'DSS', 'REPLICATION_TASK': 'DRS', 'WORKFLOW': 'WORKFLOW'}


def norm_type(t):
    t = str(t or '').upper()
    return TYPE_ALIASES.get(t, t)


def add_id(ids, path, obj_type, obj_id):
    """Index an object ID by (path, type), (name, type) and (name, any type)."""
    if not obj_id:
        return
    parts = list(path) if isinstance(path, list) else str(path or '').strip('/').split('/')
    if parts and parts[0] == 'Explore':
        parts = parts[1:]
    if parts:
        # git-style file names: <name>.<TYPE>[.xml|.zip|.json]
        last = parts[-1]
        for suffix in ('.vc.json', '.zip', '.xml', '.json'):
            if last.endswith(suffix):
                last = last[:-len(suffix)]
                if '.' in last and last.rsplit('.', 1)[1].upper() == norm_type(obj_type):
                    last = last.rsplit('.', 1)[0]
                break
        parts[-1] = last
    t = norm_type(obj_type)
    name = parts[-1] if parts else ''
    ids.setdefault(('/'.join(parts), t), obj_id)
    ids.setdefault(('name:' + name.lower(), t), obj_id)
    ids.setdefault(('name:' + name.lower(), '*'), obj_id)


class DeployError(Exception):
    pass


def log(msg=''):
    print(msg, flush=True)


def env_flag(name, default):
    v = os.environ.get(name)
    return default if v in (None, '') else v.strip().lower() == 'true'


# --------------------------------------------------------------------------------------------
# IDMC REST client
# --------------------------------------------------------------------------------------------

class Org:
    """One logged-in IDMC org (v3 login; v2 and v3 calls share the pod base URL)."""

    def __init__(self, label, login_url, username, password):
        self.label = label
        self.http = requests.Session()
        r = self._send('POST', login_url.rstrip('/') + '/saas/public/core/v3/login',
                       json={'username': username, 'password': password}, auth=False)
        if r.status_code != 200:
            raise DeployError(label + ' login failed: ' + r.text[:300])
        body = r.json()
        self.session_id = body['userInfo']['sessionId']
        self.base = body['products'][0]['baseApiUrl'].rstrip('/')      # https://<pod>.<region>.informaticacloud.com/saas
        self.host = self.base[:-len('/saas')] if self.base.endswith('/saas') else self.base
        log('Logged in to ' + label + ' org (' + self.base + ')')

    def _send(self, method, url, auth=True, v2=False, retries=4, **kw):
        headers = kw.pop('headers', {})
        if auth:
            headers.setdefault('icSessionId' if v2 else 'INFA-SESSION-ID', self.session_id)
        if 'json' in kw:
            headers.setdefault('Content-Type', 'application/json')
        headers.setdefault('Accept', 'application/json')
        for attempt in range(retries + 1):
            r = self.http.request(method, url, headers=headers, timeout=120, **kw)
            if r.status_code in (429, 502, 503, 504) and attempt < retries:
                time.sleep(5 * (attempt + 1))
                continue
            return r

    def v3(self, method, path, **kw):
        return self._send(method, self.base + '/public/core/v3' + path, **kw)

    def v2(self, method, path, **kw):
        return self._send(method, self.base + '/api/v2' + path, v2=True, **kw)

    def raw(self, method, url, **kw):
        return self._send(method, url, **kw)

    # ---- helpers -------------------------------------------------------------------------

    def lookup(self, path, obj_type):
        """ID of an object by path and type, or None."""
        r = self.v3('POST', '/lookup', json={'objects': [{'path': path, 'type': obj_type}]})
        self.last_lookup = str(r.status_code) + ' ' + r.text[:300]
        if r.status_code != 200:
            return None
        for o in r.json().get('objects') or []:
            if o.get('id'):
                return o['id']
        return None

    def commit_ids(self, commits):
        """Object IDs in this org from IDMC's commit details (GET /commit/<hash>), indexed by add_id()."""
        ids, self.commit_changes = {}, []
        for c in commits:
            r = self.v3('GET', '/commit/' + c)
            if r.status_code != 200:
                log('  ! ' + self.label + ' org could not read commit ' + c[:7] + ': ' + r.text[:200])
                continue
            changes = r.json().get('changes') or []
            log('  ' + self.label + ' commit ' + c[:7] + ': ' + str(len(changes)) + ' change(s)')
            for ch in changes:
                self.commit_changes.append(ch)
                if not ch.get('id'):
                    continue
                path, name = ch.get('path'), ch.get('name')
                parts = list(path) if isinstance(path, list) else str(path or '').strip('/').split('/')
                parts = [p for p in parts if p]
                if name and (not parts or parts[-1].split('.')[0] != name):
                    parts.append(name)          # path holds only the folder
                add_id(ids, parts, ch.get('type'), ch['id'])
        return ids

    def folder_objects(self, folder):
        """Objects in an Explore folder ('Project/Folder'), via the v3 objects query."""
        r = self.v3('GET', '/objects', params={'q': "location=='" + folder + "'", 'limit': 200})
        if r.status_code != 200:
            self.last_folder = str(r.status_code) + ' ' + r.text[:200]
            return []
        objs = r.json().get('objects') or []
        self.last_folder = str(len(objs)) + ' object(s): ' + ', '.join(
            str(o.get('path', '')).split('/')[-1] + ' (' + str(o.get('type')) + ')' for o in objs[:15])
        return objs

    def find_id(self, ids, path, obj_type):
        """Object ID: commit details (by path, then name), then lookup, then a folder listing."""
        t, name = norm_type(obj_type), path.split('/')[-1]
        obj_id = (ids.get((path, t)) or ids.get(('name:' + name.lower(), t))
                  or ids.get(('name:' + name.lower(), '*')) or self.lookup(path, t))
        if not obj_id and '/' in path:
            objs = self.folder_objects(path.rsplit('/', 1)[0])
            same = [o for o in objs if str(o.get('path', '')).split('/')[-1].lower() == name.lower()]
            typed = [o for o in same if norm_type(o.get('type')) == t]
            hit = (typed or same or [None])[0]
            obj_id = hit.get('id') if hit else None
        if not obj_id:
            log('  ! ' + t + ' ' + path + ' not found in ' + self.label + ' org')
            log('      lookup: ' + getattr(self, 'last_lookup', '-'))
            log('      folder: ' + getattr(self, 'last_folder', '-'))
            if not getattr(self, 'changes_logged', False):
                self.changes_logged = True
                seen = []
                for ch in getattr(self, 'commit_changes', None) or []:
                    row = json.dumps({k: ch.get(k) for k in ('id', 'name', 'type', 'path', 'action')})
                    if row not in seen:
                        seen.append(row)
                for row in seen[:15]:
                    log('      commit change: ' + row)
                if not seen:
                    log('      commit change: (none returned)')
        return obj_id

    def references(self, obj_id):
        """Objects that obj_id uses."""
        out, skip = [], 0
        while True:
            r = self.v3('GET', '/objects/' + obj_id + '/references',
                        params={'refType': 'Uses', 'limit': 50, 'skip': skip})
            if r.status_code != 200:
                log('  ! could not read dependencies of ' + obj_id + ': ' + r.text[:200])
                return out
            refs = r.json().get('references') or []
            out.extend(refs)
            if len(refs) < 50:
                return out
            skip += 50

    def connection_by_name(self, name):
        r = self.v2('GET', '/connection/name/' + urllib.parse.quote(name, safe=''))
        if r.status_code == 200 and isinstance(r.json(), dict) and r.json().get('id'):
            return r.json()
        return None

    def logout(self):
        try:
            self.v3('POST', '/logout')
        except Exception:
            pass


def wait_job(fetch, label, timeout, interval, done_states=('SUCCESSFUL', 'SUCCESS', 'FAILED', 'WARNING')):
    """Poll fetch() -> (state, body) until state is final."""
    waited = 0
    while True:
        state, body = fetch()
        if (state or '').upper() in done_states:
            return state.upper(), body
        if waited >= timeout:
            raise DeployError(label + ' still ' + str(state) + ' after ' + str(timeout) + 's')
        time.sleep(interval)
        waited += interval


# --------------------------------------------------------------------------------------------
# Step 1: what changed (from git, driven by the commit hash)
# --------------------------------------------------------------------------------------------

def git(*args):
    return subprocess.run(['git'] + list(args), cwd=ROOT, check=True,
                          capture_output=True, text=True).stdout.strip()


def resolve_commit(cfg):
    target = cfg['target_branch']
    ref = 'origin/' + target
    try:
        git('fetch', '--quiet', 'origin', target)
    except subprocess.CalledProcessError:
        ref = target
    commit = os.environ.get('COMMIT_HASH', '').strip()
    wait_sha = os.environ.get('WAIT_FOR_SHA', '').strip()
    if not commit and wait_sha:
        # Triggered after the Auto PR: wait until the Demo commit has been merged into the target branch
        for _ in range(30):
            try:
                git('merge-base', '--is-ancestor', wait_sha, ref)
                break
            except subprocess.CalledProcessError:
                log('Waiting for ' + wait_sha[:7] + ' to be merged into ' + target + '...')
                time.sleep(20)
                git('fetch', '--quiet', 'origin', target)
        else:
            raise DeployError(wait_sha[:7] + ' was not merged into ' + target + ' within 10 minutes')
    if not commit:
        commit = git('rev-parse', ref)
    commit = git('rev-parse', commit)
    parents = git('rev-list', '--parents', '-n', '1', commit).split()[1:]
    merged = []
    if len(parents) >= 2:
        merged = [c for c in git('rev-list', '--reverse', '--no-merges', parents[0] + '..' + parents[1]).split() if c]
    return commit, parents, merged


def parse_asset_file(path):
    """Explore/<project>/<folder>/<name>.<TYPE>.<ext> -> (asset path, type)"""
    parts = path.split('/')
    if len(parts) < 3 or parts[0] != 'Explore':
        return None
    fname = parts[-1].lstrip('.')
    for suffix in ('.vc.json', '.zip', '.xml', '.json'):
        if fname.endswith(suffix):
            fname = fname[:-len(suffix)]
            break
    if '.' not in fname:
        return None
    name, obj_type = fname.rsplit('.', 1)
    if obj_type.upper() in ('PROJECT', 'FOLDER'):
        return None
    return '/'.join(parts[1:-1] + [name]), obj_type.upper()


def changed_assets(commit, parents):
    """Assets changed by the commit (first-parent diff, so a merge commit shows everything it brought in)."""
    if not parents:
        return []
    lines = git('diff', '--name-status', '--no-renames', parents[0], commit).splitlines()
    assets = {}
    for line in lines:
        status, path = line.split('\t', 1)
        parsed = parse_asset_file(path)
        if not parsed:
            continue
        a = assets.setdefault(parsed, {'path': parsed[0], 'type': parsed[1], 'deleted': True, 'xml': None})
        if status != 'D':
            a['deleted'] = False
            if path.endswith('.xml') and not path.split('/')[-1].startswith('.'):
                a['xml'] = path
    return sorted(assets.values(), key=lambda a: (a['type'], a['path']))


# --------------------------------------------------------------------------------------------
# Step 3: connections
# --------------------------------------------------------------------------------------------

def definition_hash(obj, volatile):
    clean = {k: v for k, v in obj.items() if k not in volatile}
    if isinstance(clean.get('connParams'), dict):
        clean['connParams'] = {k: v for k, v in clean['connParams'].items()
                               if k not in ('agentId', 'agentGroupId', 'orgId')}
    return hashlib.sha256(json.dumps(clean, sort_keys=True, default=str).encode()).hexdigest()[:12]


def discover_connections(src, assets, cfg, src_ids):
    """Connection name -> source connection ID, for the connections the changed assets use."""
    found = {}
    for a in assets:
        # Application Integration assets don't use Administrator connections
        if a['deleted'] or a['type'].startswith('AI_') or a['type'] in ('PROCESS', 'GUIDE', 'PROCESS_OBJECT'):
            continue
        obj_id = src.find_id(src_ids, a['path'], a['type'])
        if not obj_id:
            continue
        for ref in src.references(obj_id):
            rtype = str(ref.get('documentType') or ref.get('type') or '')
            if 'connection' in rtype.lower() and not rtype.upper().startswith('AI_'):
                name = ref.get('name') or (ref.get('path') or '').split('/')[-1]
                if name:
                    found[name] = ref.get('id')
    for name in cfg['connections'].get('always_include') or []:
        found.setdefault(name, None)
    return found


def export_import(src, dst, object_ids, cfg, label):
    """Export objects from src and import them into dst (existing same-name objects are reused, not overwritten)."""
    r = src.v3('POST', '/export', json={'name': 'cicd-' + label + '-' + str(int(time.time())),
                                        'objects': [{'id': i, 'includeDependencies': True} for i in object_ids]})
    if r.status_code != 200:
        raise DeployError('export failed: ' + r.text[:300])
    export_id = r.json()['id']
    state, _ = wait_job(lambda: (src.v3('GET', '/export/' + export_id).json().get('status', {}).get('state'), None),
                        'export', 600, 5)
    if state != 'SUCCESSFUL':
        raise DeployError('export ' + export_id + ' ended ' + state + ': ' + src.v3('GET', '/export/' + export_id + '/log').text[:500])
    pkg = src.v3('GET', '/export/' + export_id + '/package', headers={'Accept': 'application/zip'})
    if pkg.status_code != 200:
        raise DeployError('export package download failed: ' + pkg.text[:300])

    up = dst.v3('POST', '/import/package', files={'package': ('package.zip', pkg.content, 'application/zip')})
    if up.status_code != 200:
        raise DeployError('import upload failed: ' + up.text[:300])
    job_id = up.json()['jobId']
    spec = {'defaultConflictResolution': 'REUSE'}
    rte_map = cfg.get('runtime_environment_map') or {}
    if rte_map:
        specs = []
        for s_name, t_name in rte_map.items():
            s_id, t_id = src.lookup(s_name, 'AGENTGROUP'), dst.lookup(t_name, 'AGENTGROUP')
            if s_id and t_id:
                specs.append({'sourceObjectId': s_id, 'targetObjectId': t_id})
        if specs:
            spec['objectSpecification'] = specs
    st = dst.v3('POST', '/import/' + job_id, json={'name': 'cicd-' + label, 'importSpecification': spec})
    if st.status_code != 200:
        raise DeployError('import start failed: ' + st.text[:300])
    state, body = wait_job(lambda: (lambda b: (b.get('status', {}).get('state'), b))(
        dst.v3('GET', '/import/' + job_id, params={'expand': 'objects'}).json()), 'import', 600, 5)
    if state != 'SUCCESSFUL':
        raise DeployError('import ' + job_id + ' ended ' + state + ': ' + dst.v3('GET', '/import/' + job_id + '/log').text[:500])
    return body


def set_connection_fields(dst, conn, fields):
    body = {'@type': 'connection', 'type': conn.get('type'), 'name': conn.get('name')}
    body.update(fields)
    r = dst.v2('POST', '/connection/' + conn['id'], json=body, headers={'Update-Mode': 'PARTIAL'})
    if r.status_code != 200:
        raise DeployError('updating connection ' + conn['name'] + ' failed: ' + r.text[:300])


def test_connection(dst, conn):
    r = dst.v2('GET', '/connection/test/' + conn['id'])
    if r.status_code != 200:
        return 'test failed: ' + r.text[:120]
    body = r.json() if r.text else {}
    ok = body.get('success') if isinstance(body, dict) else None
    return 'test passed' if ok in (True, None) else 'test failed: ' + str(body.get('message', ''))[:120]


def sync_connections(src, dst, assets, cfg, dry_run, secrets, report, src_ids):
    ccfg = cfg['connections']
    if not ccfg.get('sync', True):
        log('Connection sync disabled in config')
        return
    log('\n== Connections')
    wanted = discover_connections(src, assets, cfg, src_ids)
    if not wanted:
        log('No connections used by the changed assets')
        return
    settings = ccfg.get('settings') or {}
    conn_map = cfg.get('connection_map') or {}
    missing = []
    for name, src_id in sorted(wanted.items()):
        s = src.connection_by_name(name)
        t_name = conn_map.get(name, name)
        t = dst.connection_by_name(t_name)
        row = {'name': name, 'target': t_name, 'demo_hash': definition_hash(s, VOLATILE_CONNECTION_KEYS) if s else '-'}
        if not s:
            row.update(action='not found in Demo', target_hash='-')
        elif t:
            th = definition_hash(t, VOLATILE_CONNECTION_KEYS)
            row.update(target_hash=th, action='exists, in sync' if th == row['demo_hash'] else 'exists, differs')
        else:
            row.update(target_hash='-', action='missing')
            if not src_id:
                src_id = src.lookup(name, 'CONNECTION')
            if src_id and t_name == name:
                missing.append((name, src_id))
            elif t_name != name:
                row['action'] = 'missing (mapped name ' + t_name + ' not in Dfactory)'
        report['connections'].append(row)
        log('  ' + name + ': ' + row['action'] + ' [demo ' + row['demo_hash'] + ' / dfactory ' + row['target_hash'] + ']')

    if missing:
        if dry_run:
            log('  DRY RUN: would create ' + ', '.join(n for n, _ in missing))
        else:
            export_import(src, dst, [i for _, i in missing], cfg, 'connections')
            for row in report['connections']:
                if row['action'] == 'missing':
                    row['action'] = 'created'
            log('  Created ' + ', '.join(n for n, _ in missing))

    for row in report['connections']:
        s_cfg = settings.get(row['name']) or {}
        if row['action'].startswith('not found') or dry_run:
            continue
        t = dst.connection_by_name(row['target'])
        if not t:
            continue
        fields = {}
        if row['action'] == 'exists, differs' and ccfg.get('update_existing'):
            s = src.connection_by_name(row['name'])
            fields.update({k: v for k, v in s.items() if k not in VOLATILE_CONNECTION_KEYS and k != 'connParams'})
            row['action'] = 'updated'
        fields.update(s_cfg.get('overrides') or {})
        secret_name = s_cfg.get('password_secret')
        if secret_name:
            if secrets.get(secret_name):
                fields['password'] = secrets[secret_name]
            else:
                log('  ! secret ' + secret_name + ' for ' + row['name'] + ' is not set')
        if fields:
            set_connection_fields(dst, t, fields)
            if 'password' in fields:
                row['action'] += ', password set'
        if s_cfg.get('test', True) and row['action'] != 'exists, in sync':
            row['action'] += ', ' + test_connection(dst, t)


# --------------------------------------------------------------------------------------------
# Steps 4 and 6: schedules
# --------------------------------------------------------------------------------------------

def task_schedule(org, obj_id):
    """(v2 task, schedule) for a mapping task, or (task, None)."""
    r = org.v2('GET', '/mttask/frs/' + obj_id)
    if r.status_code != 200:
        return None, None
    task = r.json()
    sid = task.get('scheduleId')
    if not sid:
        return task, None
    s = org.v2('GET', '/schedule/' + sid)
    return task, (s.json() if s.status_code == 200 else None)


def sync_schedules(src, dst, assets, cfg, dry_run, report, src_ids):
    if not cfg['schedules'].get('sync', True):
        return {}
    log('\n== Schedules')
    needed = {}
    for a in assets:
        if a['deleted'] or a['type'] != 'MTT':
            continue
        obj_id = src.find_id(src_ids, a['path'], 'MTT')
        _, sched = task_schedule(src, obj_id) if obj_id else (None, None)
        if sched:
            needed[a['path']] = sched
    if not needed:
        log('No schedules used by the changed mapping tasks')
        return {}
    done = set()
    for path, sched in needed.items():
        name = sched['name']
        if name in done:
            continue
        done.add(name)
        r = dst.v2('GET', '/schedule/name/' + urllib.parse.quote(name, safe=''))
        if r.status_code == 200 and r.json().get('id'):
            action = 'exists'
        elif dry_run:
            action = 'missing (dry run: would create)'
        else:
            body = {k: v for k, v in sched.items() if k not in VOLATILE_SCHEDULE_KEYS}
            c = dst.v2('POST', '/schedule', json=body)
            if c.status_code != 200:
                raise DeployError('creating schedule ' + name + ' failed: ' + c.text[:300])
            action = 'created'
        report['schedules'].append({'name': name, 'action': action})
        log('  ' + name + ': ' + action)
    return needed


def relink_schedules(dst, needed, dry_run, report, dst_ids):
    for path, sched in needed.items():
        obj_id = dst.find_id(dst_ids, path, 'MTT') if not dry_run else None
        if not obj_id or dry_run:
            continue
        task, current = task_schedule(dst, obj_id)
        if current or not task:
            continue
        r = dst.v2('GET', '/schedule/name/' + urllib.parse.quote(sched['name'], safe=''))
        if r.status_code != 200:
            continue
        u = dst.v2('POST', '/mttask/' + task['id'], json={'@type': 'mtTask', 'scheduleId': r.json()['id']},
                   headers={'Update-Mode': 'PARTIAL'})
        state = 'linked' if u.status_code == 200 else 'link failed: ' + u.text[:120]
        report['schedules'].append({'name': sched['name'] + ' -> ' + path, 'action': state})
        log('  ' + path + ' -> ' + sched['name'] + ': ' + state)


# --------------------------------------------------------------------------------------------
# Step 5: pull
# --------------------------------------------------------------------------------------------

def object_spec(cfg):
    spec = []
    for s, t in (cfg.get('connection_map') or {}).items():
        spec.append({'source': {'path': [s], 'type': 'Connection'}, 'target': {'path': [t], 'type': 'Connection'}})
    for s, t in (cfg.get('runtime_environment_map') or {}).items():
        spec.append({'source': {'path': [s], 'type': 'AgentGroup'}, 'target': {'path': [t], 'type': 'AgentGroup'}})
    return spec


def pull(dst, commit, cfg):
    body = {'commitHash': commit}
    spec = object_spec(cfg)
    if spec:
        body['objectSpecification'] = spec
        body['relaxObjectSpecificationValidation'] = True
    r = dst.v3('POST', '/pullByCommitHash', json=body)
    if r.status_code != 200:
        return 'FAILED', r.text[:300], []
    action_id = r.json()['pullActionId']

    def fetch():
        b = dst.v3('GET', '/sourceControlAction/' + action_id, params={'expand': 'objects'}).json()
        return b.get('status', {}).get('state'), b

    state, b = wait_job(fetch, 'pull', int(cfg.get('pull_timeout_seconds', 1800)), 10)
    objs = []
    for o in b.get('objects') or []:
        t = o.get('target') or {}
        path = t.get('path')
        objs.append({'path': '/'.join(path) if isinstance(path, list) else str(path), 'type': t.get('type'),
                     'id': t.get('id'),
                     'state': (t.get('status') or o.get('status') or {}).get('state'),
                     'message': (t.get('status') or o.get('status') or {}).get('message') or ''})
    return state, (b.get('status') or {}).get('message') or '', objs


def pull_all(dst, commit, merged, cfg, dry_run, report):
    log('\n== Pull')
    if dry_run:
        log('DRY RUN: would pull ' + commit[:7] + (' (merged commits: ' + ', '.join(c[:7] for c in merged) + ')' if merged else ''))
        return
    allow = cfg.get('allow_warnings', False)
    state, msg, objs = pull(dst, commit, cfg)
    log('Pull ' + commit[:7] + ': ' + state + ' ' + msg + ' (' + str(len(objs)) + ' objects)')
    pulls = [(commit, state, msg, objs)]
    if merged and (state == 'FAILED' or (state == 'SUCCESSFUL' and not objs)):
        log('Pulling the ' + str(len(merged)) + ' merged commit(s) individually')
        pulls = []
        for c in merged:
            s, m, o = pull(dst, c, cfg)
            log('Pull ' + c[:7] + ': ' + s + ' ' + m + ' (' + str(len(o)) + ' objects)')
            pulls.append((c, s, m, o))
    for c, s, m, objs in pulls:
        report['pulls'].append({'commit': c[:7], 'state': s, 'message': m})
        report['pulled_objects'].extend(dict(o, commit=c[:7]) for o in objs)
        for o in objs:
            log('  ' + str(o['state']) + ' ' + str(o['type']) + ' ' + o['path'])
    bad = [p for p in pulls if not (p[1] == 'SUCCESSFUL' or (p[1] == 'WARNING' and allow))]
    bad_objs = [o for o in report['pulled_objects'] if o['state'] in ('FAILED', 'CANCELLED')]
    if bad or bad_objs:
        raise DeployError('pull did not succeed: ' + '; '.join(p[0][:7] + ' ' + p[1] + ' ' + p[2] for p in bad) +
                          ('; failed objects: ' + ', '.join(o['path'] for o in bad_objs) if bad_objs else ''))


# --------------------------------------------------------------------------------------------
# Step 7: publish
# --------------------------------------------------------------------------------------------

def publish(dst, assets, cfg, dry_run, report):
    if not cfg['publish'].get('enabled', True):
        return
    paths = [a['xml'] for t in PUBLISH_ORDER for a in assets if a['type'] == t and not a['deleted'] and a['xml']]
    if not paths:
        return
    log('\n== Publish')
    for i in range(0, len(paths), 199):
        batch = paths[i:i + 199]
        if dry_run:
            log('DRY RUN: would publish ' + ', '.join(batch))
            continue
        r = dst.raw('POST', dst.host + '/active-bpel/asset/v1/publish',
                    json={'data': {'type': 'publish', 'attributes': {'assetPaths': batch}}},
                    headers={'Accept': 'application/vnd.api+json', 'Content-Type': 'application/vnd.api+json'})
        state = 'submitted' if r.status_code in (200, 201, 202) else 'failed: ' + r.text[:200]
        try:
            state += ' (' + str(r.json()['data']['attributes'].get('jobState')) + ')'
        except Exception:
            pass
        for p in batch:
            report['published'].append({'asset': p, 'state': state})
        log('Publish ' + str(len(batch)) + ' asset(s): ' + state)


# --------------------------------------------------------------------------------------------
# Step 8: tests
# --------------------------------------------------------------------------------------------

def run_tests(dst, assets, cfg, dry_run, report, dst_ids):
    tasks = [a for a in assets if a['type'] == 'MTT' and not a['deleted']]
    if not tasks:
        return
    log('\n== Tests')
    allow = cfg.get('allow_warnings', False)
    timeout = int(cfg['tests'].get('timeout_seconds', 3600))
    interval = int(cfg['tests'].get('poll_seconds', 30))
    failed = []
    for a in tasks:
        if dry_run:
            log('DRY RUN: would run ' + a['path'])
            continue
        obj_id = dst.find_id(dst_ids, a['path'], 'MTT')
        if not obj_id:
            report['tests'].append({'task': a['path'], 'result': 'not found in Dfactory'})
            failed.append(a['path'])
            continue
        r = dst.v2('POST', '/job', json={'@type': 'job', 'taskFederatedId': obj_id, 'taskType': 'MTT'})
        if r.status_code != 200:
            report['tests'].append({'task': a['path'], 'result': 'could not start: ' + r.text[:120]})
            failed.append(a['path'])
            continue
        job = r.json()
        run_id, task_id = job['runId'], job.get('taskId')
        log('Started ' + a['path'] + ' (runId ' + str(run_id) + ')')

        def fetch():
            e = dst.v2('GET', '/activity/activityLog', params={'runId': run_id, 'taskId': task_id}).json()
            if isinstance(e, list) and e and e[0].get('state') in (1, 2, 3):
                return {1: 'SUCCESSFUL', 2: 'WARNING', 3: 'FAILED'}[e[0]['state']], e[0]
            return 'RUNNING', None

        state, entry = wait_job(fetch, a['path'], timeout, interval)
        rows = ''
        if entry:
            rows = ' (' + str(entry.get('successTargetRows', '?')) + ' rows written, ' + str(entry.get('failedTargetRows', '?')) + ' failed)'
        result = {'SUCCESSFUL': 'passed', 'WARNING': 'completed with errors', 'FAILED': 'failed'}[state] + rows
        report['tests'].append({'task': a['path'], 'result': result})
        log('  ' + a['path'] + ': ' + result)
        if state == 'FAILED' or (state == 'WARNING' and not allow):
            failed.append(a['path'])
    if failed:
        raise DeployError('mapping task test failed: ' + ', '.join(failed))


# --------------------------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------------------------

def table(rows, cols):
    if not rows:
        return '_none_\n'
    out = '| ' + ' | '.join(h for h, _ in cols) + ' |\n|' + '---|' * len(cols) + '\n'
    for r in rows:
        out += '| ' + ' | '.join(str(r.get(k, '')).replace('|', '/') for _, k in cols) + ' |\n'
    return out


def write_summary(report, ok, error):
    md = '# IDMC deployment: Demo → Dfactory ' + ('✅' if ok else '❌') + '\n\n'
    md += '**Commit:** `' + report['commit'][:7] + '`'
    if report['merged']:
        md += ' (merges ' + ', '.join('`' + c[:7] + '`' for c in report['merged']) + ')'
    md += '  \n**Mode:** ' + ('dry run' if report['dry_run'] else 'deploy') + '\n\n'
    if error:
        md += '> **Failed:** ' + error + '\n\n'
    md += '## Changed assets\n' + table(report['assets'], [('Type', 'type'), ('Asset', 'path'), ('Change', 'change')])
    md += '\n## Connections\n' + table(report['connections'], [('Connection', 'name'), ('Result', 'action'),
                                                              ('Demo hash', 'demo_hash'), ('Dfactory hash', 'target_hash')])
    md += '\n## Schedules\n' + table(report['schedules'], [('Schedule', 'name'), ('Result', 'action')])
    md += '\n## Pull\n' + table(report['pulls'], [('Commit', 'commit'), ('State', 'state'), ('Message', 'message')])
    md += '\n' + table(report['pulled_objects'], [('State', 'state'), ('Type', 'type'), ('Object', 'path')])
    md += '\n## Publish\n' + table(report['published'], [('Asset', 'asset'), ('Result', 'state')])
    md += '\n## Tests\n' + table(report['tests'], [('Mapping task', 'task'), ('Result', 'result')])
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if path:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(md)


# --------------------------------------------------------------------------------------------

def main():
    with open(CONFIG_PATH, encoding='utf-8') as f:
        cfg = yaml.safe_load(f) or {}
    for k in ('connections', 'schedules', 'publish', 'tests'):
        cfg.setdefault(k, {})
    cfg.setdefault('target_branch', 'Dfactory-Branch')
    cfg['allow_warnings'] = env_flag('ALLOW_WARNINGS', cfg.get('allow_warnings', False))
    dry_run = env_flag('DRY_RUN', cfg.get('dry_run', False))
    tests_on = env_flag('RUN_TESTS', cfg['tests'].get('enabled', True))
    try:
        secrets = json.loads(os.environ.get('SECRETS_JSON') or '{}')
    except ValueError:
        secrets = {}

    report = {'commit': '', 'merged': [], 'dry_run': dry_run, 'assets': [], 'connections': [], 'schedules': [],
              'pulls': [], 'pulled_objects': [], 'published': [], 'tests': []}
    orgs, error = [], None
    try:
        commit, parents, merged = resolve_commit(cfg)
        report['commit'], report['merged'] = commit, merged
        assets = changed_assets(commit, parents)
        report['assets'] = [{'type': a['type'], 'path': a['path'], 'change': 'deleted' if a['deleted'] else 'added/modified'}
                            for a in assets]
        log('Deploying ' + commit[:7] + ' from ' + cfg['target_branch'] + (' (dry run)' if dry_run else ''))
        for a in report['assets']:
            log('  ' + a['change'] + ' ' + a['type'] + ' ' + a['path'])

        login_url = os.environ['IICS_LOGIN_URL']
        src = Org('Demo', login_url, os.environ['IICS_USERNAME'], os.environ['IICS_PASSWORD'])
        orgs.append(src)
        dst = Org('Dfactory', login_url, os.environ['UAT_IICS_USERNAME'], os.environ['UAT_IICS_PASSWORD'])
        orgs.append(dst)

        # object IDs in Demo, straight from IDMC's commit details (hash-based)
        src_ids = src.commit_ids(merged or [commit]) if any(not a['deleted'] for a in assets) else {}
        sync_connections(src, dst, assets, cfg, dry_run, secrets, report, src_ids)
        needed = sync_schedules(src, dst, assets, cfg, dry_run, report, src_ids)
        pull_all(dst, commit, merged, cfg, dry_run, report)
        # object IDs in Dfactory after the pull
        dst_ids = {}
        for o in report['pulled_objects']:
            if o.get('id') and o.get('type'):
                add_id(dst_ids, o['path'], o['type'], o['id'])
        relink_schedules(dst, needed, dry_run, report, dst_ids)
        publish(dst, assets, cfg, dry_run, report)
        if tests_on:
            run_tests(dst, assets, cfg, dry_run, report, dst_ids)
        log('\nDeployment ' + ('dry run ' if dry_run else '') + 'completed')
    except (DeployError, subprocess.CalledProcessError, KeyError, requests.RequestException) as e:
        error = str(e)
        log('\nDeployment failed: ' + error)
    finally:
        write_summary(report, error is None, error)
        for o in orgs:
            o.logout()
    sys.exit(0 if error is None else 1)


if __name__ == '__main__':
    main()
