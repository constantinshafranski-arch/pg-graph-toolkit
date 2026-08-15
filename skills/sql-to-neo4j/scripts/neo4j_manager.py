#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
Bring up a local Neo4j Community container, reusing whatever already exists.

This implements the "don't rebuild if you don't have to" ladder the skill needs:

  1. Is a container named <name> already RUNNING and healthy? -> reuse it.
  2. Does a STOPPED container named <name> exist?            -> start it.
  3. Is the neo4j:<tag> image already pulled locally?         -> run a new container.
  4. Otherwise                                                -> pull the image, then run.

("Build the image" for Neo4j means pulling the official Community image -- there
is no Dockerfile to compile. If the project later needs a custom image with
plugins baked in, that's a Dockerfile in the project; the pull path covers the
default community engine the user asked for.)

On success it writes the connection details to an env file (default .neo4j.env in
the project dir) so the loader and the user share one source of truth, and prints
the Neo4j Browser URL.

Usage:
  neo4j_manager.py up   [--name graph-neo4j] [--password <pw>] [--env-file .neo4j.env]
  neo4j_manager.py status [--name graph-neo4j]
  neo4j_manager.py down [--name graph-neo4j] [--force]   # stop (keeps data volume);
                                                         # --force overrides the ownership guard
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

DEFAULT_NAME = "graph-neo4j"
DEFAULT_TAG = "5-community"
DEFAULT_HTTP = 7474
DEFAULT_BOLT = 7687
DEFAULT_USER = "neo4j"
MANAGED_LABEL = "managed-by"
MANAGED_VALUE = "pg-graph-toolkit"
PORT_FALLBACK_TRIES = 10


def run(cmd, check=True, capture=True):
    return subprocess.run(
        cmd,
        check=check,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        text=True,
    )


def docker_ok():
    try:
        run(["docker", "info"])
        return True
    except Exception:
        return False


def container_state(name):
    """Return 'running', 'stopped', or None."""
    r = run(["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.State}}"], check=False)
    out = (r.stdout or "").strip()
    if not out:
        return None
    return "running" if out.startswith("running") else "stopped"


def image_present(tag):
    r = run(["docker", "images", "-q", f"neo4j:{tag}"], check=False)
    return bool((r.stdout or "").strip())


def container_env(name):
    """Read back host ports + password from a running/existing container."""
    r = run(["docker", "inspect", name], check=False)
    if r.returncode != 0:
        return {}
    data = json.loads(r.stdout)[0]
    info = {}
    env = data.get("Config", {}).get("Env", []) or []
    for e in env:
        if e.startswith("NEO4J_AUTH="):
            auth = e.split("=", 1)[1]
            if "/" in auth:
                info["user"], info["password"] = auth.split("/", 1)
    ports = data.get("NetworkSettings", {}).get("Ports", {}) or {}
    for cport, hostbinds in ports.items():
        if hostbinds:
            hp = hostbinds[0]["HostPort"]
            if cport.startswith(str(DEFAULT_HTTP)):
                info["http_port"] = hp
            elif cport.startswith(str(DEFAULT_BOLT)):
                info["bolt_port"] = hp
    return info


def container_label_owner(name):
    """Return the container's managed-by label value, or None.

    Uses a stderr-separated call (unlike run()) so a docker warning on stderr
    can never be mistaken for a label value.
    """
    r = subprocess.run(
        ["docker", "inspect", "--format", f'{{{{index .Config.Labels "{MANAGED_LABEL}"}}}}', name],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        return None
    return (r.stdout or "").strip() or None


def port_free(port):
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def pick_ports(http_port, bolt_port, user_specified):
    """Return free (http, bolt); auto-advance from the defaults if busy."""
    if port_free(http_port) and port_free(bolt_port):
        return http_port, bolt_port
    if user_specified:
        sys.exit(
            f"Requested port(s) busy (http {http_port} free={port_free(http_port)}, "
            f"bolt {bolt_port} free={port_free(bolt_port)}). Pick others or stop what holds them."
        )
    for i in range(1, PORT_FALLBACK_TRIES + 1):
        h, b = http_port + i, bolt_port + i
        if port_free(h) and port_free(b):
            print(f"Ports {http_port}/{bolt_port} busy — using {h}/{b} instead.")
            return h, b
    sys.exit(f"No free port pair found in {http_port}-{http_port + PORT_FALLBACK_TRIES}.")


def wait_healthy(http_port, timeout=90):
    url = f"http://localhost:{http_port}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def write_env_file(path, info):
    lines = [
        f"NEO4J_URI=bolt://localhost:{info['bolt_port']}",
        f"NEO4J_HTTP=http://localhost:{info['http_port']}",
        f"NEO4J_USER={info['user']}",
        f"NEO4J_PASSWORD={info['password']}",
        f"NEO4J_CONTAINER={info['name']}",
    ]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    # keep secrets out of git if this is a repo
    try:
        gi = os.path.join(os.path.dirname(os.path.abspath(path)) or ".", ".gitignore")
        base = os.path.basename(path)
        existing = ""
        if os.path.exists(gi):
            existing = open(gi).read()
        if base not in existing:
            with open(gi, "a") as f:
                f.write(("" if existing.endswith("\n") or not existing else "\n") + base + "\n")
    except Exception:
        pass


def build_run_cmd(name, tag, http_port, bolt_port, password, with_gds=False, no_plugins=False):
    """docker run argv for a new container. APOC Core ships by default
    (NEO4J_PLUGINS makes the official image fetch it at first start);
    --with-gds adds the free Graph Data Science Community library.
    Plugins are baked in at container creation only — an existing container
    keeps whatever it started with."""
    plugins = [] if no_plugins else ["apoc"] + (["graph-data-science"] if with_gds else [])
    if no_plugins:
        return [
            "docker", "run", "-d", "--name", name,
            "--label", f"{MANAGED_LABEL}={MANAGED_VALUE}",
            "-p", f"{http_port}:7474", "-p", f"{bolt_port}:7687",
            "-e", f"NEO4J_AUTH={DEFAULT_USER}/{password}",
            "-e", "NEO4J_server_memory_heap_max__size=1G",
            "-v", f"{name}-data:/data", f"neo4j:{tag}",
        ]
    return [
        "docker", "run", "-d",
        "--name", name,
        "--label", f"{MANAGED_LABEL}={MANAGED_VALUE}",
        "-p", f"{http_port}:7474",
        "-p", f"{bolt_port}:7687",
        "-e", f"NEO4J_AUTH={DEFAULT_USER}/{password}",
        "-e", "NEO4J_server_memory_heap_max__size=1G",
        "-e", f"NEO4J_PLUGINS={json.dumps(plugins)}",
        "-v", f"{name}-data:/data",
        f"neo4j:{tag}",
    ]


def cmd_up(args):
    if not docker_ok():
        sys.exit("Docker daemon is not reachable. Start Docker and retry.")

    # None defaults distinguish "user explicitly asked for this port" (fail
    # fast if busy — even if they asked for the default) from "tool default"
    # (auto-advance to the next free pair).
    ports_user_specified = args.http_port is not None or args.bolt_port is not None
    http_req = args.http_port if args.http_port is not None else DEFAULT_HTTP
    bolt_req = args.bolt_port if args.bolt_port is not None else DEFAULT_BOLT

    name = args.name
    state = container_state(name)

    if state == "running":
        info = container_env(name)
        info["name"] = name
        info.setdefault("user", DEFAULT_USER)
        print(f"Reusing running container '{name}'.")
    elif state == "stopped":
        print(f"Starting existing stopped container '{name}'...")
        run(["docker", "start", name], capture=False)
        info = container_env(name)
        info["name"] = name
        info.setdefault("user", DEFAULT_USER)
    else:
        if not image_present(args.tag):
            print(f"Image neo4j:{args.tag} not found locally. Pulling...")
            run(["docker", "pull", f"neo4j:{args.tag}"], capture=False)
        else:
            print(f"Image neo4j:{args.tag} already present.")
        http_port, bolt_port = pick_ports(http_req, bolt_req, ports_user_specified)
        print(f"Creating and starting container '{name}'...")
        run(build_run_cmd(name, args.tag, http_port, bolt_port, args.password, args.with_gds, args.no_plugins), capture=False)
        info = {
            "name": name,
            "user": DEFAULT_USER,
            "password": args.password,
            "http_port": str(http_port),
            "bolt_port": str(bolt_port),
        }

    # make sure ports resolved (reuse paths read the real ports from docker
    # inspect; this fallback only fires if inspection came back empty)
    info.setdefault("http_port", str(http_req))
    info.setdefault("bolt_port", str(bolt_req))
    if "password" not in info:
        info["password"] = args.password

    print("Waiting for Neo4j to accept connections...")
    if not wait_healthy(info["http_port"]):
        sys.exit("Neo4j did not become healthy in time. Check: docker logs " + name)

    write_env_file(args.env_file, info)
    print("\nNeo4j is up.")
    print(f"  Browser : http://localhost:{info['http_port']}")
    print(f"  Bolt    : bolt://localhost:{info['bolt_port']}")
    print(f"  User    : {info['user']}")
    print(f"  Env file: {args.env_file}")


def cmd_status(args):
    state = container_state(args.name)
    if state is None:
        print(f"No container named '{args.name}'.")
        return
    info = container_env(args.name)
    print(f"Container '{args.name}': {state}")
    if info.get("http_port"):
        print(f"  Browser: http://localhost:{info['http_port']}")


def cmd_down(args):
    if container_state(args.name) != "running":
        print(f"Container '{args.name}' is not running.")
        return
    owner = container_label_owner(args.name)
    if owner is not None and owner != MANAGED_VALUE and not args.force:
        sys.exit(
            f"Container '{args.name}' is labeled {MANAGED_LABEL}={owner} — it belongs to "
            f"another tool. Refusing to stop it (pass --force to override)."
        )
    if owner is None:
        # Pre-label containers created by earlier versions of this tool have no
        # label; stopping by exact name is still safe, but say what we saw.
        print(f"Note: '{args.name}' has no {MANAGED_LABEL} label (created before v0.2 or externally).")
    run(["docker", "stop", args.name], capture=False)
    print(f"Stopped '{args.name}' (data volume preserved).")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--name", default=DEFAULT_NAME)
    common.add_argument("--tag", default=DEFAULT_TAG)

    up = sub.add_parser("up", parents=[common])
    up.add_argument("--password", default="neo4jpassword")
    up.add_argument("--http-port", type=int, default=None, help=f"default {DEFAULT_HTTP}; explicit values fail fast if busy")
    up.add_argument("--bolt-port", type=int, default=None, help=f"default {DEFAULT_BOLT}; explicit values fail fast if busy")
    up.add_argument("--env-file", default=".neo4j.env")
    up.add_argument(
        "--no-plugins",
        action="store_true",
        help="skip NEO4J_PLUGINS entirely (offline machines: plugin jars are fetched at first container start)",
    )
    up.add_argument(
        "--with-gds",
        action="store_true",
        help="include the free Graph Data Science Community library (new containers only)",
    )
    up.set_defaults(func=cmd_up)

    st = sub.add_parser("status", parents=[common])
    st.set_defaults(func=cmd_status)

    dn = sub.add_parser("down", parents=[common])
    dn.add_argument("--force", action="store_true", help="stop even if the container belongs to another tool")
    dn.set_defaults(func=cmd_down)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
