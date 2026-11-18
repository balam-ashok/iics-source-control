# Helper functions for .github/workflows/idmc-build-check.yml and idmc-promote.yml; sourced by their steps.
# ---- Git ---------------------------------------------------------------------------------------------------
# norm_assets: file names (stdin) -> assets, one per line: Explore/<project>/<folder>/<name>.<TYPE>
norm_assets() {
  grep -viE '\.(project|folder)\.json$' \
    | sed -E 's#/\.([^/]+)$#/\1#; s/\.(vc\.json|zip|xml|json|dat)$//' | sort -u
}
# list_assets <base> <head>: assets changed on <head> since it split from <base>
list_assets() {
  git -c core.quotePath=false diff --name-only --diff-filter=d "$1...$2" -- Explore | norm_assets
}
# bullets <file>: asset list as markdown bullets without the Explore/ prefix
bullets() { sed 's/^Explore\//- /' "$1"; }

# ---- IDMC session ------------------------------------------------------------------------------------------
# idmc_login <user> <password>: sets $api (v3 base URL), $host and $session
idmc_login() {
  local r base code
  r=$(curl -s -w '\n%{http_code}' -X POST "$IICS_LOGIN_URL/saas/public/core/v3/login" -H 'Content-Type: application/json' \
        -H 'Accept: application/json' -d "$(jq -n --arg u "$1" --arg p "$2" '{username: $u, password: $p}')") || r=$'\n000'
  code=${r##*$'\n'}; r=${r%$'\n'*}
  if [[ "$code" != 2?? ]]; then
    echo "::error::IDMC login failed for $1 at $IICS_LOGIN_URL (HTTP $code): $(jq -r '.error.message // .message // empty' <<< "$r" 2> /dev/null || true)"
    return 1
  fi
  base=$(jq -r '.products[0].baseApiUrl' <<< "$r")
  api="$base/public/core/v3"; host=${base%/saas}; session=$(jq -r '.userInfo.sessionId' <<< "$r")
  echo "Logged in to IDMC org $(jq -r '.userInfo.orgName' <<< "$r")"
}
# call <curl args>: authenticated IDMC request
call() { curl -sf -H "INFA-SESSION-ID: $session" -H 'Accept: application/json' "$@"; }
# wait_state <status URL>: polls a source control action (up to 30 min) and prints its final state
wait_state() {
  local s=""
  for _ in $(seq 180); do
    s=$(call "$1" | jq -r '.status.state' || true)
    case "$s" in SUCCESSFUL|SUCCESS|WARNING|FAILED|CANCELLED) break ;; esac
    sleep 10
  done
  echo "$s"
}

# ---- IDMC objects ------------------------------------------------------------------------------------------
# gtype <type>: the type name Git uses, for types the IDMC APIs name differently
gtype() {
  case "${1^^}" in
    MCT|MAPPING_TASK|MAPPINGTASK)  echo MTT ;;
    MAPPING)                       echo DTEMPLATE ;;
    SAAS_BSERVICES|SAAS_BSERVICE)  echo BSERVICE ;;
    SYNCHRONIZATION_TASK)          echo DSS ;;
    REPLICATION_TASK)              echo DRS ;;
    *)                             echo "${1^^}" ;;
  esac
}
# find_id <project/folder/name> <TYPE>: the object's id in the logged-in org
find_id() {
  call -G "$api/objects" --data-urlencode "q=location=='${1%/*}'" --data-urlencode "limit=200" \
    | jq -r --arg p "$1" --arg t "$2" '
        def n: ascii_upcase | {MAPPING:"DTEMPLATE", MCT:"MTT", MAPPING_TASK:"MTT", SAAS_BSERVICES:"BSERVICE"}[.] // .;
        [.objects[] | select((.path | ltrimstr("/") | ltrimstr("Explore/")) == $p)] | (map(select((.type | n) == ($t | n))) + .)[0].id // empty'
}
# tagged_assets <tag>: assets carrying <tag> in the logged-in org, as Explore/<project>/<folder>/<name>.<TYPE>
# An unknown tag (HTTP 400 / 404 from IDMC) means no asset carries it: prints nothing and succeeds.
tagged_assets() {
  local skip=0 page n p t out code
  while :; do
    out=$(curl -s -w '\n%{http_code}' -G -H "INFA-SESSION-ID: $session" -H 'Accept: application/json' "$api/objects" \
            --data-urlencode "q=tag=='$1'" --data-urlencode "limit=200" --data-urlencode "skip=$skip") || out=$'\n000'
    code=${out##*$'\n'}; page=${out%$'\n'*}
    case "$code" in
      2??) ;;
      400|404)
        echo "::notice::No assets tagged $1 (IDMC answered HTTP $code: $(jq -r '.error.message // .message // empty' <<< "$page" 2> /dev/null || true))" >&2
        return 0 ;;
      *) echo "::error::Could not list the assets tagged $1 in IDMC (HTTP $code)" >&2; return 1 ;;
    esac
    while IFS=$'\t' read -r p t; do
      if [ -n "$p" ]; then echo "Explore/$p.$(gtype "$t")"; fi
    done < <(jq -r '.objects[]? | [((.path // "") | ltrimstr("/") | ltrimstr("Explore/")), (.type // "")] | @tsv' <<< "$page")
    n=$(jq '.objects | length' <<< "$page")
    if [ "$n" -lt 200 ]; then break; fi
    skip=$((skip + n))
  done
}
# walk_uses <assets file> <file names list>: follows "uses" references in the logged-in org, up to 5 levels.
# Writes deps.txt (used assets whose Explore/<path>.<TYPE>. is in the list), connections.txt, runtimes.txt,
# outside.txt (used assets not in the list) and unresolved.txt (assets not found in the org, so not walked).
walk_uses() {
  local queue=() next seen=" " a p id rid rtype rpath t
  : > deps.txt; : > connections.txt; : > runtimes.txt; : > outside.txt; : > unresolved.txt
  while IFS= read -r a; do
    p=${a#Explore/}; id=$(find_id "${p%.*}" "${p##*.}")
    if [ -n "$id" ]; then queue+=("$id"); else echo "::warning::$p not found in the source org"; echo "$a" >> unresolved.txt; fi
  done < "$1"
  for _ in 1 2 3 4 5; do
    [ ${#queue[@]} -gt 0 ] || break
    next=()
    for id in "${queue[@]}"; do
      case "$seen" in *" $id "*) continue ;; esac
      seen="$seen$id "
      while IFS=$'\t' read -r rid rtype rpath; do
        [ -n "$rid" ] || continue
        t=$(gtype "$rtype")
        if [[ "$t" == *CONNECTION* && "$t" != AI_* ]]; then
          echo "${rpath##*/}" >> connections.txt
        elif [[ "$t" == *AGENT* || "$t" == *RUNTIME* ]]; then
          echo "${rpath##*/}" >> runtimes.txt
        elif grep -Fqi "Explore/$rpath.$t." "$2"; then
          echo "Explore/$rpath.$t" >> deps.txt; next+=("$rid")
        elif [ -n "$rpath" ]; then
          echo "Explore/$rpath.$t" >> outside.txt
        fi
      done < <(call -G "$api/objects/$id/references" --data-urlencode "refType=Uses" --data-urlencode "limit=50" \
        | jq -r '.references[]? | [.id, (.documentType // .type // ""),
            ((.path // "") | (if type == "array" then join("/") else . end) | ltrimstr("/") | ltrimstr("Explore/")) as $p
            | (.name // "") as $n
            | (if $n != "" and ($p | split("/") | last) != $n then ($p + "/" + $n | ltrimstr("/")) else $p end)] | @tsv')
    done
    queue=("${next[@]}")
  done
  sort -u -o deps.txt deps.txt
  sort -u -o connections.txt connections.txt
  sort -u -o runtimes.txt runtimes.txt
  sort -u -o outside.txt outside.txt
  sort -u -o unresolved.txt unresolved.txt
}
# retag <assets file> <tag to remove or ""> <tag to add>: changes the IDMC tags in the logged-in org
retag() {
  local asset path id ids=()
  while IFS= read -r asset; do
    path=${asset#Explore/}
    id=$(find_id "${path%.*}" "${path##*.}")
    if [ -n "$id" ]; then ids+=("$id"); else echo "::warning::${path} not found in the source org, not tagged"; fi
  done < "$1"
  [ ${#ids[@]} -gt 0 ] || return 0
  if [ -n "$2" ]; then
    printf '%s\n' "${ids[@]}" | jq -R . | jq -s --arg t "$2" 'map({id: ., tags: [$t]})' \
      | call -X POST "$api/UntagObjects" -H 'Content-Type: application/json' -d @- > /dev/null || true
  fi
  printf '%s\n' "${ids[@]}" | jq -R . | jq -s --arg t "$3" 'map({id: ., tags: [$t]})' \
    | call -X POST "$api/TagObjects" -H 'Content-Type: application/json' -d @- > /dev/null \
    || echo "::warning::Could not add the tag $3 in the source org"
  echo "Tagged ${#ids[@]} asset(s) $3 in the source org"
}

# ---- Release selection -------------------------------------------------------------------------------------
# select_release <source commit> <target ref> (source org, logged in): what a release from <source commit> promotes.
# changed.txt: assets that differ from <target ref>; tagged.txt: assets tagged $TAG_READY in the org; selected.txt:
# changed and tagged (compared without case, spelled as in Git); promote.txt: selected + changed assets they use;
# used.txt: the added ones; left.txt: changed, not promoted. Plus walk_uses' files (outside.txt, unresolved.txt, ...).
select_release() {
  : > tagged.txt; : > selected.txt; : > promote.txt; : > used.txt; : > left.txt; : > outside.txt; : > unresolved.txt
  git -c core.quotePath=false diff --name-only --diff-filter=d "$2" "$1" -- Explore | norm_assets > changed.txt || true
  [ -s changed.txt ] || return 0
  tagged_assets "$TAG_READY" > tagged.txt || return 1
  awk 'NR == FNR { t[tolower($0)] = 1; next } tolower($0) in t' tagged.txt changed.txt > selected.txt
  [ -s selected.txt ] || return 0
  sed 's/$/./' changed.txt > changed_prefix.txt
  walk_uses selected.txt changed_prefix.txt
  awk 'NR == FNR { t[tolower($0)] = 1; next } tolower($0) in t' <(cat selected.txt deps.txt) changed.txt > promote.txt
  awk 'NR == FNR { s[$0] = 1; next } !($0 in s)' selected.txt promote.txt > used.txt
  awk 'NR == FNR { s[$0] = 1; next } !($0 in s)' promote.txt changed.txt > left.txt
}

# ---- Connection / runtime environment renames --------------------------------------------------------------
# Naming convention: an org's connection / runtime environment names carry its environment name as an _-separated
# part, e.g. CONN_DEV_WMI_SNOWFLAKE_MDM / CONN_TST_... / CONN_PRD_..., SAG_Dev_Azure_windows01 / SAG_Tst_... (case kept).
# Git always holds the Dev names, in the assets' metadata files (Explore/**/.<name>.<TYPE>.json, objectRefs).
# name_map <target env> [assets file]: writes names.json and spec.json, the pull's objectSpecification (each Dev name
# with its DEV part swapped for <target env>). A name with no DEV part keeps its name. With an assets file, only
# their metadata files are read, otherwise every asset's under Explore.
name_map() {
  # pipefail: a metadata file jq cannot parse must fail the map, not leave it partial
  if ! (set -o pipefail
        if [ -n "$2" ]; then
          sed -E 's#^(.*)/([^/]+)$#\1/.\2.json#' "$2" | while IFS= read -r f; do
            if [ -f "$f" ]; then printf '%s\0' "$f"; fi
          done
        elif [ -d Explore ]; then find Explore -type f -name '.*.json' -print0; fi \
    | xargs -0 -r jq -c '.objectRefs[]? | select(.type | test("^(Connection|AgentGroup|RuntimeEnvironment)$"; "i"))
                         | {kind: (if .type == "Connection" then "Connection" else "AgentGroup" end), from: .name}' \
    | jq -s --arg to "$1" '
        def swap: split("_") | map(if ascii_downcase == "dev"
            then (if . == ascii_upcase then ($to | ascii_upcase) elif . == ascii_downcase then ($to | ascii_downcase)
                  else ($to | ascii_downcase | (.[0:1] | ascii_upcase) + .[1:]) end)
            else . end) | join("_");
        unique | map(. + {to: (.from | swap)})' > names.json); then
    echo "::error::Could not read the connection / runtime environment names from the metadata files in Git"; return 1
  fi
  { echo "### Connection and runtime environment names, DEV -> $1"
    if [ "$(jq length names.json)" = "0" ]; then echo "None used by the assets in Git"; fi
    jq -r '.[] | "- \(if .kind == "Connection" then "Connection" else "Runtime environment" end): \(.from) -> \(.to)"' names.json
  } | tee -a "$GITHUB_STEP_SUMMARY"
  # an absent source name is ignored by the pull (relaxObjectSpecificationValidation)
  jq '[ .[] | select(.from != .to) | {source: {path: [.from], type: .kind}, target: {path: [.to], type: .kind}} ]' \
    names.json > spec.json || return 1
}
# check_target_names <target env> (target org): fails unless each name in names.json exists in the logged-in org
check_target_names() {
  local v2=${api%/public/core/v3}/api/v2 missing r
  r=$(call_v2 "$v2/connection") || { echo "::error::Could not list the connections in $1"; return 1; }
  jq -r '.[] | "Connection|\(.name)"' <<< "$r" > have.txt
  if jq -e 'any(.kind == "AgentGroup")' names.json > /dev/null; then
    r=$(call_v2 "$v2/runtimeEnvironment") || { echo "::error::Could not list the runtime environments in $1"; return 1; }
    jq -r '.[] | "AgentGroup|\(.name)"' <<< "$r" >> have.txt
  fi
  missing=$(jq -r --rawfile have have.txt '($have | split("\n")) as $h
    | [.[] | select("\(.kind)|\(.to)" | IN($h[]) | not) | .to] | join(", ")' names.json)
  if [ -n "$missing" ]; then echo "::error::Not found in the $1 org: $missing"; return 1; fi
}
# call_v2 <url>: authenticated IDMC v2 request (v2 takes the session in icSessionId)
call_v2() { curl -sf -H "icSessionId: $session" -H 'Accept: application/json' "$1"; }
# check_target_assets <target name> (target org): fails unless each used asset outside Git (outside.txt from
# walk_uses) exists in the logged-in org, and every release asset was found in the source org (unresolved.txt)
check_target_assets() {
  local a p missing=()
  if [ -s unresolved.txt ]; then
    echo "::error::Not found in the source org (missing, or hidden from its service user), so what they use could not be checked: $(sed 's#^Explore/##' unresolved.txt | paste -sd ' ' -)"
    return 1
  fi
  [ -s outside.txt ] || return 0
  while IFS= read -r a; do
    p=${a#Explore/}
    if [ -z "$(find_id "${p%.*}" "${p##*.}")" ]; then missing+=("$p"); fi
  done < outside.txt
  { echo "### Used assets not in Git, must already be in $1"; bullets outside.txt; } | tee -a "$GITHUB_STEP_SUMMARY"
  if [ ${#missing[@]} -gt 0 ]; then
    echo "::error::Used by the release but neither in Git nor in the $1 org (or hidden from its service user): ${missing[*]}. Check them in from Dev and tag them for promotion."
    return 1
  fi
}

# ---- Deploy: the steps of the deploy job in idmc-promote.yml; SRC_IICS_* log in to the source org, IICS_* to the target
# require <VAR>...: fails unless each named environment variable is set
require() {
  local v missing=""
  for v in "$@"; do
    if [ -z "${!v}" ]; then missing="$missing $v"; fi
  done
  if [ -n "$missing" ]; then echo "::error::Not set:$missing"; return 1; fi
}
# deploy_find_deps (source org): all.txt = the pushed assets + the assets they use that are in Git (connections /
# runtime environments are not in Git; they must already exist in the target org under the mapped names)
deploy_find_deps() {
  : > all.txt; : > deps.txt; : > connections.txt; : > runtimes.txt
  list_assets "$BASE" "$HEAD" > assets.txt || true
  if [ ! -s assets.txt ]; then echo "No IDMC assets to deploy."; return 0; fi
  git -c core.quotePath=false ls-tree -r --name-only "$MERGE_SHA" -- Explore > gitfiles.txt
  idmc_login "$SRC_IICS_USERNAME" "$SRC_IICS_PASSWORD"
  walk_uses assets.txt gitfiles.txt
  sort -u assets.txt deps.txt > all.txt

  { echo "### To deploy"; bullets all.txt
    echo "### Connections the assets use in $SRC_NAME"
    if [ -s connections.txt ]; then sed 's/^/- /' connections.txt; else echo "None found"; fi
    echo "### Runtime environments the assets use in $SRC_NAME"
    if [ -s runtimes.txt ]; then sed 's/^/- /' runtimes.txt; else echo "None detected"; fi
  } | tee -a "$GITHUB_STEP_SUMMARY"
}
# deploy_pull (target org): pulls all.txt into the target org at the merge commit and publishes the CAI assets
deploy_pull() {
  [ -s all.txt ] || { echo "No IDMC assets to deploy."; return 0; }
  git -c core.quotePath=false ls-tree -r --name-only "$MERGE_SHA" -- Explore > gitfiles.txt
  # pull.txt: the projects / folders holding the assets (a pull does not create missing ones), then the assets
  {
    while IFS= read -r asset; do
      dir=${asset#Explore/}; dir=${dir%/*}; acc=""; kind=Project
      IFS=/ read -ra parts <<< "$dir"
      for part in "${parts[@]}"; do
        acc=${acc:+$acc/}$part
        if grep -Fqx "Explore/$acc/$part.$kind.json" gitfiles.txt; then echo "$acc|$kind"; fi
        kind=Folder
      done
    done < all.txt | awk '!seen[$0]++'
    sed -E 's#^Explore/##; s#\.([^.]+)$#|\1#' all.txt
  } > pull.txt

  name_map "$DST_ENV" || exit 1

  # pull at the merge commit
  body=$(jq -R 'split("|") | {path: (.[0] | split("/")), type: .[1]}' pull.txt \
         | jq -s --arg c "$MERGE_SHA" --slurpfile spec spec.json \
             '{commitHash: $c, objects: .} + (if ($spec[0] | length) > 0
                then {objectSpecification: $spec[0], relaxObjectSpecificationValidation: true} else {} end)')
  idmc_login "$IICS_USERNAME" "$IICS_PASSWORD"
  check_target_assets "$DST_NAME" || exit 1
  resp=$(curl -s -X POST "$api/pull" -H "INFA-SESSION-ID: $session" -H 'Content-Type: application/json' -d "$body")
  action=$(jq -r '.pullActionId // empty' <<< "$resp" 2> /dev/null || true)
  if [ -z "$action" ]; then echo "::error::IDMC did not start the pull: $resp"; exit 1; fi
  state=$(wait_state "$api/sourceControlAction/$action")
  status=$(call "$api/sourceControlAction/$action?expand=objects")
  { echo "### Pull into $DST_NAME: $state"
    jq -r '.objects[]? | "- \(.target.status.state // .status.state) \(.target.type // .source.type) \((.target.path // .source.path) | if type == "array" then join("/") else . end) \(.target.status.message // .status.message // "")"' <<< "$status"
  } | tee -a "$GITHUB_STEP_SUMMARY"
  if [ "$state" != "SUCCESSFUL" ]; then
    echo "::error::Pull into $DST_NAME ended $state: $(jq -r '.status.message // ""' <<< "$status")"; exit 1
  fi

  # publish CAI assets and taskflows (connections first)
  paths=$(for t in AI_CONNECTION AI_SERVICE_CONNECTOR PROCESS GUIDE TASKFLOW; do
            { grep -i "\.$t$" all.txt || true; } | while IFS= read -r a; do
              if grep -Fqx "$a.xml" gitfiles.txt; then echo "$a.xml"; fi
            done
          done)
  if [ -n "$paths" ]; then
    if jq -Rn '[inputs] | {data: {type: "publish", attributes: {assetPaths: .}}}' <<< "$paths" \
         | curl -sf -X POST "$host/active-bpel/asset/v1/publish" -H "INFA-SESSION-ID: $session" \
             -H 'Accept: application/vnd.api+json' -H 'Content-Type: application/vnd.api+json' -d @- > /dev/null; then
      echo "Publish submitted for: $paths"
    else
      echo "::warning::Publish failed; publish these in $DST_NAME by hand: $paths"
    fi
  fi
}
# deploy_retag (source org): swaps the ready tag for the done tag on the deployed assets
deploy_retag() {
  [ -s all.txt ] || return 0
  idmc_login "$SRC_IICS_USERNAME" "$SRC_IICS_PASSWORD"
  retag all.txt "$TAG_READY" "$TAG_DONE"
}
# deploy_git_tag: tags the merge commit $RELEASE_TAG (vX.Y.Z-tst after Test, vX.Y.Z after Prod)
deploy_git_tag() {
  local at
  if at=$(git rev-parse -q --verify "refs/tags/$RELEASE_TAG^{commit}"); then
    if [ "$at" = "$MERGE_SHA" ]; then echo "Git tag $RELEASE_TAG is already on $MERGE_SHA"; return 0; fi
    echo "::error::Git tag $RELEASE_TAG already exists on $at, not on the deployed commit $MERGE_SHA"; return 1
  fi
  git -c user.name="github-actions[bot]" -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
    tag -a "$RELEASE_TAG" -m "Deployed to $DST_NAME" -m "$(bullets all.txt)" "$MERGE_SHA"
  git push origin "refs/tags/$RELEASE_TAG"
  echo "Created the Git tag $RELEASE_TAG on $MERGE_SHA" | tee -a "$GITHUB_STEP_SUMMARY"
}
# deploy_report: labels the PR and comments the result ($RESULT is the job status)
deploy_report() {
  if [ "$RESULT" = "success" ]; then
    gh label create "$DEPLOYED_LABEL" --color 1D76DB --force
    gh pr edit "$PR" --add-label "$DEPLOYED_LABEL" || true
    echo "@$AUTHOR deployed to $DST_NAME, Git tag **$RELEASE_TAG** created, and tagged **$TAG_DONE** in $SRC_NAME. Details: $RUN_URL" > comment.md
  else
    echo "@$AUTHOR the deployment to $DST_NAME **failed**; the assets keep the **$TAG_READY** tag. See $RUN_URL" > comment.md
  fi
  gh pr comment "$PR" --body-file comment.md
}
