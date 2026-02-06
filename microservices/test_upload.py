#!/usr/bin/env python3
"""
Upload test archives to EDP ACM addon ingress service.

Setup:
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt

    # Install molodec
    export PIP_INDEX_URL=https://repository.engineering.redhat.com/nexus/repository/insights-qe/simple
    pip install -U molodec

Usage:
    python test_upload.py upload https://$INGRESS_URL
"""

import json
import tarfile
from io import BytesIO

import click
import requests
from molodec.archive_producer import ArchiveProducer
from molodec.crc import CONTENT_TYPE
from molodec.renderer import Renderer
from molodec.rules import RuleSet

CLUSTER_ID = "181862b9-c53b-4ea9-ae22-ac4415e2cf21"
IDENTITY_HEADER = "eyJpZGVudGl0eSI6IHsidHlwZSI6ICJVc2VyIiwgImFjY291bnRfbnVtYmVyIjogIjAwMDAwMDEiLCAib3JnX2lkIjogIjAwMDAwMSIsICJpbnRlcm5hbCI6IHsib3JnX2lkIjogIjAwMDAwMSJ9fX0="


def upload_ocp_recommendations(ingress_url):
    producer = ArchiveProducer(Renderer(*RuleSet("io").get_default_rules()))
    tario = producer.make_tar_io(CLUSTER_ID)

    ingress_endpoint = f"{ingress_url}/api/ingress/v1/upload"

    print(f"Uploading to: {ingress_endpoint}")
    response = requests.post(
        ingress_endpoint,
        files={"file": ("archive", tario.getvalue(), CONTENT_TYPE)},
        headers={"x-rh-identity": IDENTITY_HEADER},
        verify=False
    )

    print(f"Status Code: {response.status_code}")
    if response.status_code == 202:
        print("✅ Archive uploaded successfully!")
    else:
        print(f"Response: {response.text}")


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli():
    pass


@cli.command("upload")
@click.argument("ingress_url")
def _upload(ingress_url):
    upload_ocp_recommendations(ingress_url.rstrip('/'))


if __name__ == "__main__":
    cli()
