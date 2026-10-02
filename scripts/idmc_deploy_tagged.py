"""
IDMC: deploy the assets tagged ready-to-deploy in the Demo org to the Dfactory org, then re-tag them.

Instead of deploying every check-in, this deploys only the assets a reviewer has tagged (default tag
ready-to-deploy) and that are safe to deploy:
  - under source control and not checked out (so the version in DemoCentral is the checked-in one)
  - their last check-in is merged into Dfactory-Branch (the branch the Dfactory org pulls from)
  - not tagged hold (only when a hold tag is set in config), and (if RELEASE_TAG is given) also carrying
    that release tag

Steps: find the tagged assets -> check each one -> connections, schedules and missing dependencies (as
idmc_cicd.py does) -> one pull of exactly those assets from Dfactory-Branch -> re-tag them in DemoCentral
(ready-to-deploy / reviewed / in-review -> deployed; a failed asset keeps ready-to-deploy, or gets the failed
tag when one is set) -> re-link schedules, publish, test -> summary.

Tag names live under tags: in cicd/config.yml. Environment: IICS_LOGIN_URL, IICS_USERNAME / IICS_PASSWORD
(Demo), UAT_IICS_USERNAME / UAT_IICS_PASSWORD (Dfactory), RELEASE_TAG, DRY_RUN, RUN_TESTS, SECRETS_JSON.
"""

import json
import os
import subprocess
import sys

import requests
import yaml

import idmc_cicd as core
from idmc_cicd import DeployError, Org, add_id, env_flag, log, norm_type, ref_path_type

# the tags that exist in the IDMC orgs; failed and hold are optional (blank = not used)
DEFAULT_TAGS = {'in_review': 'in-review', 'reviewed': 'reviewed', 'ready': 'ready-to-deploy',
                'deployed': 'deployed', 'failed': '', 'hold': ''}
NOT_DEPLOYABLE = {'PROJECT', 'FOLDER'}


def load_config():
    with open(core.CONFIG_PATH, encoding='utf-8') as f:
        cfg = yaml.safe_load(f) or {}
    for k in ('connections', 'schedules', 'publish', 'tests'):
        cfg.setdefault(k, {})
    cfg.setdefault('target_branch', 'Dfactory-Branch')
    cfg['allow_warnings'] = env_flag('ALLOW_WARNINGS', cfg.get('allow_warnings', False))
    tags = dict(DEFAULT_TAGS)
    tags.update({k: str(v or '').strip() for k, v in (cfg.get('tags') or {}).items()})
    for k in ('ready', 'deployed'):
        if not tags.get(k):
            raise DeployError('cicd/config.yml tags: ' + k + ' must be set')
    cfg['tags'] = tags
    return cfg


def target_head(cfg):
    """Latest commit of the target branch (fetched fresh) and the ref it was read from."""
    ref = 'origin/' + cfg['target_branch']
    try:
        core.git('fetch', '--quiet', 'origin', cfg['target_branch'])
    except subprocess.CalledProcessError:
        ref = cfg['target_branch']
    return core.git('rev-parse', ref), ref


def repo_files(commit):
    """{(asset path, TYPE): [files]} for every asset in Git at this commit."""
    out = {}
    listing = core.git('-c', 'core.quotePath=false', 'ls-tree', '-r', '--name-only', commit, '--', 'Explore')
    for f in listing.splitlines():
        parsed = core.parse_asset_file(f)
        if parsed:
            out.setdefault(parsed, []).append(f)
    return out


def merged_into(sha, ref):
    """True if commit sha is on ref (merged), False if not merged or unknown to the repo."""
    if subprocess.run(['git', 'cat-file', '-e', sha + '^{commit}'], cwd=core.ROOT, capture_output=True).returncode:
        return False
    return subprocess.run(['git', 'merge-base', '--is-ancestor', sha, ref], cwd=core.ROOT,
                          capture_output=True).returncode == 0


def location_objects(org, folder, cache):
    """{(path, TYPE): object} of everything in an Explore folder or project, read once per run."""
    if folder not in cache:
        found, skip = {}, 0
        while True:
            r = org.v3('GET', '/objects', params={'q': "location=='" + folder + "'", 'limit': 200, 'skip': skip})
            if r.status_code != 200:
                log('  ! could not list ' + folder + ' in ' + org.label + ': ' + r.text[:200])
                break
            objs = r.json().get('objects') or []
            for o in objs:
                found[(clean_path(o.get('path')), norm_type(o.get('type')))] = o
                if o.get('id'):
                    found['id:' + o['id']] = o
            if len(objs) < 200:
                break
            skip += 200
        cache[folder] = found
    return cache[folder]


def checkin_type(t):
    """Types that live in Git. Application Integration assets (App Connections AI_CONNECTION, service connectors,
    process objects ...) are Explore assets and are checked in like any other. Platform connections, runtime
    environments (SAAS_RUNTIME_ENVIRONMENT ...), agents and schedules are never checked in, so they are not required."""
    if not t:
        return False
    if t.startswith('AI_'):
        return True
    return core.is_dependency_type(t) and not any(w in t for w in ('RUNTIME', 'AGENT', 'SCHEDULE', 'CONNECTION'))


def check_dependencies(src, assets, files, ref, cfg):
    """Every asset a ready asset uses (followed down: taskflow -> mapping task -> mapping -> mapplet ...) must be
    checked in too: under source control, not checked out, on the target branch and with its last check-in merged.
    Connections, schedules and runtime environments are not checked in, so they are not part of this.
    Returns (assets whose dependencies are all checked in, blocked rows with the reason)."""
    if not (cfg.get('dependencies') or {}).get('require_checked_in', True):
        return assets, []
    max_depth = int((cfg.get('dependencies') or {}).get('max_depth', 5))
    branch = ref.split('/')[-1]
    folders, refs_cache, problems, to_checkin, demo_obj = {}, {}, {}, {}, {}
    # Git file names and the "uses" list name some types differently (MTT / MCT, BSERVICE / SAAS_BSERVICES), so an
    # asset counts as in Git when Git has that path, whatever the type is called there
    git_paths = {p for p, _ in files}

    def in_git(key):
        return key in files or key[0] in git_paths

    def problem(key, obj_id=None):
        if key not in problems:
            path, t = key
            listing = location_objects(src, path.rsplit('/', 1)[0] if '/' in path else path, folders)
            o = listing.get(key) or (listing.get('id:' + obj_id) if obj_id else None)
            demo_obj[key] = o
            sc = (o or {}).get('sourceControl') or {}
            commit = sc.get('hash') or ''
            if sc.get('checkedOutBy'):
                why = 'checked out by ' + str(sc['checkedOutBy'])
            elif in_git(key):
                why = ('last check-in ' + commit[:7] + ' not merged into ' + branch) \
                    if commit and not merged_into(commit, ref) else None
            elif o is None and not obj_id:
                why = 'not found in DemoCentral'
            elif sc.get('sourceControlled'):
                why = 'checked in but not on ' + branch + ' yet'
            else:
                why = 'never checked in'
                if (o or {}).get('id') or obj_id:
                    to_checkin[key] = (o or {}).get('id') or obj_id
            problems[key] = why
        return problems[key]

    def uses(obj_id):
        if obj_id not in refs_cache:
            refs_cache[obj_id] = src.references(obj_id)
        return refs_cache[obj_id]

    ok, blocked = [], []
    for a in assets:
        tree, seen, queue = {}, set(), [(a['id'], 0)]
        while queue:
            obj_id, depth = queue.pop(0)
            if not obj_id or obj_id in seen or depth >= max_depth:
                continue
            seen.add(obj_id)
            for r in uses(obj_id):
                key = ref_path_type(r)
                if not key[0] or not checkin_type(key[1]) or key in tree or key == (a['path'], a['type']):
                    continue
                tree[key] = r.get('id')
                queue.append((r.get('id'), depth + 1))
        bad = [(k, problem(k, tree[k])) for k in sorted(tree) if problem(k, tree[k])]
        if bad:
            names = '; '.join(k[1] + ' ' + k[0] + ' (' + why + ')' for k, why in bad[:5]) + (' ...' if len(bad) > 5 else '')
            blocked.append(dict(a, reason='uses assets that are not checked in: ' + names +
                                '. Check them in and run again',
                                needs_sync=all(why.startswith(('checked in but', 'last check-in')) for _, why in bad),
                                checkin_deps={k: to_checkin[k] for k, _ in bad if k in to_checkin}))
            log('  blocked ' + a['type'] + ' ' + a['path'] + ': ' + str(len(bad)) + ' of its ' + str(len(tree)) +
                ' dependencies not checked in')
        else:
            # remembered so the dependencies get the deployed tag together with the asset
            a['deps'] = {k: {'id': (demo_obj.get(k) or {}).get('id') or tree[k],
                             'tags': (demo_obj.get(k) or {}).get('tags')} for k in tree}
            ok.append(a)
            if tree:
                log('  ' + a['type'] + ' ' + a['path'] + ': all ' + str(len(tree)) + ' dependencies checked in')
    return ok, blocked


CHECKIN_SUMMARY = 'Dependencies of '


def last_source_commit(cfg):
    """Subject of the latest commit on the source branch (where DemoCentral checks in)."""
    source = cfg.get('source_branch', 'Demo-Central-Branch')
    try:
        core.git('fetch', '--quiet', 'origin', source)
        return core.git('log', '-1', '--format=%s', 'origin/' + source)
    except subprocess.CalledProcessError:
        return ''


def checkin_dependencies(src, blocked, cfg):
    """Check in, from DemoCentral, the never-checked-in assets that ready assets use (POST /checkin), so the next
    run finds the whole set in Git. Objects checked out by someone are left alone. The check-in reaches
    Dfactory-Branch through the Auto PR, which starts this workflow again. Returns {(path, TYPE): result}."""
    wanted = {}
    for b in blocked:
        for k, obj_id in (b.get('checkin_deps') or {}).items():
            wanted.setdefault(k, (obj_id, b['path']))
    if not wanted:
        return {}
    log('\n== Check in dependencies in DemoCentral')
    users = sorted({w[1] for w in wanted.values()})
    body = {'objects': [{'id': obj_id} for obj_id, _ in wanted.values()],
            'summary': CHECKIN_SUMMARY + ', '.join(users)[:200],
            'description': 'Checked in by IDMC - Deploy tagged assets: used by ' + ', '.join(users) +
                           ', which is tagged ' + cfg['tags']['ready'] + '.'}
    results = {}
    r = src.v3('POST', '/checkin', json=body)
    if r.status_code not in (200, 201, 202):
        msg = 'check-in failed: ' + r.text[:300]
        log(msg)
        return {k: msg for k in wanted}
    reply = r.json() if r.text.strip() else {}
    action = next((v for k, v in reply.items() if k.lower().endswith('actionid')), None) or reply.get('id')
    if not action:
        log('check-in started; IDMC returned no action id to follow: ' + r.text[:200])
        return {k: 'check-in started' for k in wanted}

    def fetch():
        b = src.v3('GET', '/sourceControlAction/' + action, params={'expand': 'objects'}).json()
        return (b.get('status') or {}).get('state'), b

    try:
        state, b = core.wait_job(fetch, 'check-in', int(cfg.get('pull_timeout_seconds', 1800)), 5)
    except DeployError as e:
        return {k: str(e) for k in wanted}
    per = {}
    for o in b.get('objects') or []:
        t = o.get('target') or o.get('source') or o
        key = (clean_path(t.get('path')), norm_type(t.get('type')))
        per[key] = core.object_message(o) or str((t.get('status') or o.get('status') or {}).get('state') or state)
    commit = b.get('commitHash') or (b.get('status') or {}).get('commitHash') or ''
    msg = (b.get('status') or {}).get('message') or ''
    for k in wanted:
        results[k] = ('checked in' + (' as ' + commit[:7] if commit else '')) if state == 'SUCCESSFUL' \
            else 'check-in ' + state.lower() + ': ' + (per.get(k) or msg)[:200]
        log('  ' + k[1] + ' ' + k[0] + ': ' + results[k])
    return results


def show_folder(src, folder):
    """Read-only: list what IDMC returns for every asset in a DemoCentral folder (id, type, tags, source control
    state) and what each one uses. For checking why an asset is or is not treated as checked in or tagged."""
    log('\n== Assets in ' + folder + ' (DemoCentral, as the API returns them)')
    rows = []
    for k, o in sorted(location_objects(src, folder, {}).items(), key=lambda x: str(x[0])):
        if isinstance(k, str):
            continue
        uses = []
        for r in src.references(o.get('id')) if o.get('id') else []:
            p, t = ref_path_type(r)
            uses.append(t + ' ' + p + ' [' + str(r.get('id')) + ']')
        row = {'asset': k[0], 'type': k[1], 'id': o.get('id'), 'tags': ', '.join(o.get('tags') or []) or '-',
               'source': json.dumps(o.get('sourceControl'), default=str) if o.get('sourceControl') is not None
               else 'not returned', 'uses': '; '.join(uses) or '-'}
        rows.append(row)
        log('  ' + row['type'] + ' ' + row['asset'] + '\n      id=' + str(row['id']) + '  tags=' + row['tags'] +
            '\n      sourceControl=' + row['source'] + '\n      uses: ' + row['uses'])
        log('      all fields: ' + ', '.join(sorted(o.keys())))
    probe_source_control(src, folder, [o for k, o in location_objects(src, folder, {}).items() if not isinstance(k, str)])
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if path:
        with open(path, 'a', encoding='utf-8') as f:
            f.write('# Assets in ' + folder + '\n\n' + core.table(rows, [
                ('Asset', 'asset'), ('Type', 'type'), ('Id', 'id'), ('Tags', 'tags'), ('Source control', 'source'),
                ('Uses', 'uses')]))


def probe_source_control(src, folder, objs):
    """Read-only: which IDMC calls return the check-in state of these objects (for show_folder)."""
    log('\n== Where IDMC returns the check-in state')
    ids = {o.get('id'): o for o in objs if o.get('id')}

    def query(label, q, limit=200):
        r = src.v3('GET', '/objects', params={'q': q, 'limit': limit})
        if r.status_code != 200:
            log('  ' + label + ': HTTP ' + str(r.status_code) + ' ' + r.text[:200])
            return
        got = [o for o in (r.json().get('objects') or []) if o.get('id') in ids]
        log('  ' + label + ' (q=' + q + '): ' + str(len(got)) + ' of these objects returned')
        for o in got[:20]:
            log('      ' + str(o.get('type')) + ' ' + clean_path(o.get('path')) + ': sourceControl=' +
                json.dumps(o.get('sourceControl'), default=str) + '  fields=' + ','.join(sorted(o.keys())))

    for t in sorted({str(o.get('type')) for o in objs}):
        query('folder + type ' + t, "location=='" + folder + "' and type=='" + t + "'")
    for tag in sorted({tg for o in objs for tg in (o.get('tags') or [])}):
        query('tag ' + tag, "tag=='" + tag + "'")
    for t in ('DTEMPLATE', 'MTT', 'BSERVICE', 'TASKFLOW'):
        query('type ' + t, "type=='" + t + "'")
    for o in objs:
        if str(o.get('type')) not in ('DTEMPLATE', 'MTT', 'BSERVICE', 'TASKFLOW'):
            continue
        p = clean_path(o.get('path'))
        for label, path_ in (('commitHistory', '/commitHistory'), ('object by id', '/objects/' + str(o.get('id')))):
            params = {'q': "path=='" + p + "' and type=='" + str(o.get('type')) + "'", 'limit': 2} \
                if path_ == '/commitHistory' else None
            r = src.v3('GET', path_, params=params)
            log('  ' + label + ' ' + str(o.get('type')) + ' ' + p + ': HTTP ' + str(r.status_code) + ' ' +
                r.text[:400].replace('\n', ' '))


def sync_branches(cfg, dry_run):
    """Ask for the check-ins on the source branch (where DemoCentral checks in) to be merged into the target
    branch (what DFactory pulls from): open a pull request, or reuse the open one, and turn on auto-merge so
    GitHub merges it as soon as it is approved. Called only when an asset tagged ready-to-deploy is not on the
    target branch yet. Nothing is merged without review; the assets go on the first run after the merge.
    Needs GH_TOKEN and GH_REPO. Returns (merged now, pull request number or '')."""
    source, target = cfg.get('source_branch', 'Demo-Central-Branch'), cfg['target_branch']
    log('\n== Git: check-ins from ' + source + ' into ' + target)

    def pending():
        core.git('fetch', '--quiet', 'origin', source, target)
        return int(core.git('rev-list', '--count', 'origin/' + target + '..origin/' + source) or 0)

    try:
        count = pending()
    except subprocess.CalledProcessError as e:
        log('Could not compare the branches: ' + (e.stderr or str(e)).strip()[:300])
        return False, ''
    if not count:
        log('Nothing to merge: every check-in on ' + source + ' is already on ' + target)
        return False, ''
    if dry_run:
        log('DRY RUN: would open a pull request for ' + str(count) + ' commit(s) from ' + source + ' into ' + target)
        return False, ''
    if not os.environ.get('GH_TOKEN'):
        log('GH_TOKEN is not set, so no pull request can be opened')
        return False, ''

    def gh(*args):
        return subprocess.run(['gh'] + list(args), cwd=core.ROOT, check=True, capture_output=True, text=True).stdout.strip()

    try:
        number = gh('pr', 'list', '--base', target, '--head', source, '--state', 'open', '--json', 'number',
                    '--jq', '.[0].number')
        if number:
            log('Pull request #' + number + ' is already open')
        else:
            url = gh('pr', 'create', '--base', target, '--head', source,
                     '--title', 'Move IICS assets from ' + source + ' to ' + target,
                     '--body', 'Opened by IDMC - Deploy tagged assets because assets tagged ' + cfg['tags']['ready'] +
                     ' are checked in but not yet on ' + target + '. Approve it to let them deploy.')
            number = url.rstrip('/').split('/')[-1]
            log('Opened pull request #' + number + ' (' + str(count) + ' commit(s))')
    except (subprocess.CalledProcessError, OSError) as e:
        log('Opening the pull request failed: ' + (getattr(e, 'stderr', '') or str(e)).strip()[:300])
        return False, ''
    try:
        gh('pr', 'merge', number, '--auto', '--merge')
    except (subprocess.CalledProcessError, OSError) as e:
        log('Auto-merge could not be turned on (' + (getattr(e, 'stderr', '') or str(e)).strip()[:200] +
            '); merge the pull request after approving it')
    try:
        if not pending():
            log('Pull request #' + number + ' is merged')
            return True, number
    except subprocess.CalledProcessError:
        pass
    log('Waiting for pull request #' + number + ' to be approved; the assets deploy on the first run after it merges')
    return False, number


def find_tagged(org, tag):
    """Every asset in org carrying tag (the objects API returns at most 200 per call)."""
    out, skip = [], 0
    while True:
        r = org.v3('GET', '/objects', params={'q': "tag=='" + tag + "'", 'limit': 200, 'skip': skip})
        if r.status_code != 200:
            # IDMC answers 'Unable to find tag' when no asset in the org carries the tag yet
            if 'unable to find tag' in r.text.lower():
                return out
            raise DeployError('finding assets tagged ' + tag + ' failed: ' + r.text[:300])
        objs = r.json().get('objects') or []
        out.extend(objs)
        if len(objs) < 200:
            return out
        skip += 200


def clean_path(path):
    parts = list(path) if isinstance(path, list) else str(path or '').strip('/').split('/')
    if parts and parts[0] == 'Explore':
        parts = parts[1:]
    return '/'.join(p for p in parts if p)


def select(objs, tags, release, files, ref):
    """Split the tagged assets into (deployable assets, skipped rows with the reason)."""
    ready, skipped = [], []
    for o in objs:
        path, t = clean_path(o.get('path')), norm_type(o.get('type'))
        otags = o.get('tags') or []
        sc = o.get('sourceControl') or {}
        commit = sc.get('hash') or ''
        reason = None
        if t in NOT_DEPLOYABLE:
            reason = 'projects and folders are not deployed by tag; tag the assets inside'
        elif tags['hold'] and tags['hold'] in otags:
            reason = 'on hold (' + tags['hold'] + ')'
        elif release and release not in otags:
            reason = 'not in release ' + release
        elif sc.get('sourceControlled') is False:
            reason = 'never checked in'
        elif sc.get('checkedOutBy'):
            reason = 'checked out by ' + str(sc['checkedOutBy']) + '; check it in first'
        elif (path, t) not in files:
            reason = 'not found in Git on ' + ref.split('/')[-1]
        elif commit and not merged_into(commit, ref):
            reason = 'last check-in ' + commit[:7] + ' is not merged into ' + ref.split('/')[-1] + ' yet'
        row = {'path': path, 'type': t, 'id': o.get('id'), 'tags': otags, 'checkin': commit[:7] or '-',
               'checkin_by': sc.get('lastCheckinBy') or '-'}
        if reason:
            skipped.append(dict(row, reason=reason, needs_sync=reason.startswith(('not found in Git', 'last check-in'))))
            continue
        xml = next((f for f in files[(path, t)] if f.endswith('.xml') and not f.split('/')[-1].startswith('.')), None)
        ready.append(dict(row, deleted=False, xml=xml))
    return ready, skipped


def pull_assets(src, dst, assets, cfg, commit, repo, deps, report, files=None, with_deps=None):
    """One pull of these assets at commit, together with with_deps [(path, TYPE)] (their dependencies, pulled from
    Git so they are checked in in Dfactory too); otherwise fetch missing dependencies and retry if IDMC reports any.
    Returns {(path, TYPE): (ok, message)}."""
    log('\n== Pull')
    allow = cfg.get('allow_warnings', False)
    tried = set()
    state, msg, objs = 'FAILED', '', []
    extra = list(with_deps or [])
    for attempt in range(4):
        body = {'commitHash': commit,
                'objects': [{'path': a['path'].split('/'), 'type': a['type']} for a in assets] +
                           [{'path': k[0].split('/'), 'type': k[1]} for k in extra]}
        state, msg, objs = core.run_pull(dst, '/pull', body, cfg)
        log('Pull of ' + str(len(assets)) + ' asset(s)' + (' and ' + str(len(extra)) + ' dependencies' if extra else '') +
            ' at ' + commit[:7] + ': ' + state + ' ' + msg)
        if with_deps is not None:
            break       # strict: everything is in Git and in this one pull; nothing is imported
        bad = state not in ('SUCCESSFUL', 'WARNING') or (state == 'WARNING' and not allow) or \
            any(str(o['state']).upper() in ('FAILED', 'CANCELLED') for o in objs)
        if not bad or attempt == 3:
            break
        missing = core.missing_from_messages([msg] + [o['message'] for o in objs])
        new = {k: deps.get(k) for k in missing if k not in tried}
        if not new or not (cfg.get('dependencies') or {}).get('sync', True):
            break
        tried.update(new)
        log('IDMC reports missing dependencies: ' + ', '.join(k[1] + ' ' + k[0] for k in sorted(new)))
        core.bring_objects(src, dst, new, cfg, False, report, commit, repo)
        log('Retrying the pull')
    report['pulls'].append({'commit': commit[:7], 'state': state, 'message': msg})
    for o in objs:
        log('  ' + str(o['state']) + ' ' + str(o['type']) + ' ' + o['path'] + (': ' + o['message'] if o['message'] else ''))
        if o.get('raw'):
            log('      IDMC response for this object: ' + json.dumps(o.pop('raw'), default=str)[:1500])
    objs = commit_fallback(dst, assets, cfg, commit, files or {}, objs, allow, report)
    report['pulled_objects'].extend(objs)
    by_key = {}
    for o in objs:
        by_key[(o['path'], norm_type(o['type']))] = o
    if with_deps:
        for k in extra:
            o = by_key.get(k)
            st = str((o or {}).get('state') or state)
            report['dependencies'].append({'object': k[0], 'type': k[1], 'result': (
                'pulled from Git with the asset' if st.upper() == 'SUCCESSFUL' or (st.upper() == 'WARNING' and allow)
                else 'pull from Git ' + st.lower() + ': ' + str((o or {}).get('message') or msg)[:200])})
    whole_ok = state == 'SUCCESSFUL' or (state == 'WARNING' and allow)
    out = {}
    # an asset retried by commit counts by its own result, not the first pull's overall state
    for a in assets:
        o = by_key.get((a['path'], a['type']))
        if o is not None:
            ok = str(o['state']).upper() == 'SUCCESSFUL' or (str(o['state']).upper() == 'WARNING' and allow)
            out[(a['path'], a['type'])] = (ok, o['message'] or str(o['state']))
        else:
            out[(a['path'], a['type'])] = (whole_ok, msg or state)
    return out


def object_failed(o, allow):
    st = str(o.get('state')).upper()
    return not (st == 'SUCCESSFUL' or (st == 'WARNING' and allow))


def commit_fallback(dst, assets, cfg, head, files, objs, allow, report):
    """IDMC sometimes fails an object in a pull of named objects ("Internal error", seen with an MDM reference
    entity) that it pulls fine as part of a whole commit. For each ready asset that failed, find the
    Dfactory-Branch commit that last changed it; if everything that commit changed is ready to deploy, pull
    that commit instead. Returns objs with the retried assets' entries replaced by the new results."""
    ready = {(a['path'], a['type']) for a in assets}
    failed = {(o['path'], norm_type(o['type'])) for o in objs if object_failed(o, allow)} & ready
    if not failed:
        return objs
    by_commit = {}
    for key in sorted(failed):
        paths = files.get(key) or []
        # first-parent: the Dfactory-Branch commit (usually the Auto PR merge) that brought the asset in
        sha = core.git('log', '-1', '--first-parent', '--format=%H', head, '--', *paths) if paths else ''
        if sha:
            by_commit.setdefault(sha, []).append(key)
    for sha, keys in by_commit.items():
        parents = core.git('log', '-1', '--format=%P', sha).split()
        changed = {(a['path'], a['type']) for a in core.changed_assets(sha, parents)}
        others = sorted(changed - ready)
        names = ', '.join(k[0] for k in keys)
        if others:
            log('Not retrying ' + names + ' by commit ' + sha[:7] + ': that commit also changes assets not tagged ready (' +
                ', '.join(k[0] for k in others[:5]) + (' ...' if len(others) > 5 else '') + ')')
            continue
        log('Retrying ' + names + ' by pulling commit ' + sha[:7] + ' (it changes only assets tagged ready)')
        state, msg, cobjs = core.run_pull(dst, '/pullByCommitHash', {'commitHash': sha}, cfg)
        log('Pull of commit ' + sha[:7] + ': ' + state + ' ' + msg)
        report['pulls'].append({'commit': sha[:7], 'state': state, 'message': msg})
        for o in cobjs:
            log('  ' + str(o['state']) + ' ' + str(o['type']) + ' ' + o['path'] + (': ' + o['message'] if o['message'] else ''))
            if o.get('raw'):
                log('      IDMC response for this object: ' + json.dumps(o.pop('raw'), default=str)[:1500])
        got = {(o['path'], norm_type(o['type'])): o for o in cobjs}
        ok_all = state == 'SUCCESSFUL' or (state == 'WARNING' and allow)
        for key in keys:
            new = got.get(key) or {'path': key[0], 'type': key[1], 'id': None, 'message': msg or state,
                                   'state': 'SUCCESSFUL' if ok_all else 'FAILED'}
            objs = [x for x in objs if (x['path'], norm_type(x['type'])) != key] + [new]
        objs += [o for k, o in got.items() if k not in keys]
    return objs


def dependency_assets(with_deps, files, report, dry=False):
    """The dependencies pulled with the assets, as publishable entries (App Connections, service connectors and
    processes must be published in Dfactory before a guide or taskflow that uses them can run)."""
    pulled = {(r['object'], r['type']) for r in report['dependencies']
              if str(r.get('result', '')).startswith('pulled from Git' if not dry else 'would pull')}
    out = []
    for k in with_deps or []:
        if k in pulled:
            xml = next((f for f in files.get(k, []) if f.endswith('.xml') and not f.split('/')[-1].startswith('.')), None)
            out.append({'path': k[0], 'type': k[1], 'deleted': False, 'xml': xml})
    return out


def change_tags(org, endpoint, changes):
    """POST /TagObjects or /UntagObjects for [{'id', 'tags'}], 100 per call. Returns {id: error} for failures."""
    errors = {}
    changes = [c for c in changes if c['tags']]
    for i in range(0, len(changes), 100):
        batch = changes[i:i + 100]
        r = org.v3('POST', '/' + endpoint, json=batch)
        if r.status_code == 204 or (r.status_code == 200 and not r.text.strip()):
            continue
        body = None
        try:
            body = r.json()
        except ValueError:
            pass
        if isinstance(body, list):
            for item in body:
                if str(item.get('status', '')).upper() not in ('SUCCESS', 'SUCCESSFUL'):
                    errors[item.get('id')] = str(item.get('msg') or item.get('status'))[:200]
        elif r.status_code >= 300:
            for c in batch:
                errors[c['id']] = endpoint + ' failed: ' + r.text[:200]
    return errors


def retag(src, assets, outcome, tags):
    """In DemoCentral: deployed assets get the deployed tag (ready / reviewed / in-review / failed removed), and so
    do the assets they use (mapping task, mapping, business service ...; connections and runtime environments have
    no tags here). A failed asset gets the failed tag in place of ready when a failed tag is set; otherwise its
    tags are left as they are, so it stays ready and the next run tries it again.
    Returns ({(path, TYPE): tag change text}, {id: error})."""
    untag, tag, text = [], [], {}
    own_ids = {a['id'] for a in assets}
    dep_done, dep_count = set(), {}
    for a in assets:
        ok, _ = outcome[(a['path'], a['type'])]
        if not ok:
            continue
        n = 0
        for k, d in sorted((a.get('deps') or {}).items()):
            if not d.get('id') or d['id'] in own_ids:
                continue
            n += 1
            if d['id'] in dep_done:
                continue
            dep_done.add(d['id'])
            have = d.get('tags')
            if have is not None:
                drop = [tags[x] for x in ('ready', 'reviewed', 'in_review', 'failed') if tags.get(x) and tags[x] in have]
                if drop:
                    untag.append({'id': d['id'], 'tags': drop})
            if have is None or tags['deployed'] not in have:
                tag.append({'id': d['id'], 'tags': [tags['deployed']]})
        dep_count[a['id']] = n
    for a in assets:
        ok, _ = outcome[(a['path'], a['type'])]
        if not ok and not tags['failed']:
            text[(a['path'], a['type'])] = 'unchanged (still ' + tags['ready'] + ')'
            continue
        if ok:
            drop = [tags[k] for k in ('ready', 'reviewed', 'in_review', 'failed') if tags.get(k)]
        else:
            drop = [tags['ready']]
        drop = [t for t in drop if t in a['tags']]
        add = tags['deployed'] if ok else tags['failed']
        untag.append({'id': a['id'], 'tags': drop})
        tag.append({'id': a['id'], 'tags': [add] if add not in a['tags'] else []})
        text[(a['path'], a['type'])] = (', '.join(drop) or '-') + ' -> ' + add
        if dep_count.get(a['id']):
            text[(a['path'], a['type'])] += ' (and its ' + str(dep_count[a['id']]) + ' dependencies -> ' + add + ')'
    errors = change_tags(src, 'UntagObjects', untag)
    errors.update(change_tags(src, 'TagObjects', tag))
    for a in assets:
        if a['id'] in errors:
            text[(a['path'], a['type'])] += ' (tagging failed: ' + errors[a['id']] + ')'
        dep_errors = [k[0].split('/')[-1] + ': ' + errors[d['id']] for k, d in (a.get('deps') or {}).items()
                      if d.get('id') in errors]
        if dep_errors:
            text[(a['path'], a['type'])] += ' (tagging dependencies failed: ' + '; '.join(dep_errors)[:300] + ')'
    return text, errors


def write_summary(report, dry_run, error):
    md = '# IDMC tagged deployment: Demo → Dfactory ' + ('❌' if error else '✅') + '\n\n'
    md += '**Mode:** ' + ('dry run' if dry_run else 'deploy') + '  \n**Tag:** `' + report['ready_tag'] + '`'
    if report.get('release'):
        md += ' + `' + report['release'] + '`'
    if report.get('commit'):
        md += '  \n**Deployed from:** `' + report['commit'][:7] + '` on ' + report['branch']
    if report.get('orgs'):
        md += '  \n**Orgs:** ' + report['orgs']
    md += '\n\n'
    if error:
        md += '> **Failed:** ' + error + '\n\n'
    md += '## Assets\n' + core.table(report['assets'], [('Asset', 'path'), ('Type', 'type'), ('Last check-in', 'checkin'),
                                                        ('Result', 'result'), ('Tags in DemoCentral', 'tag_change')])
    md += '\n## Skipped\n' + core.table(report['skipped'], [('Asset', 'path'), ('Type', 'type'), ('Reason', 'reason')])
    md += '\n## Connections\n' + core.table(report['connections'], [('Connection', 'name'), ('Result', 'action'),
                                                                     ('Demo hash', 'demo_hash'), ('Dfactory hash', 'target_hash')])
    md += '\n## Dependencies\n' + core.table(report['dependencies'], [('Object', 'object'), ('Type', 'type'), ('Result', 'result')])
    md += '\n## Schedules\n' + core.table(report['schedules'], [('Schedule', 'name'), ('Result', 'action')])
    md += '\n## Pull\n' + core.table(report['pulls'], [('Commit', 'commit'), ('State', 'state'), ('Message', 'message')])
    md += '\n' + core.table(report['pulled_objects'], [('State', 'state'), ('Type', 'type'), ('Object', 'path'), ('Message', 'message')])
    md += '\n## Publish\n' + core.table(report['published'], [('Asset', 'asset'), ('Result', 'state')])
    md += '\n## Tests\n' + core.table(report['tests'], [('Mapping task', 'task'), ('Result', 'result')])
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if path:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(md)


def main():
    cfg = load_config()
    tags = cfg['tags']
    dry_run = env_flag('DRY_RUN', cfg.get('dry_run', False))
    tests_on = env_flag('RUN_TESTS', cfg['tests'].get('enabled', True))
    release = os.environ.get('RELEASE_TAG', '').strip()
    try:
        secrets = json.loads(os.environ.get('SECRETS_JSON') or '{}')
    except ValueError:
        secrets = {}
    report = {'ready_tag': tags['ready'], 'release': release, 'assets': [], 'skipped': [], 'connections': [],
              'dependencies': [], 'schedules': [], 'pulls': [], 'pulled_objects': [], 'published': [], 'tests': []}
    logins, error = [], None
    try:
        commit, ref = target_head(cfg)
        report.update(commit=commit, branch=cfg['target_branch'])
        files = repo_files(commit)

        login_url = os.environ['IICS_LOGIN_URL']
        src = Org('Demo', login_url, os.environ['IICS_USERNAME'], os.environ['IICS_PASSWORD'])
        logins.append(src)
        dst = Org('Dfactory', login_url, os.environ['UAT_IICS_USERNAME'], os.environ['UAT_IICS_PASSWORD'])
        logins.append(dst)
        report['orgs'] = src.org_name + ' → ' + dst.org_name
        core.check_orgs(src, dst, cfg)

        if os.environ.get('SHOW_FOLDER', '').strip():
            show_folder(src, os.environ['SHOW_FOLDER'].strip().strip('/'))
            if os.environ.get('GITHUB_OUTPUT'):
                with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as f:
                    f.write('to_deploy=0\n')
            return

        tagged = find_tagged(src, tags['ready'])

        def evaluate():
            ready, skip_rows = select(tagged, tags, release, files, ref)
            ready, blocked = check_dependencies(src, ready, files, ref, cfg)
            return ready, skip_rows + blocked

        assets, skipped = evaluate()
        # a ready asset whose check-in is not on the target branch yet: merge the check-ins first, then look again
        if any(s.get('needs_sync') for s in skipped):
            merged, pr = sync_branches(cfg, dry_run)
            if merged:
                commit, ref = target_head(cfg)
                report.update(commit=commit)
                files = repo_files(commit)
                assets, skipped = evaluate()
            elif pr:
                for s in skipped:
                    if s.get('needs_sync'):
                        s['reason'] += '; waiting for approval of pull request #' + pr
        # dependencies that were never checked in: check them in now (not in a dry run started by hand). They reach
        # Dfactory-Branch through the Auto PR, which starts this workflow again; that run deploys the whole set.
        want_checkin = env_flag('AUTO_CHECKIN', (cfg.get('dependencies') or {}).get('auto_checkin', True)) and \
            any(s.get('checkin_deps') for s in skipped)
        if want_checkin and last_source_commit(cfg).startswith(CHECKIN_SUMMARY):
            # this run was started by our own check-in: never check in again from here, so it cannot repeat itself
            log('\nThis run was started by an automatic check-in; not checking in again')
            for s in skipped:
                if s.get('checkin_deps'):
                    s['reason'] += '. Already checked in automatically once; check these in by hand'
            want_checkin = False
        if want_checkin:
            done = checkin_dependencies(src, skipped, cfg)
            for s in skipped:
                if s.get('checkin_deps'):
                    got = [done.get(k, '') for k in s['checkin_deps']]
                    if got and all(g.startswith(('checked in', 'check-in started')) for g in got):
                        s['reason'] = s['reason'].replace('. Check them in and run again', '') + (
                            '. Checked them in automatically; this asset deploys on the run that this check-in starts')
                    else:
                        s['reason'] += '. Automatic check-in: ' + '; '.join(
                            k[0].split('/')[-1] + ' ' + done.get(k, '-') for k in s['checkin_deps'])
        report['skipped'] = skipped
        log('Assets tagged ' + tags['ready'] + (' and ' + release if release else '') + ': ' + str(len(tagged)) +
            ' found, ' + str(len(assets)) + ' to deploy, ' + str(len(skipped)) + ' skipped')
        for s in skipped:
            log('  skip ' + s['type'] + ' ' + s['path'] + ': ' + s['reason'])
        for a in assets:
            log('  deploy ' + a['type'] + ' ' + a['path'] + ' (last check-in ' + a['checkin'] + ' by ' + a['checkin_by'] + ')')
        # for the workflow: the approval step is only requested when there is something to deploy
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as f:
                f.write('to_deploy=' + str(len(assets)) + '\n')
        if not assets:
            log('\nNothing to deploy')
            return

        src_ids = {}
        for a in assets:
            add_id(src_ids, a['path'], a['type'], a['id'])
        dep_on = (cfg.get('dependencies') or {}).get('sync', True)
        deps = core.discover_dependencies(src, assets, cfg, src_ids) if dep_on else {}
        repo = set(files)
        core.sync_connections(src, dst, assets, cfg, dry_run, secrets, report, src_ids, deps)
        needed = core.sync_schedules(src, dst, assets, cfg, dry_run, report, src_ids)
        strict = (cfg.get('dependencies') or {}).get('require_checked_in', True)
        with_deps = None
        if strict:
            # every dependency is checked in (check_dependencies made sure): pull them from Git together with the
            # assets, also the ones Dfactory already has, so they are checked in (linked to Git) there too
            git_key = {}
            for k in files:
                git_key.setdefault(k[0], k)
            own = {(a['path'], a['type']) for a in assets}
            with_deps = sorted({git_key.get(k[0], k) for a in assets for k in (a.get('deps') or {})} - own)
            log('\n== Dependencies\n' + (str(len(with_deps)) + ' pulled from Git together with the assets: ' +
                                         ', '.join(k[1] + ' ' + k[0] for k in with_deps) if with_deps else 'none'))
            if dry_run:
                report['dependencies'].extend({'object': k[0], 'type': k[1], 'result': 'would pull from Git with the asset'}
                                              for k in with_deps)
        else:
            core.sync_dependencies(src, dst, assets, cfg, dry_run, report, deps, commit, repo)

        if dry_run:
            log('\n== Pull\nDRY RUN: would pull ' + ', '.join(a['path'] for a in assets) + ' at ' + commit[:7] +
                ' and re-tag them ' + tags['ready'] + ' -> ' + tags['deployed'])
            for a in assets:
                drop = [tags[k] for k in ('ready', 'reviewed', 'in_review', 'failed') if tags.get(k) and tags[k] in a['tags']]
                deps_n = len([d for d in (a.get('deps') or {}).values() if d.get('id')])
                report['assets'].append(dict(a, result='would deploy', tag_change=', '.join(drop) + ' -> ' + tags['deployed'] +
                                             (' (and its ' + str(deps_n) + ' dependencies)' if deps_n else '')))
            core.publish(dst, assets + dependency_assets(with_deps, files, report, dry=True), cfg, True, report)
            if tests_on:
                core.run_tests(dst, assets, cfg, True, report, {})
            log('\nDeployment dry run completed')
            return

        outcome = pull_assets(src, dst, assets, cfg, commit, repo, deps, report, files, with_deps)
        log('\n== Tags in DemoCentral')
        tag_text, tag_errors = retag(src, assets, outcome, tags)
        for a in assets:
            ok, msg = outcome[(a['path'], a['type'])]
            report['assets'].append(dict(a, result='deployed' if ok else 'failed: ' + msg,
                                         tag_change=tag_text[(a['path'], a['type'])]))
            log('  ' + a['path'] + ': ' + tag_text[(a['path'], a['type'])])
        done = [a for a in assets if outcome[(a['path'], a['type'])][0]]
        failed = [a for a in assets if not outcome[(a['path'], a['type'])][0]]

        dst_ids = {}
        for o in report['pulled_objects']:
            if o.get('id') and o.get('type'):
                add_id(dst_ids, o['path'], o['type'], o['id'])
        core.relink_schedules(dst, needed, False, report, dst_ids)
        core.publish(dst, done + dependency_assets(with_deps, files, report), cfg, False, report)
        problems = []
        if failed:
            problems.append('not deployed: ' + ', '.join(a['path'] for a in failed))
        if tag_errors:
            problems.append('tags not updated for ' + str(len(tag_errors)) + ' asset(s)')
        if tests_on and done:
            try:
                core.run_tests(dst, done, cfg, False, report, dst_ids)
            except DeployError as e:
                problems.append(str(e))
        if problems:
            error = '; '.join(problems)
        log('\n' + ('Deployment finished with problems: ' + error if error else 'Deployment completed'))
    except (DeployError, subprocess.CalledProcessError, KeyError, requests.RequestException) as e:
        error = str(e)
        log('\nDeployment failed: ' + error)
    finally:
        write_summary(report, dry_run, error)
        for o in logins:
            o.logout()
    sys.exit(0 if error is None else 1)


if __name__ == '__main__':
    main()
