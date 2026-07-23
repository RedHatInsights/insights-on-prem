#!/usr/bin/env python3
"""Load generator for insights-on-prem memory leak reproduction.

Uploads archives to the monolithic FastAPI app to stress-test the
insights-core processing pipeline (dr.run_components / broker).

Default uses self-contained archive generators. Pass --use-molodec for
realistic OCP archives via the molodec CLI.

Usage:
    python send_archives.py --duration 60 --bad-ratio 0.3
    python send_archives.py --use-molodec --duration 60
    python send_archives.py --parallel 5 --duration 30
"""

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

UPLOAD_URL = "http://localhost:8000/api/ingress/v1/upload"

VERSION_JSON = json.dumps({
    "kind": "ClusterVersion",
    "metadata": {"name": "version"},
    "spec": {"clusterID": "PLACEHOLDER"},
    "status": {
        "desired": {"version": "4.17.0"},
        "history": [{"state": "Completed", "version": "4.17.0", "verified": False}],
        "conditions": [
            {"type": "Available", "status": "True", "message": "Done applying 4.17.0"}
        ],
    },
})


def make_valid_archive(cluster_id, tmpdir):
    """Create a minimal valid OCP tar archive on disk."""
    version_data = VERSION_JSON.replace("PLACEHOLDER", cluster_id)

    files = {
        "config/id": cluster_id,
        "config/version": version_data,
        "config/infrastructure": json.dumps({
            "apiVersion": "config.openshift.io/v1",
            "kind": "Infrastructure",
            "metadata": {"name": "cluster"},
            "status": {
                "apiServerURL": "https://api.test.example.com:6443",
                "platform": "AWS",
                "infrastructureName": "test-cluster-abc123",
            },
        }),
        "config/network": json.dumps({
            "apiVersion": "config.openshift.io/v1",
            "kind": "Network",
            "metadata": {"name": "cluster"},
            "spec": {
                "clusterNetwork": [{"cidr": "10.128.0.0/14", "hostPrefix": 23}],
                "serviceNetwork": ["172.30.0.0/16"],
                "networkType": "OVNKubernetes",
            },
        }),
        "config/image.json": json.dumps({
            "apiVersion": "config.openshift.io/v1",
            "kind": "Image",
            "metadata": {"name": "cluster"},
            "spec": {},
        }),
        "config/node/worker-0": json.dumps({
            "apiVersion": "v1",
            "kind": "Node",
            "metadata": {
                "name": "worker-0",
                "labels": {"node-role.kubernetes.io/worker": ""},
            },
            "status": {
                "conditions": [{"type": "Ready", "status": "True"}],
                "capacity": {"cpu": "4", "memory": "16Gi"},
            },
        }),
    }

    path = os.path.join(tmpdir, f"{cluster_id}.tar")
    with tarfile.open(path, mode="w") as tar:
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, BytesIO(data))
    return path


def make_bad_archive(cluster_id, tmpdir):
    """Create a tar archive with corrupted JSON that triggers exceptions."""
    version_data = VERSION_JSON.replace("PLACEHOLDER", cluster_id)

    bad_files = {
        "config/id": cluster_id,
        "config/version": version_data,
        "config/infrastructure": '{"metadata":{"name":"cluster"},"status":{"broken',
        "config/node/bad-node-1": '{"apiVersion":"v1","kind":"Node","metadata":',
        "config/node/bad-node-2": '{truncated',
        "config/clusteroperator/bad-co-1": '{"apiVersion":"config.openshift.io/v1"',
        "config/pod/bad-ns/bad-pod-1": '{"kind":"Pod","broken',
        "config/machineconfigpools/bad-mcp": '{"apiVersion":"machineconfiguration',
        "config/machines/openshift-machine-api/bad-m1": '{"corrupted',
        "config/image.json": '{not valid json at all',
        "config/network": '{"metadata":{"name":"cluster"},"spec":',
        "config/olm_operators.json": '[{"name":',
        "config/metrics": 'not_prometheus_format{broken',
        "config/install_plans": '{"items":[{"broken',
        "config/persistentvolumes/bad-pv": '{"metadata":{"name":',
        "config/certificatesigningrequests/bad-csr": '{"TypeMeta',
        "config/cost_management_metrics_configs/bad.json": '{"apiVersion":',
        "config/namespaces_with_overlapping_uids.json": '[["broken',
    }

    path = os.path.join(tmpdir, f"{cluster_id}.tar")
    with tarfile.open(path, mode="w") as tar:
        for name, content in bad_files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, BytesIO(data))
    return path


def make_molodec_archive(cluster_id, tmpdir):
    """Create a realistic OCP archive using the molodec CLI."""
    path = os.path.join(tmpdir, f"{cluster_id}.tar")
    result = subprocess.run(
        ["molodec", "archive", "generate", "-c", cluster_id, path],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"molodec failed: {result.stderr}")
    return path


def upload_archive(url, file_path):
    """Upload archive file via curl (reliable multipart)."""
    result = subprocess.run(
        [
            "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
            "-X", "POST", url,
            "-F", f"file=@{file_path}",
        ],
        capture_output=True, text=True, timeout=30,
    )
    return int(result.stdout.strip())


def run_continuous(args, make_archive_fn):
    """Send archives continuously with parallel workers."""
    duration_sec = args.duration * 60
    workers = args.parallel

    print(f"\n{'='*60}")
    print(f"CONTINUOUS MODE: {args.duration} min, {workers} workers")
    print(f"Bad archive ratio: {args.bad_ratio:.0%}")
    print(f"Target: {args.url}")
    print(f"{'='*60}\n")

    start = time.time()
    lock = threading.Lock()
    counters = {"sent": 0, "bad": 0}

    def worker(worker_id):
        tmpdir = tempfile.mkdtemp(prefix=f"send_archives_w{worker_id}_")
        try:
            while (time.time() - start) < duration_sec:
                cluster_id = str(uuid.uuid4())
                is_bad = args.bad_ratio > 0 and random.random() < args.bad_ratio
                path = None

                try:
                    if is_bad:
                        path = make_bad_archive(cluster_id, tmpdir)
                    else:
                        path = make_archive_fn(cluster_id, tmpdir)

                    status = upload_archive(args.url, path)
                except Exception as e:
                    print(f"  [W{worker_id}] Upload failed: {e}")
                    time.sleep(1)
                    continue
                finally:
                    if path:
                        try:
                            os.remove(path)
                        except OSError:
                            pass

                with lock:
                    counters["sent"] += 1
                    if is_bad:
                        counters["bad"] += 1
                    total = counters["sent"]
                    bad = counters["bad"]
                    should_print = (total % 100 == 0)

                if should_print:
                    elapsed_min = (time.time() - start) / 60
                    print(
                        f"[{elapsed_min:.1f}min] Sent {total} ({bad} bad) "
                        f"(Status: {status})"
                    )

                time.sleep(args.delay)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker, i) for i in range(workers)]
        for f in futures:
            f.result()

    print(f"\n{'='*60}")
    print(
        f"COMPLETE — {counters['sent']} archives ({counters['bad']} bad) "
        f"in {args.duration} min ({workers} workers)"
    )
    print(f"{'='*60}\n")


def run_burst(args, make_archive_fn):
    """Send archives in burst/break cycles with parallel workers."""
    burst_sec = 10 * 60
    break_sec = 1 * 60
    num_cycles = max(1, int(args.duration / 11))
    workers = args.parallel

    print(f"\n{'='*60}")
    print(f"BURST MODE: {num_cycles} cycles, {workers} workers")
    print(f"Bad archive ratio: {args.bad_ratio:.0%}")
    print(f"Target: {args.url}")
    print(f"{'='*60}\n")

    total_sent = 0
    total_bad = 0

    for cycle in range(num_cycles):
        print(f"\n--- Cycle {cycle + 1}/{num_cycles} — SENDING ---")
        burst_start = time.time()
        lock = threading.Lock()
        counters = {"sent": 0, "bad": 0}

        def burst_worker(worker_id):
            tmpdir = tempfile.mkdtemp(prefix=f"send_archives_w{worker_id}_")
            try:
                while (time.time() - burst_start) < burst_sec:
                    cluster_id = str(uuid.uuid4())
                    is_bad = args.bad_ratio > 0 and random.random() < args.bad_ratio
                    path = None

                    try:
                        if is_bad:
                            path = make_bad_archive(cluster_id, tmpdir)
                        else:
                            path = make_archive_fn(cluster_id, tmpdir)

                        status = upload_archive(args.url, path)
                    except Exception as e:
                        print(f"  [W{worker_id}] Upload failed: {e}")
                        time.sleep(1)
                        continue
                    finally:
                        if path:
                            try:
                                os.remove(path)
                            except OSError:
                                pass

                    with lock:
                        counters["sent"] += 1
                        if is_bad:
                            counters["bad"] += 1
                        total = counters["sent"]
                        bad = counters["bad"]
                        should_print = (total % 100 == 0)

                    if should_print:
                        elapsed = time.time() - burst_start
                        print(
                            f"  [Cycle {cycle+1}] Sent {total} ({bad} bad) "
                            f"in {elapsed:.0f}s (Status: {status})"
                        )

                    time.sleep(args.delay)
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(burst_worker, i) for i in range(workers)]
            for f in futures:
                f.result()

        total_sent += counters["sent"]
        total_bad += counters["bad"]
        print(f"  Cycle {cycle+1} done: {counters['sent']} archives ({counters['bad']} bad)")

        if cycle < num_cycles - 1:
            print(f"  BREAK — {break_sec}s (watch for memory release)")
            time.sleep(break_sec)

    print(f"\n{'='*60}")
    print(
        f"COMPLETE — {total_sent} archives ({total_bad} bad) "
        f"over {num_cycles} cycles ({workers} workers)"
    )
    print(f"{'='*60}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Load generator for insights-on-prem memory leak reproduction"
    )
    parser.add_argument(
        "--duration", type=int, default=60,
        help="Duration in minutes (default: 60)",
    )
    parser.add_argument(
        "--delay", type=float, default=0,
        help="Seconds between uploads per worker (default: 0)",
    )
    parser.add_argument(
        "--bad-ratio", type=float, default=0.0,
        help="Fraction of bad archives 0.0-1.0 (default: 0.0)",
    )
    parser.add_argument(
        "--parallel", type=int, default=10,
        help="Number of parallel upload workers (default: 3)",
    )
    parser.add_argument(
        "--url", default=UPLOAD_URL,
        help=f"Upload endpoint URL (default: {UPLOAD_URL})",
    )
    parser.add_argument(
        "--burst", action="store_true",
        help="Use burst mode (10min send + 1min break cycles)",
    )
    parser.add_argument(
        "--use-molodec", action="store_true", default=True,
        help="Use molodec for realistic OCP archives (default: on)",
    )
    parser.add_argument(
        "--no-molodec", action="store_true",
        help="Use self-contained archives instead of molodec",
    )
    args = parser.parse_args()

    if args.no_molodec:
        args.use_molodec = False

    if args.use_molodec:
        if shutil.which("molodec") is None:
            print(
                "ERROR: --use-molodec requires molodec CLI.\n"
                "Run: ./scripts/setup_venv.sh",
                file=sys.stderr,
            )
            sys.exit(1)
        make_fn = make_molodec_archive
        print("Using molodec for archive generation")
    else:
        make_fn = make_valid_archive
        print("Using self-contained archive generator")

    if args.burst:
        run_burst(args, make_fn)
    else:
        run_continuous(args, make_fn)


if __name__ == "__main__":
    main()
