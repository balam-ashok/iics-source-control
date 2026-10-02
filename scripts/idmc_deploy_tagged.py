"""
IDMC: deploy the assets tagged ready-to-deploy in the Demo org to the Dfactory org, then re-tag them.

Instead of deploying every check-in, this deploys only the assets a reviewer has tagged (default tag
cicd-ready-qa) and that are safe to deploy:
  - under source control and not checked out (so the version in DemoCentral is the checked-in one)
  - their last check-in is merged into Dfactory-Branch (the branch the Dfactory org pulls from)
  - not tagged cicd-hold, and (if RELEASE_TAG is given) also carrying that release tag

Steps: find the tagged assets -> check each one -> connections, schedules and missing dependencies (as
idmc_cicd.py does) -> one pull of exactly those assets from Dfactory-Branch -> re-tag them in DemoCentral
(cicd-ready-qa -> cicd-deployed-qa, or cicd-failed-qa) -> re-link schedules, publish, test -> summary.

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
from idmc_cicd import DeployError, Org, add_id, env_flag, log, norm_type

DEFAULT_TAGS = {'in_review': 'cicd-in-review', 'ready': 'cicd-ready-qa', 'deployed': 'cicd-deployed-qa',
                'failed': 'cicd-failed-qa', 'hold': 'cicd-hold'}
NOT_DEPLOYABLE = {'PROJECT', 'FOLDER'}


def load_config():
    with open(core.CONFIG_PATH, encoding='utf-8') as f:
        cfg = yaml.safe_load(f) or {}
    for k in ('connections', 'schedules', 'publish', 'tests'):
        cfg.setdefault(k, {})
    cfg.setdefault('target_branch', 'Dfactory-Branch')
    cfg['allow_warnings'] = env_flag('ALLOW_WARNINGS', cfg.get('allow_warnings', False))
    tags = dict(DEFAULT_TAGS)
    tags.update({k: v for k, v in (cfg.get('tags') or {}).items() if v})
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
        elif tags['hold'] in otags:
            reason = 'on hold (' + tags['hold'] + ')'
        elif release and release not in otags:
            reason = 'not in release ' + release
        elif sc.get('sourceControlled') is False:
            reason = 'never checked in'
        elif sc.get('checkedOutBy'):
            reason = 'checked out by ' + str(sc['checkedOutBy']) + '; check it in first'
        elif (path, t) not in files:
            reason = 'not found in Git on ' + ref.split('/')[-1] + '; check it in and let the Auto PR merge it'
        elif commit and not merged_into(commit, ref):
            reason = 'last check-in ' + commit[:7] + ' is not merged into ' + ref.split('/')[-1] + ' yet'
        row = {'path': path, 'type': t, 'id': o.get('id'), 'tags': otags, 'checkin': commit[:7] or '-',
               'checkin_by': sc.get('lastCheckinBy') or '-'}
        if reason:
            skipped.append(dict(row, reason=reason))
            continue
        xml = next((f for f in files[(path, t)] if f.endswith('.xml') and not f.split('/')[-1].startswith('.')), None)
        ready.append(dict(row, deleted=False, xml=xml))
    return ready, skipped


def pull_assets(src, dst, assets, cfg, commit, repo, deps, report):
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
    report['pulled_objects'].extend(objs)
    by_key = {}
    for o in objs:
        log('  ' + str(o['state']) + ' ' + str(o['type']) + ' ' + o['path'] + (': ' + o['message'] if o['message'] else ''))
        if o.get('raw'):
            log('      IDMC response for this object: ' + json.dumps(o.pop('raw'), default=str)[:1500])
        by_key[(o['path'], norm_type(o['type']))] = o
    whole_ok = state == 'SUCCESSFUL' or (state == 'WARNING' and allow)
    out = {}
    for a in assets:
        o = by_key.get((a['path'], a['type']))
        if o is not None:
            ok = str(o['state']).upper() == 'SUCCESSFUL' or (str(o['state']).upper() == 'WARNING' and allow)
            out[(a['path'], a['type'])] = (ok, o['message'] or str(o['state']))
        else:
            out[(a['path'], a['type'])] = (whole_ok, msg or state)
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
    """In DemoCentral: deployed assets -> deployed tag (ready / failed / in-review removed);
    failed ones -> failed tag (ready removed). Returns {(path, TYPE): tag change text}."""
    untag, tag, text = [], [], {}
    for a in assets:
        ok, _ = outcome[(a['path'], a['type'])]
        drop = [t for t in ([tags['ready'], tags['failed'], tags['in_review']] if ok else [tags['ready']])
                if t in a['tags']]
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
        assets, skipped = select(tagged, tags, release, files, ref)
        report['skipped'] = skipped
        log('Assets tagged ' + tags['ready'] + (' and ' + release if release else '') + ': ' + str(len(tagged)) +
            ' found, ' + str(len(assets)) + ' to deploy, ' + str(len(skipped)) + ' skipped')
        for s in skipped:
            log('  skip ' + s['type'] + ' ' + s['path'] + ': ' + s['reason'])
        for a in assets:
            log('  deploy ' + a['type'] + ' ' + a['path'] + ' (last check-in ' + a['checkin'] + ' by ' + a['checkin_by'] + ')')
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
                report['assets'].append(dict(a, result='would deploy', tag_change=tags['ready'] + ' -> ' + tags['deployed']))
            core.publish(dst, assets, cfg, True, report)
            if tests_on:
                core.run_tests(dst, assets, cfg, True, report, {})
            log('\nDeployment dry run completed')
            return

        outcome = pull_assets(src, dst, assets, cfg, commit, repo, deps, report)
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
