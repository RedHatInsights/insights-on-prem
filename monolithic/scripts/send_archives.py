#!/usr/bin/env python3
"""Load generator for insights-on-prem memory leak reproduction.

Uploads archives to the monolithic FastAPI app to stress-test the
insights-core processing pipeline (dr.run_components / broker).

Default mode uses self-contained archive generators (no external deps).
Pass --use-molodec to use molodec for realistic OCP archives.

Usage:
    python send_archives.py --duration 60 --delay 0.5 --bad-ratio 0.3
    python send_archives.py --duration 120 --burst
    python send_archives.py --use-molodec --duration 60
"""

import argparse
import json
import random
import sys
import tarfile
import time
import uuid
from io import BytesIO
from urllib.error import URLError
from urllib.request import Request, urlopen

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


def make_valid_archive(cluster_id):
    """Create a minimal valid OCP archive that insights-core can process."""
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
                "conditions": [
                    {"type": "Ready", "status": "True"},
                ],
                "capacity": {"cpu": "4", "memory": "16Gi"},
            },
        }),
    }

    tario = BytesIO()
    with tarfile.open(fileobj=tario, mode="w") as tar:
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, BytesIO(data))
    tario.seek(0)
    return tario


def make_bad_archive(cluster_id):
    """Create a tar archive with corrupted JSON that triggers exceptions.

    Exercises the broker.add_exception() code path where the traceback
    circular reference leak occurs.
    """
    version_data = VERSION_JSON.replace("PLACEHOLDER", cluster_id)

    tario = BytesIO()
    with tarfile.open(fileobj=tario, mode="w") as tar:
        files = {
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
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, BytesIO(data))
    tario.seek(0)
    return tario


def make_molodec_archive(cluster_id):
    """Create a realistic OCP archive using molodec."""
    from molodec.archive_producer import ArchiveProducer
    from molodec.renderer import Renderer
    from molodec.rules import RuleSet

    producer = ArchiveProducer(Renderer(*RuleSet("io").get_default_rules()))
    return producer.make_tar_io(cluster_id)


def upload_archive(url, tario, filename="archive.tar"):
    """Upload archive via multipart POST, using only stdlib."""
    boundary = f"----PythonBoundary{uuid.uuid4().hex}"
    body = BytesIO()

    body.write(f"--{boundary}\r\n".encode())
    body.write(
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        .encode()
    )
    body.write(b"Content-Type: application/octet-stream\r\n\r\n")
    body.write(tario.read())
    body.write(f"\r\n--{boundary}--\r\n".encode())

    data = body.getvalue()
    req = Request(
        url,
        data=data,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        resp = urlopen(req, timeout=30)
        return resp.status
    except URLError as e:
        if hasattr(e, "code"):
            return e.code
        raise


def run_continuous(args, make_archive_fn):
    """Send archives continuously for the configured duration."""
    duration_sec = args.duration * 60
    print(f"\n{'='*60}")
    print(f"CONTINUOUS MODE: {args.duration} min, delay={args.delay}s")
    print(f"Bad archive ratio: {args.bad_ratio:.0%}")
    print(f"Target: {args.url}")
    print(f"{'='*60}\n")

    start = time.time()
    sent = 0
    bad_sent = 0

    while (time.time() - start) < duration_sec:
        cluster_id = str(uuid.uuid4())
        is_bad = args.bad_ratio > 0 and random.random() < args.bad_ratio

        if is_bad:
            tario = make_bad_archive(cluster_id)
        else:
            tario = make_archive_fn(cluster_id)

        try:
            status = upload_archive(args.url, tario)
        except Exception as e:
            print(f"  Upload failed: {e}")
            time.sleep(1)
            continue

        sent += 1
        if is_bad:
            bad_sent += 1
        elapsed_min = (time.time() - start) / 60

        if sent % 100 == 0:
            print(
                f"[{elapsed_min:.1f}min] Sent {sent} ({bad_sent} bad) "
                f"(Status: {status})"
            )

        time.sleep(args.delay)

    print(f"\n{'='*60}")
    print(f"COMPLETE — {sent} archives ({bad_sent} bad) in {args.duration} min")
    print(f"{'='*60}\n")


def run_burst(args, make_archive_fn):
    """Send archives in burst/break cycles."""
    burst_sec = 10 * 60
    break_sec = 1 * 60
    num_cycles = max(1, int(args.duration / 11))

    print(f"\n{'='*60}")
    print(f"BURST MODE: {num_cycles} cycles of (10min send + 1min break)")
    print(f"Bad archive ratio: {args.bad_ratio:.0%}")
    print(f"Target: {args.url}")
    print(f"{'='*60}\n")

    total_sent = 0
    total_bad = 0

    for cycle in range(num_cycles):
        print(f"\n--- Cycle {cycle + 1}/{num_cycles} — SENDING ---")
        burst_start = time.time()
        sent = 0
        bad_sent = 0

        while (time.time() - burst_start) < burst_sec:
            cluster_id = str(uuid.uuid4())
            is_bad = args.bad_ratio > 0 and random.random() < args.bad_ratio

            if is_bad:
                tario = make_bad_archive(cluster_id)
            else:
                tario = make_archive_fn(cluster_id)

            try:
                status = upload_archive(args.url, tario)
            except Exception as e:
                print(f"  Upload failed: {e}")
                time.sleep(1)
                continue

            sent += 1
            if is_bad:
                bad_sent += 1

            if sent % 100 == 0:
                elapsed = time.time() - burst_start
                print(
                    f"  [Cycle {cycle+1}] Sent {sent} ({bad_sent} bad) "
                    f"in {elapsed:.0f}s (Status: {status})"
                )

            time.sleep(args.delay)

        total_sent += sent
        total_bad += bad_sent
        print(f"  Cycle {cycle+1} done: {sent} archives ({bad_sent} bad)")

        if cycle < num_cycles - 1:
            print(f"  BREAK — {break_sec}s (watch for memory release)")
            time.sleep(break_sec)

    print(f"\n{'='*60}")
    print(f"COMPLETE — {total_sent} archives ({total_bad} bad) over {num_cycles} cycles")
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
        "--delay", type=float, default=0.5,
        help="Seconds between uploads (default: 0.5)",
    )
    parser.add_argument(
        "--bad-ratio", type=float, default=0.3,
        help="Fraction of bad archives 0.0-1.0 (default: 0.3)",
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
        "--use-molodec", action="store_true",
        help="Use molodec for realistic OCP archives (requires molodec installed)",
    )
    args = parser.parse_args()

    if args.use_molodec:
        try:
            from molodec.archive_producer import ArchiveProducer  # noqa: F401
        except ImportError:
            print(
                "ERROR: --use-molodec requires molodec to be installed.\n"
                "Install with:\n"
                "  export PIP_INDEX_URL="
                "https://repository.engineering.redhat.com/nexus/repository/"
                "insights-qe/simple\n"
                "  pip install -U molodec",
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
