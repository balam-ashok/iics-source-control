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


def check_dependencies(src, assets, files, ref, cfg):
    """Every asset a ready asset uses (followed down: taskflow -> mapping task -> mapping -> mapplet ...) must be
    checked in too: under source control, not checked out, on the target branch and with its last check-in merged.
    Connections, schedules and runtime environments are not checked in, so they are not part of this.
    Returns (assets whose dependencies are all checked in, blocked rows with the reason)."""
    if not (cfg.get('dependencies') or {}).get('require_checked_in', True):
        return assets, []
    max_depth = int((cfg.get('dependencies') or {}).get('max_depth', 5))
    branch = ref.split('/')[-1]
    folders, refs_cache, problems = {}, {}, {}

    def problem(key, obj_id=None):
        if key not in problems:
            path, t = key
            listing = location_objects(src, path.rsplit('/', 1)[0] if '/' in path else path, folders)
            o = listing.get(key) or (listing.get('id:' + obj_id) if obj_id else None)
            sc = (o or {}).get('sourceControl') or {}
            commit = sc.get('hash') or ''
            if o is None:
                why = 'not found in DemoCentral'
            elif sc.get('sourceControlled') is False:
                why = 'never checked in'
            elif sc.get('checkedOutBy'):
                why = 'checked out by ' + str(sc['checkedOutBy'])
            elif key not in files:
                why = 'not on ' + branch
            elif commit and not merged_into(commit, ref):
                why = 'last check-in ' + commit[:7] + ' not merged into ' + branch
            else:
                why = None
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
                if not key[0] or not core.is_dependency_type(key[1]) or key in tree or key == (a['path'], a['type']):
                    continue
                tree[key] = r.get('id')
                queue.append((r.get('id'), depth + 1))
        bad = [(k, problem(k, tree[k])) for k in sorted(tree) if problem(k, tree[k])]
        if bad:
            names = '; '.join(k[1] + ' ' + k[0] + ' (' + why + ')' for k, why in bad[:5]) + (' ...' if len(bad) > 5 else '')
            blocked.append(dict(a, reason='uses assets that are not checked in: ' + names +
                                '. Check them in and run again',
                                needs_sync=all(why.startswith(('not on', 'last check-in')) for _, why in bad)))
            log('  blocked ' + a['type'] + ' ' + a['path'] + ': ' + str(len(bad)) + ' of its ' + str(len(tree)) +
                ' dependencies not checked in')
        else:
            ok.append(a)
            if tree:
                log('  ' + a['type'] + ' ' + a['path'] + ': all ' + str(len(tree)) + ' dependencies checked in')
    return ok, blocked


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
    except subprocess.CalledProcessError as e:
        log('Opening the pull request failed: ' + (e.stderr or str(e)).strip()[:300])
        return False, ''
    try:
        gh('pr', 'merge', number, '--auto', '--merge')
    except subprocess.CalledProcessError as e:
        log('Auto-merge could not be turned on (' + (e.stderr or str(e)).strip()[:200] +
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


def pull_assets(src, dst, assets, cfg, commit, repo, deps, report, files=None):
    """One pull of exactly these assets at commit; fetch missing dependencies and retry if IDMC reports any.
    Returns {(path, TYPE): (ok, message)}."""
    log('\n== Pull')
    allow = cfg.get('allow_warnings', False)
    tried = set()
    state, msg, objs = 'FAILED', '', []
    for attempt in range(4):
        body = {'commitHash': commit,
                'objects': [{'path': a['path'].split('/'), 'type': a['type']} for a in assets]}
        state, msg, objs = core.run_pull(dst, '/pull', body, cfg)
        log('Pull of ' + str(len(assets)) + ' asset(s) at ' + commit[:7] + ': ' + state + ' ' + msg)
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
    """In DemoCentral: deployed assets get the deployed tag (ready / reviewed / in-review / failed removed).
    A failed asset gets the failed tag in place of ready when a failed tag is set; otherwise its tags are left
    as they are, so it stays ready and the next run tries it again. Returns {(path, TYPE): tag change text}."""
    untag, tag, text = [], [], {}
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
    errors = change_tags(src, 'UntagObjects', untag)
    errors.update(change_tags(src, 'TagObjects', tag))
    for a in assets:
        if a['id'] in errors:
            text[(a['path'], a['type'])] += ' (tagging failed: ' + errors[a['id']] + ')'
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
        core.sync_dependencies(src, dst, assets, cfg, dry_run, report, deps, commit, repo)

        if dry_run:
            log('\n== Pull\nDRY RUN: would pull ' + ', '.join(a['path'] for a in assets) + ' at ' + commit[:7] +
                ' and re-tag them ' + tags['ready'] + ' -> ' + tags['deployed'])
            for a in assets:
                drop = [tags[k] for k in ('ready', 'reviewed', 'in_review', 'failed') if tags.get(k) and tags[k] in a['tags']]
                report['assets'].append(dict(a, result='would deploy', tag_change=', '.join(drop) + ' -> ' + tags['deployed']))
            core.publish(dst, assets, cfg, True, report)
            if tests_on:
                core.run_tests(dst, assets, cfg, True, report, {})
            log('\nDeployment dry run completed')
            return

        outcome = pull_assets(src, dst, assets, cfg, commit, repo, deps, report, files)
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
        core.publish(dst, done, cfg, False, report)
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
