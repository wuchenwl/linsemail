"""Manage only this checkout's Windows console processes, with detached file handles."""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
RUNTIME=Path(os.environ.get("LINSEMAIL_RUNTIME",str(ROOT/".runtime"))).resolve()
RUNTIME.mkdir(parents=True,exist_ok=True)
MANIFEST=RUNTIME/"services.local.json"
PYTHON=ROOT/".venv/Scripts/python.exe"
NODE=shutil.which("node")
API_PORT=int(os.environ.get("LINSEMAIL_PORT","8100"))
SMTP_PORT=int(os.environ.get("LINSEMAIL_SMTP_PORT","1025"))
FLAGS=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0
services=json.loads(MANIFEST.read_text(encoding="utf-8-sig")) if MANIFEST.exists() else {}


def run(command,timeout=20):
    return subprocess.run(command,cwd=ROOT,capture_output=True,encoding="utf-8",errors="replace",
                          timeout=timeout,creationflags=FLAGS,stdin=subprocess.DEVNULL)


def process_info(pid):
    query="Get-CimInstance Win32_Process -Filter 'ProcessId = "+str(int(pid))+"' | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    result=run(["powershell.exe","-NoProfile","-NonInteractive","-Command",query])
    return json.loads(result.stdout) if result.returncode==0 and result.stdout.strip() else None


def owned(name,pid):
    info=process_info(pid)
    if not info:return False
    command=(info.get("CommandLine") or "").replace("\\","/").casefold()
    expected="tools/postgres/server.mjs" if name=="postgres" else "backend."+name
    return str(ROOT).replace("\\","/").casefold() in command and expected in command


def connected(port):
    try:
        with socket.create_connection(("127.0.0.1",port),timeout=.3):return True
    except OSError:return False


def wait_for(port):
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        if connected(port):return
        time.sleep(.15)
    raise RuntimeError("Service not ready on port "+str(port)+"; inspect .runtime/*.log")


def start(name,command,port):
    pid=services.get(name)
    if pid and owned(name,pid):
        wait_for(port);return
    if name!="postgres" and connected(port):
        raise RuntimeError("Port "+str(port)+" is occupied by an unmanaged process.")
    with (RUNTIME/(name+".out.log")).open("w",encoding="utf-8") as stdout, (RUNTIME/(name+".err.log")).open("w",encoding="utf-8") as stderr:
        process=subprocess.Popen(command,cwd=ROOT,stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,
                                 close_fds=True,creationflags=FLAGS|subprocess.CREATE_NEW_PROCESS_GROUP)
    services[name]=process.pid
    MANIFEST.write_text(json.dumps(services,indent=2),encoding="utf-8")
    wait_for(port)
    if process.poll() is not None:
        raise RuntimeError(name+" exited during startup; inspect .runtime/*.log")


def main(mode):
    if not NODE:raise RuntimeError("Node.js is required.")
    if mode=="stop":
        for name in ("worker","api","postgres"):
            pid=services.get(name)
            if pid and owned(name,pid):
                result=run(["taskkill","/PID",str(int(pid)),"/T","/F"])
                if result.returncode and process_info(pid):
                    raise RuntimeError("Could not stop managed "+name+" process.")
        result=run([NODE,str(ROOT/"tools/postgres/stop.mjs")],timeout=50)
        if result.returncode:raise RuntimeError("PostgreSQL shutdown failed: "+result.stderr)
        print("Linsemail stopped. Persistent data remains in .runtime/.")
        return
    start("postgres",[NODE,str(ROOT/"tools/postgres/server.mjs")],55432)
    result=run([str(PYTHON),"-c","from backend.db import Database; from backend.config import Settings; db=Database(Settings.load()); db.initialize(); db.close()"])
    if result.returncode:raise RuntimeError("Database initialization failed; inspect database configuration.")
    start("api",[str(PYTHON),"-m","backend.api"],API_PORT)
    start("worker",[str(PYTHON),"-m","backend.worker"],SMTP_PORT)
    import httpx
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        try:
            response=httpx.get("http://127.0.0.1:"+str(API_PORT)+"/api/v1/health",timeout=2)
            if response.is_success and response.json()["worker_online"]:break
        except httpx.HTTPError:pass
        time.sleep(.2)
    else:raise RuntimeError("Worker heartbeat not ready; inspect .runtime/worker.err.log")
    print("Console: http://127.0.0.1:"+str(API_PORT)+" | database=postgresql")
    print("API docs: http://127.0.0.1:"+str(API_PORT)+"/api/docs")
    print("Stop: .\\scripts\\stop-console.ps1")


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode",choices=["start","stop"])
    try:main(parser.parse_args().mode)
    except Exception as error:
        print(type(error).__name__+": "+str(error),file=sys.stderr);raise SystemExit(1)
