"""Installation-scoped OAS lifecycle. Never discover or kill by process name."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import uuid
from urllib.request import ProxyHandler, Request, build_opener

import psutil
from filelock import FileLock, Timeout


class InstanceError(RuntimeError):
    pass


def installation_root(root=None) -> Path:
    return Path(root or Path(__file__).resolve().parent.parent).resolve()


def instance_id(root=None) -> str:
    identity = os.path.normcase(str(installation_root(root)))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def runtime_path(root=None) -> Path:
    return installation_root(root) / "log" / "runtime" / "instance.json"


def instance_lock(root=None) -> FileLock:
    path = runtime_path(root).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    return FileLock(str(path), timeout=0)


def read_record(root=None) -> dict | None:
    try:
        data = json.loads(runtime_path(root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (ValueError, OSError) as exc:
        raise InstanceError(f"Cannot read instance record: {exc}") from exc
    if not isinstance(data, dict):
        raise InstanceError("Invalid instance record")
    return data


def verified_process(record: dict, root=None):
    """Check creation time, executable, working directory and server entry point."""
    target = installation_root(root)
    if record.get("instance_id") != instance_id(target) or record.get("root") != str(target):
        raise InstanceError("Instance record belongs to a different installation; refusing to stop it")
    try:
        process = psutil.Process(int(record["pid"]))
        if abs(process.create_time() - float(record["create_time"])) > 0.01:
            return None  # PID has been reused.
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return None
        if Path(process.exe()).resolve() != Path(record["executable"]).resolve():
            raise InstanceError("Process executable does not match the instance record")
        if Path(process.cwd()).resolve() != target:
            raise InstanceError("Process working directory does not match the installation")
        command = process.cmdline()
        entry = Path(command[1]) if len(command) > 1 else Path("")
        if not entry.is_absolute():
            entry = target / entry
        if entry.resolve() != target / "server.py":
            raise InstanceError("Process is not this installation's server.py")
        return process
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return None
    except (KeyError, TypeError, ValueError, psutil.AccessDenied) as exc:
        raise InstanceError(f"Cannot verify instance process: {exc}") from exc


def service_ports(root=None, web_port=None) -> list[tuple[str, int]]:
    from deploy.utils import poor_yaml_read

    config = poor_yaml_read(str(installation_root(root) / "config" / "deploy.yaml"))
    ports = [("API", int(web_port or config.get("WebuiPort", 22267)))]
    for name, default in (("Ocr", 22268), ("Image", 22269)):
        if config.get(f"Start{name}Server", True):
            port = int(config.get(f"{name}ServerPort") or default)
            address = str(config.get(f"{name}ClientAddress") or f"127.0.0.1:{port}")
            host, separator, client_port = address.removeprefix("tcp://").rpartition(":")
            if not separator or host not in {"127.0.0.1", "localhost"} or client_port != str(port):
                raise InstanceError(f"Managed {name} server and client address must use the same local port")
            ports.append((name, port))
    if any(not 1 <= port <= 65535 for _, port in ports):
        raise InstanceError("Service port must be between 1 and 65535")
    if len({port for _, port in ports}) != len(ports):
        raise InstanceError("API, OCR and image services must use different ports")
    return ports


def check_ports(root=None, web_port=None) -> None:
    sockets = []
    try:
        for name, port in service_ports(root, web_port):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sockets.append(sock)
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                sock.bind(("0.0.0.0", port))
            except OSError as exc:
                raise InstanceError(f"{name} port {port} is occupied. No process was stopped; configure independent ports.") from exc
    finally:
        for sock in sockets:
            sock.close()


def owned_children(parent, root):
    """Track Python workers only; console hosts and shared ADB are not owned services."""
    children = []
    executable = Path(parent.exe()).resolve()
    for child in parent.children(recursive=True):
        try:
            if Path(child.exe()).resolve() == executable and Path(child.cwd()).resolve() == installation_root(root):
                children.append(child)
        except psutil.NoSuchProcess:
            pass
    return children


class InstanceRuntime:
    def __init__(self, root=None, *, host="127.0.0.1", port=None):
        self.root = installation_root(root)
        self.host = host
        self.port = port or service_ports(self.root)[0][1]
        self.lock = instance_lock(self.root)
        self.record = None
        self.port_locks = []

    def __enter__(self):
        try:
            self.lock.acquire()
        except Timeout as exc:
            raise InstanceError(f"OAS is already running in {self.root}") from exc
        try:
            lease_directory = Path(tempfile.gettempdir()) / "oas-service-leases"
            lease_directory.mkdir(parents=True, exist_ok=True)
            for name, port in sorted(service_ports(self.root, self.port), key=lambda item: item[1]):
                lease = FileLock(str(lease_directory / f"tcp-{port}.lock"), timeout=0)
                try:
                    lease.acquire()
                except Timeout as exc:
                    raise InstanceError(f"{name} port {port} is reserved by another OAS instance") from exc
                self.port_locks.append(lease)
            check_ports(self.root, self.port)
            process = psutil.Process()
            self.record = {
                "instance_id": instance_id(self.root),
                "run_id": uuid.uuid4().hex,
                "root": str(self.root),
                "pid": process.pid,
                "create_time": process.create_time(),
                "executable": process.exe(),
                "host": self.host,
                "port": self.port,
            }
            path = runtime_path(self.root)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.record, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, path)
            return self
        except BaseException:
            for lease in reversed(self.port_locks):
                lease.release()
            self.lock.release()
            raise

    def update_children(self):
        # Preserve verifiable child identities so a crashed parent leaves no orphan services.
        children = []
        for child in owned_children(psutil.Process(), self.root):
            try:
                children.append({"pid": child.pid, "create_time": child.create_time(),
                                 "executable": child.exe(), "cwd": child.cwd(),
                                 "command": child.cmdline()})
            except psutil.NoSuchProcess:
                pass
        self.record["children"] = children
        path = runtime_path(self.root)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    def __exit__(self, *_):
        try:
            current = read_record(self.root)
            if current and current.get("run_id") == self.record["run_id"]:
                runtime_path(self.root).unlink(missing_ok=True)
        finally:
            for lease in reversed(self.port_locks):
                lease.release()
            self.lock.release()


def verified_children(record, root):
    children = []
    for identity in record.get("children", []):
        try:
            if Path(identity["cwd"]).resolve() != installation_root(root):
                continue
            child = psutil.Process(int(identity["pid"]))
            if abs(child.create_time() - float(identity["create_time"])) > 0.01:
                continue
            if (Path(child.exe()).resolve() != Path(identity["executable"]).resolve()
                    or Path(child.cwd()).resolve() != installation_root(root)
                    or child.cmdline() != identity["command"]):
                raise InstanceError("Recorded child process identity changed; refusing to stop it")
            children.append(child)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except (KeyError, TypeError, ValueError, psutil.AccessDenied) as exc:
            raise InstanceError(f"Cannot verify child process: {exc}") from exc
    return children


def stop_instance(root=None, *, timeout=10.0) -> bool:
    """Stop only a verified server and its captured children, including stale runs."""
    target = installation_root(root)
    record = read_record(target)
    if not record:
        return False
    process = verified_process(record, target)
    children = verified_children(record, target)
    if process is None:
        return _stop_children(children)
    children.extend(owned_children(process, target))
    host = record.get("host", "127.0.0.1")
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    opener = build_opener(ProxyHandler({}))
    body = json.dumps({key: record[key] for key in ("instance_id", "run_id")}).encode()
    try:
        request = Request(f"http://{host}:{int(record['port'])}/home/stop_instance", body,
                          {"Content-Type": "application/json"}, method="POST")
        with opener.open(request, timeout=2) as response:
            response.read()
        process.wait(timeout=timeout)
    except (OSError, psutil.TimeoutExpired):
        # A crashed HTTP service can still be stopped after rechecking identity.
        process = verified_process(record, target)
        if process is not None:
            children.extend(owned_children(process, target))
            process.terminate()
            try:
                process.wait(timeout=3)
            except psutil.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
    _stop_children(children)
    return True


def _stop_children(children):
    children = list({child.pid: child for child in children}.values())
    for child in children:
        try:
            child.terminate()  # psutil checks creation time before sending signals.
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(children, timeout=2)
    for child in alive:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(children, timeout=2)
    if alive:
        raise InstanceError("Some instance child processes have not exited")
    return bool(children)


def prepare_instance(root=None) -> None:
    target = installation_root(root)
    record = read_record(target)
    # A full directory copy may include the source installation's live record.
    # It is not ours: leave that process and its record untouched. A successful
    # startup writes fresh metadata only in the destination directory.
    if not record or (record.get("instance_id") == instance_id(target)
                      and record.get("root") == str(target)):
        stop_instance(target)
    try:
        with instance_lock(target):
            check_ports(target)
    except Timeout as exc:
        raise InstanceError("Another process is starting this installation") from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "check", "prepare", "stop"))
    args = parser.parse_args()
    try:
        if args.action == "status":
            record = read_record()
            running = bool(record and verified_process(record))
            print(json.dumps({"running": running, "instance": record}, ensure_ascii=False))
        elif args.action == "check":
            check_ports()
            print("Instance ports are available")
        elif args.action == "prepare":
            prepare_instance()
            print("Installation is ready; other OAS instances were not stopped")
        else:
            print("Stopped this installation" if stop_instance() else "This installation is not running")
    except (InstanceError, OSError, psutil.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
