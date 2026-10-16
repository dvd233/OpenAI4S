#!/usr/bin/env python3
import hashlib,json,os,pathlib,re,shutil,signal,subprocess,sys,time,traceback,threading
MAIN="751593d170fbcaea7ecc83a15a1d8c3e8e0375d5"
BASE="571b237b1521715f8c4caeb7fa409a2d8463110d"
FORK="dvd233/OpenAI4S"; UPSTREAM="PKU-YuanGroup/OpenAI4S"
REF="refs/heads/codex/validate-openai4s-closeout-20261017"
SOURCES={
"214":("test/paroutes-groupby-source","9252e7c9147c3b0ab83abd525e71d77cd7b32ace"),
"215":("fix/compute-status-read-truth-20261005","361694cd1dfc1ae2188fab8b95bf8015a4a50558"),
"216":("fix/execution-history-response-ownership","c3d6944276d9ff05876a30458476f9be6c3e91f3")}
PR=os.environ["TARGET_PR"]; BRANCH,SOURCE=SOURCES[PR]; GIB=1024**3
MAX_LOG=32*1024**2; MAX_BLOB=32*1024**2; MAX_ARTIFACT=128*1024**2; META_RESERVE=1024**2
ROOT=pathlib.Path(os.environ["RUNNER_TEMP"])/("openai4s-closeout-195-"+PR)
REPO=ROOT/"product"; OUT=pathlib.Path(os.environ["GITHUB_WORKSPACE"])/"evidence"
OUT.mkdir(exist_ok=True); (OUT/"logs").mkdir(exist_ok=True)
S={"schema":1,"pr":int(PR),"source":SOURCE,"branch":BRANCH,"main":MAIN,"base":BASE,
"commands":[],"ready_for_root_review":False,"remote_writes":0,
"limitations":["Python3.12/Linux and Chromium only; not the entire upstream CI matrix.",
"No macOS/Windows, Firefox/WebKit, single-cell, container/release packaging or sandbox-platform jobs.",
"No live LLM/GPU/SSH/lab, fresh Zenodo download, scientific-validity or maintainer approval claim."]}
S["run"]={k:os.environ.get(k) for k in ["GITHUB_RUN_ID","GITHUB_RUN_ATTEMPT","GITHUB_SHA","GITHUB_REF","GITHUB_REPOSITORY","GITHUB_REPOSITORY_ID","GITHUB_REPOSITORY_OWNER_ID","GITHUB_WORKFLOW_REF"]}
S["driver_sha256"]=hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()
S["limits"]={"log_bytes":MAX_LOG,"blob_bytes":MAX_BLOB,"artifact_bytes":MAX_ARTIFACT,"metadata_reserve_bytes":META_RESERVE}
E=os.environ.copy()
E.update(GIT_TERMINAL_PROMPT="0",GIT_CONFIG_NOSYSTEM="1",
UV_CACHE_DIR=str(ROOT/"uv-cache"),UV_PYTHON_INSTALL_DIR=str(ROOT/"python"),
npm_config_cache=str(ROOT/"npm-cache"),PRE_COMMIT_HOME=str(ROOT/"precommit"),
PLAYWRIGHT_BROWSERS_PATH=str(ROOT/"browsers"),TMPDIR=str(ROOT/"tmp"),
TEMP=str(ROOT/"tmp"),TMP=str(ROOT/"tmp"),PYTHONDONTWRITEBYTECODE="1",
GIT_AUTHOR_NAME="OpenAI4S closeout validation",GIT_AUTHOR_EMAIL="actions@users.noreply.github.com",
GIT_COMMITTER_NAME="OpenAI4S closeout validation",GIT_COMMITTER_EMAIL="actions@users.noreply.github.com")
DAEMON=None
def artifact_bytes():
    return sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
def artifact_write(path,data,blob=False):
    if isinstance(data,str): data=data.encode("utf-8")
    if blob and len(data)>MAX_BLOB: raise RuntimeError("Artifact blob exceeds32MiB: "+path.name)
    old=path.stat().st_size if path.exists() else 0
    if artifact_bytes()-old+len(data)>MAX_ARTIFACT-META_RESERVE:
        raise RuntimeError("Artifact128MiB budget exhausted before "+path.name)
    path.write_bytes(data)
def save():
    data=json.dumps(S,indent=2).encode("utf-8")
    if len(data)>META_RESERVE: raise RuntimeError("Summary exceeded reserved metadata budget")
    target=OUT/"summary.json"; old=target.stat().st_size if target.exists() else 0
    if artifact_bytes()-old+len(data)>MAX_ARTIFACT: raise RuntimeError("Total artifact quota exceeded")
    target.write_bytes(data)
def runtime_versions():
    result={"driver_python":sys.version,"driver_executable":sys.executable}
    for name,args in [("git",["git","--version"]),("node",["node","--version"]),
        ("npm",["npm","--version"]),("uv",["uv","--version"])]:
        r=subprocess.run(args,cwd=ROOT,env=E,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=30)
        result[name]={"exit_code":r.returncode,"output":r.stdout.decode("utf-8","replace")[:4096]}
    return result
def free(): return shutil.disk_usage(os.environ["RUNNER_TEMP"]).free
def sha(b): return hashlib.sha256(b).hexdigest()
def stop(p):
    if p.poll() is None:
        os.killpg(p.pid,signal.SIGTERM)
        try: p.wait(timeout=10)
        except subprocess.TimeoutExpired: os.killpg(p.pid,signal.SIGKILL); p.wait()
def run(label,args,cwd=None,timeout=2700,fatal=False,env=None):
    log=OUT/"logs"/(label+".log"); start=time.time()
    remaining=MAX_ARTIFACT-META_RESERVE-artifact_bytes()
    budget=min(MAX_LOG,max(0,remaining))
    row={"label":label,"argv":args,"free_before":free(),"started_at":start,
         "log_limit_bytes":MAX_LOG,"allocated_log_bytes":budget}
    if budget<65536:
        row.update(exit_code=None,raw_exit_code=None,passed=False,terminated="artifact_limit")
        S["commands"].append(row); save()
        if fatal: raise RuntimeError(label+" cannot fit remaining artifact budget")
        return False
    overflow=threading.Event(); capture={"bytes":0,"tail":b"","error":None}
    with log.open("wb") as f:
        p=subprocess.Popen(args,cwd=cwd or REPO,env=env or E,stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT,start_new_session=True)
        def pump():
            try:
                while True:
                    chunk=p.stdout.read(65536)
                    if not chunk: break
                    capture["bytes"]+=len(chunk)
                    capture["tail"]=(capture["tail"]+chunk)[-65536:]
                    space=max(0,budget-f.tell())
                    if space: f.write(chunk[:space])
                    if len(chunk)>space: overflow.set()
            except Exception as exc:
                capture["error"]=type(exc).__name__; overflow.set()
        reader=threading.Thread(target=pump,daemon=True); reader.start()
        while p.poll() is None:
            reason=None
            if overflow.is_set(): reason="log_limit" if budget==MAX_LOG else "artifact_limit"
            elif time.time()-start>timeout: reason="timeout"
            elif free()<11*GIB: reason="disk_guard"
            if reason: row["terminated"]=reason; stop(p); break
            time.sleep(.2)
        reader.join(timeout=15)
        if reader.is_alive():
            row["terminated"]="output_pipe_timeout"
            try: os.killpg(p.pid,signal.SIGTERM)
            except ProcessLookupError: pass
            reader.join(timeout=5)
            if reader.is_alive(): raise RuntimeError("Output reader did not stop")
        if overflow.is_set():
            row["terminated"]="log_limit" if budget==MAX_LOG else "artifact_limit"
            marker=b"\n[OUTPUT_LIMIT: head truncated; bounded final output tail follows]\n"
            tail=capture["tail"][-max(1,budget-len(marker)):]; keep=max(0,budget-len(marker)-len(tail))
            f.truncate(keep); f.seek(keep); f.write(marker+tail)
            row["log_truncated"]=True
        if capture["error"]: row["capture_error"]=capture["error"]
        passed=p.returncode==0 and "terminated" not in row and not capture["error"]
        row.update(exit_code=p.returncode,raw_exit_code=p.returncode,passed=passed,
            total_output_bytes_observed=capture["bytes"],duration_seconds=round(time.time()-start,3),
            free_after=free(),log=str(log.relative_to(OUT)))
    if log.stat().st_size>MAX_LOG: raise RuntimeError("Log cap invariant failed")
    S["commands"].append(row); save()
    if fatal and not passed: raise RuntimeError(label+" failed; bounded log/tail retained")
    return passed
def git(*args):
    p=subprocess.run(["git",*args],cwd=REPO,env=E,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if p.returncode: raise RuntimeError("git "+str(args[:3])+": "+p.stderr.decode("utf-8","replace")[-1000:])
    return p.stdout
def gt(*args): return git(*args).decode().strip()
def paths(a,b): return [p.decode() for p in git("diff","--no-renames","--name-only","-z",a,b).split(b"\0") if p]
def clean(label):
    status=gt("status","--porcelain","--untracked-files=no")
    if status:
        S["tracked_drift"]={"after":label,"status":status}
        artifact_write(OUT/("drift-"+label+".patch"),git("diff","--binary","HEAD"))
        raise RuntimeError("Tracked tree changed during "+label)
def gate(label,args,**kw):
    clean("before-"+label); ok=run(label,args,**kw); clean(label); return ok
def exact_ref(url,branch,expected):
    p=subprocess.run(["git","ls-remote","--heads",url,"refs/heads/"+branch],env=E,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=90)
    lines=p.stdout.decode().strip().splitlines()
    if p.returncode or len(lines)!=1 or lines[0].split()[0]!=expected: raise RuntimeError("Remote ref moved/unreadable: "+branch)
def readme(name):
    p=REPO/name; text=p.read_text(encoding="utf-8")
    pattern=re.compile(r"^<<<<<<<[^\n]*\n(.*?)^=======\n(.*?)^>>>>>>>[^\n]*\n",re.M|re.S)
    ms=list(pattern.finditer(text))
    if len(ms)!=1: raise RuntimeError("Unexpected README conflicts: "+name)
    a,b=ms[0].group(1),ms[0].group(2)
    an=re.findall(r"\[\x60([^\x60]+)\x60\]",a); bn=re.findall(r"\[\x60([^\x60]+)\x60\]",b)
    expected=["test_project_folders.py","test_project_history.py","test_project_history_autosave.py","test_project_history_routes.py","browser_project_folders.mjs"]
    if an!=["test_provenance_groupby.py"] or bn!=expected: raise RuntimeError("Unexpected README rows")
    if any(not x.startswith("| ") for x in (a+b).splitlines()): raise RuntimeError("Unexpected README payload")
    result=text[:ms[0].start()]+b+a+text[ms[0].end():]
    if re.search(r"^(<<<<<<<|=======|>>>>>>>|\|\|\|\|\|\|\|)",result,re.M): raise RuntimeError("Conflict marker remains")
    p.write_text(result,encoding="utf-8",newline="\n")
    S.setdefault("readme_resolutions",[]).append({"path":name,"kept_original":an,"kept_upstream":bn,"sha256":sha(result.encode())})
def export(commit):
    (OUT/"blobs").mkdir(exist_ok=True); records=[]
    for name in paths(MAIN,commit):
        entry=git("ls-tree","-z",commit,"--",name).split(b"\0")[0]
        if not entry: records.append({"path":name,"operation":"delete"}); continue
        meta,_=entry.split(b"\t",1); mode,kind,oid=meta.decode().split()
        if kind!="blob": raise RuntimeError("Unexpected tree/gitlink delta")
        size=int(gt("cat-file","-s",oid))
        if size>MAX_BLOB: raise RuntimeError("Candidate blob exceeds32MiB before reading: "+name)
        b=git("cat-file","blob",oid); artifact_write(OUT/"blobs"/oid,b,blob=True)
        records.append({"path":name,"operation":"upsert","mode":mode,"type":kind,"oid":oid,"bytes":len(b),"sha256":sha(b),"artifact_file":"blobs/"+oid})
    for a,label in [(SOURCE,"original-head"),(MAIN,"current-main")]:
        artifact_write(OUT/("delta-from-"+label+".patch"),git("diff","--binary","--full-index",a,commit))
        artifact_write(OUT/("delta-from-"+label+".txt"),git("diff","--name-status",a,commit))
    artifact_write(OUT/"original-contribution.patch",git("diff","--binary","--full-index",BASE,SOURCE))
    tree=gt("rev-parse",commit+"^{tree}")
    artifact_write(OUT/"tree-manifest.json",json.dumps({"validation_commit":commit,"tree":tree,"parents":[SOURCE,MAIN],"base_tree":gt("rev-parse",MAIN+"^{tree}"),"files":records,
    "publication_rule":"Root rechecks remote original head and reviews this exact tree before a normal two-parent Git Data merge. Runner commit identity/metadata is validation-only."},indent=2))
    S.update(candidate_commit=commit,candidate_tree=tree,candidate_parents=[SOURCE,MAIN])
def prepare():
    if os.environ.get("GITHUB_REPOSITORY")!=FORK or os.environ.get("GITHUB_REPOSITORY_ID")!="1383248212": raise RuntimeError("Wrong repo")
    if os.environ.get("GITHUB_REPOSITORY_OWNER_ID")!="111864431" or os.environ.get("GITHUB_REF")!=REF: raise RuntimeError("Wrong owner/ref")
    S["preflight"]={"free_bytes":free(),"estimated_peak_bytes":8*GIB,"reserve_bytes":10*GIB,"stop_floor_bytes":11*GIB}
    if free()<18*GIB: raise RuntimeError("Disk below estimated8GiB + reserve10GiB")
    if ROOT.exists(): raise RuntimeError("Runner task directory already exists")
    ROOT.mkdir(); (ROOT/"tmp").mkdir(); REPO.mkdir()
    S["runtime_versions"]=runtime_versions(); save()
    exact_ref("https://github.com/"+FORK+".git",BRANCH,SOURCE)
    exact_ref("https://github.com/"+UPSTREAM+".git","main",MAIN)
    run("git-init",["git","init","."],fatal=True)
    run("fetch-original",["git","fetch","--no-tags","https://github.com/"+FORK+".git",SOURCE],timeout=900,fatal=True)
    run("fetch-main",["git","fetch","--no-tags","https://github.com/"+UPSTREAM+".git",MAIN],timeout=900,fatal=True)
    git("checkout","-b","validation-candidate",SOURCE)
    if gt("merge-base",SOURCE,MAIN)!=BASE: raise RuntimeError("Merge base changed")
    original=paths(BASE,SOURCE); upstream=paths(BASE,MAIN)
    S["original_diff_paths"]=original; S["upstream_delta_paths"]=upstream
    run("merge-attempt",["git","-c","merge.conflictstyle=merge","merge","--no-commit","--no-ff",MAIN],timeout=600)
    conflicts=[p.decode() for p in git("diff","--name-only","--diff-filter=U","-z").split(b"\0") if p]
    S["initial_conflicts"]=conflicts
    if PR=="214":
        allowed={"tests/README.md","tests/README_zh.md"}
        if set(conflicts)!=allowed: raise RuntimeError("PR214 conflict scope changed")
        for p in sorted(allowed): readme(p)
        git("add","--",*sorted(allowed))
    else:
        if not conflicts or any(not p.startswith("openai4s/server/webui/dist/") for p in conflicts): raise RuntimeError("Unexpected non-generated conflict")
        git("restore","--source="+MAIN,"--staged","--worktree","--","openai4s/server/webui/dist")
    run("frontend-install",["npm","ci","--prefix","frontend"],timeout=1200,fatal=True)
    run("frontend-build",["npm","run","build","--prefix","frontend"],timeout=900,fatal=True)
    if PR=="214" and git("diff","--","openai4s/server/webui/dist"): raise RuntimeError("PR214 unexpected dist drift")
    git("add","-A","--","openai4s/server/webui/dist")
    if git("ls-files","-u"): raise RuntimeError("Unmerged paths remain")
    staged=gt("diff","--cached","--name-only",MAIN).splitlines()
    if any(p not in original and not(PR!="214" and p.startswith("openai4s/server/webui/dist/")) for p in staged): raise RuntimeError("Unexpected delta path")
    for p in original:
        if p not in upstream and not p.startswith("openai4s/server/webui/dist/"):
            if git("rev-parse",":"+p).strip()!=git("rev-parse",SOURCE+":"+p).strip(): raise RuntimeError("Original source changed: "+p)
    run("diff-check",["git","diff","--cached","--check",MAIN],fatal=True)
    git("-c","commit.gpgsign=false","commit","-m","Validation-only merge for existing PR "+PR)
    commit=gt("rev-parse","HEAD")
    if gt("rev-list","--parents","-n","1",commit).split()[1:]!=[SOURCE,MAIN]: raise RuntimeError("Wrong parent order")
    S["expected_nonzero_merge_attempt_resolved"]=True
    export(commit); clean("prepare")
def browser():
    global DAEMON
    if not gate("browser-driver",["npm","ci","--ignore-scripts"],timeout=900): return False
    if not gate("browser-install",["./node_modules/.bin/playwright","install","--with-deps","chromium"],timeout=1200): return False
    e=E.copy(); e.update(OPENAI4S_DATA_DIR=str(ROOT/"browser-data"),OPENAI4S_KERNEL_SANDBOX="off",OPENAI4S_SECRET_STORE="plaintext",OPENAI4S_NOTEBOOK_REPL="1",OPENAI4S_HOST="127.0.0.1",OPENAI4S_PORT="8760")
    log=(ROOT/"daemon.log").open("wb")
    DAEMON=subprocess.Popen(["uv","run","openai4s","serve"],cwd=REPO,env=e,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    import urllib.request
    ready=False
    for _ in range(80):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8760/health",timeout=1) as r: ready=r.status==200
            if ready: break
        except Exception: pass
        if DAEMON.poll() is not None: break
        time.sleep(.25)
    if not ready: raise RuntimeError("Disposable loopback daemon not healthy; its log is not in public artifacts")
    ok=True
    try:
        for name in ["browser_smoke.mjs","browser_admission_fault.mjs","browser_p1_controls.mjs","browser_stage1_trusted_delivery.mjs","browser_sandbox_preview.mjs","browser_auto_mode_status.mjs"]:
            ok=gate(name[:-4],["node","tests/"+name],env=e,timeout=1200) and ok
        ok=gate("browser-matrix-chromium",["node","tests/browser_matrix.mjs","--browser=chromium"],env=e,timeout=1200) and ok
    finally: stop(DAEMON); log.close(); DAEMON=None
    return ok
def validate():
    ok=True
    for label,args in [
    ("frontend-unit",["npm","test","--prefix","frontend"]),
    ("frontend-typecheck",["npm","run","typecheck","--prefix","frontend"]),
    ("rebuild-repeat",["npm","run","build","--prefix","frontend"]),
    ("renderer-parser",["node","tests/scientific_renderers_smoke.cjs"])]:
        ok=gate(label,args) and ok
    if not gate("python-install",["uv","sync","--locked","--extra","science","--extra","chemistry"],timeout=1200): raise RuntimeError("Locked Python install failed")
    for label,args in [
    ("compileall",["uv","run","python","-m","compileall","-q","openai4s","openai4s_compute_provider","harness","tests"]),
    ("collect",["uv","run","pytest","--collect-only","-q"])]:
        ok=gate(label,args) and ok
    if PR=="214":
        ok=gate("focused-provenance",["uv","run","pytest","tests/test_provenance_groupby.py","tests/test_dataset_import.py","tests/test_store.py","tests/test_kernel.py","tests/test_project_history.py"]) and ok
    else:
        f="src/features/sessions/compute.test.ts" if PR=="215" else "src/features/execution/exec.test.ts"
        ok=gate("focused-frontend",["npm","test","--prefix","frontend","--",f]) and ok
    ok=gate("offline-full",["uv","run","pytest","-n","auto","--maxprocesses=4","--dist","loadfile"]) and ok
    ok=gate("science-import",["uv","run","python","-c","import matplotlib,pandas; print('science-import-ok')"]) and ok
    for label,args in [
    ("harness",["uv","run","python","-m","harness.cli","run","--tier","pr","--offline"]),
    ("response-contract",["uv","run","python","scripts/capture_response_contract.py","--check"]),
    ("response-schemas",["uv","run","python","scripts/capture_response_schemas.py","--check"]),
    ("mypy",["uv","run","mypy"]),
    ("docs",["uv","run","python","scripts/check_directory_readmes.py"]),
    ("css",["uv","run","python","scripts/check_css_tokens.py"]),
    ("source-scan",["python","scripts/source_secret_scan.py"])]:
        ok=gate(label,args) and ok
    ok=browser() and ok
    ok=gate("precommit",["uv","run","pre-commit","run","--all-files"],timeout=1200) and ok
    if gate("fuzz-install",["uv","sync","--locked","--extra","fuzz"],timeout=1200):
        (ROOT/"fuzz-corpus").mkdir(exist_ok=True)
        ok=gate("fuzz60",["uv","run","--locked","--extra","fuzz","python","scripts/protocol_fuzzer.py",str(ROOT/"fuzz-corpus"),"-max_total_time=60","-max_len=1048576"],timeout=300) and ok
    else: ok=False
    clean("final"); S["ready_for_root_review"]=ok; return ok
try:
    prepare(); success=validate()
except Exception as exc:
    S["failure"]=type(exc).__name__+": "+str(exc)
    S["driver_traceback"]=traceback.format_exc()[-65536:]
    success=False
finally:
    if DAEMON is not None: stop(DAEMON)
    S["final_free_bytes"]=free()
    S["artifact_bytes_before_final_summary"]=artifact_bytes()
    S["artifact_inventory"]=[{"path":str(p.relative_to(OUT)),"bytes":p.stat().st_size,"sha256":sha(p.read_bytes())} for p in sorted(OUT.rglob("*")) if p.is_file() and p.name!="summary.json"]
    save()
sys.exit(0 if success else 1)
