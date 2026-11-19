#!/usr/bin/env python3
"""Seed the running stack with synthetic, fake-PHI data.

1. Generates 10 synthetic studies (7 patients; CT/MR/CR/PT; names, MRNs, birth dates and
   addresses are invented) and C-STOREs them to the de-identification gateway.
2. Sends matching HL7v2 messages (ADT^A01, ORM^O01, ORU^R01 with lung-cancer diagnoses for
   three patients) to the Mirth MLLP listener, if it is running.

Optional: ``--tcia COLLECTION`` additionally downloads one small public study from the TCIA
NBIA API and pushes it through the same gateway.

Usage: python scripts/seed.py [--host 127.0.0.1] [--dicom-port 24112] [--mllp-port 26661]
"""

from __future__ import annotations

import argparse
import io
import json
import os
import socket
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "services" / "indexer"))

from pydicom.dataset import Dataset  # noqa: E402

from scoutoss_indexer.synthetic import LUNG_CANCER_MRNS, PATIENTS, default_cohort, make_instance  # noqa: E402

MLLP_START, MLLP_END = b"\x0b", b"\x1c\x0d"


def dotenv(name: str, default: str) -> str:
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    return os.getenv(name, default)


def log(msg: str, **fields: object) -> None:
    print(json.dumps({"service": "seed", "msg": msg, **fields}), flush=True)


def send_datasets(host: str, port: int, datasets: list[Dataset], called_ae: str) -> int:
    from pynetdicom import AE, StoragePresentationContexts

    ae = AE(ae_title="SCOUTSEED")
    ae.requested_contexts = StoragePresentationContexts[:120]
    assoc = ae.associate(host, port, ae_title=called_ae)
    if not assoc.is_established:
        raise SystemExit(f"could not associate with gateway {host}:{port}")
    ok = 0
    try:
        for ds in datasets:
            status = assoc.send_c_store(ds)
            if status and int(status.Status) == 0:
                ok += 1
    finally:
        assoc.release()
    return ok


def seed_dicom(host: str, port: int, project: str) -> int:
    total = 0
    for study in default_cohort():
        datasets = [make_instance(study, s, i) for s in study.series for i in range(s.slices)]
        sent = send_datasets(host, port, datasets, project)
        total += sent
        log("study sent", modality=study.series[0].modality, instances=sent)
    return total


def hl7_messages() -> list[str]:
    """One admission per patient; lung-cancer diagnoses + CT orders + labs for three."""
    msgs = []
    for i, p in enumerate(PATIENTS):
        family, given = p.name.split("^")[:2]
        pid = (
            f"PID|1||{p.mrn}^^^SPRINGFIELD^MR||{family}^{given}||{p.birth_date:%Y%m%d}|{p.sex}|||"
            "742 Evergreen Terrace^^Springfield^IL^62704||555-867-5309"
        )
        ts = f"2022011{i % 10}0930"

        def msh(kind: str, ctrl: str, ts: str = ts) -> str:
            return f"MSH|^~\\&|EPIC|SPRINGFIELD|SCOUTOSS|SCOUT|{ts}||{kind}|{ctrl}|P|2.5.1"

        dg1 = ""
        if p.mrn in LUNG_CANCER_MRNS:
            dg1 = (
                "\rDG1|1|I10|C34.90^Malignant neoplasm of unspecified part of bronchus or lung^I10||"
                + ts
                + "|F"
            )
        else:
            dg1 = "\rDG1|1|I10|I10^Essential (primary) hypertension^I10||" + ts + "|F"
        msgs.append(
            f"{msh('ADT^A01^ADT_A01', f'ADT{i:04d}')}\rEVN|A01|{ts}\r{pid}\rPV1|1|I|ONC^101^1{dg1}"
        )
        if p.mrn in LUNG_CANCER_MRNS:
            msgs.append(
                f"{msh('ORM^O01^ORM_O01', f'ORM{i:04d}')}\r{pid}\rORC|NW|ORD{i:04d}\r"
                f"OBR|1|ORD{i:04d}||CTCHEST^CT chest with contrast^L|||{ts}"
            )
            msgs.append(
                f"{msh('ORU^R01^ORU_R01', f'ORU{i:04d}')}\r{pid}\rOBR|1|ORD{i:04d}||LAB^Panel^L|||{ts}\r"
                f"OBX|1|NM|2160-0^Creatinine^LN||1.{i}|mg/dL|0.6-1.3|{'H' if i > 2 else 'N'}|||F\r"
                f"OBX|2|NM|1751-7^Albumin^LN||3.{i}|g/dL|3.5-5.0|L|||F"
            )
    return msgs


def send_mllp(host: str, port: int, message: str, timeout: float = 15) -> str:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(MLLP_START + message.encode() + MLLP_END)
        data = b""
        while not data.endswith(MLLP_END):
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    return data.strip(MLLP_START + MLLP_END).decode(errors="replace")


def seed_hl7(host: str, port: int) -> int:
    try:
        socket.create_connection((host, port), timeout=3).close()
    except OSError:
        log("MLLP listener not reachable; skipping HL7 seed", port=port)
        return 0
    acked = 0
    for msg in hl7_messages():
        ack = send_mllp(host, port, msg)
        if "MSA|AA" in ack or "MSA|CA" in ack:
            acked += 1
        else:
            log(
                "HL7 message not accepted", ack=ack.split("\r")[1][:40] if "\r" in ack else ack[:40]
            )
    log("HL7 messages sent", acked=acked)
    return acked


def seed_tcia(
    host: str, port: int, project: str, collection: str
) -> int:  # pragma: no cover - network
    import httpx
    import pydicom

    api = "https://services.cancerimagingarchive.net/nbia-api/services/v1"
    series = httpx.get(
        f"{api}/getSeries", params={"Collection": collection, "Modality": "CT"}, timeout=60
    ).json()
    if not series:
        raise SystemExit(f"no CT series in TCIA collection {collection}")
    smallest = min(series, key=lambda s: int(s.get("ImageCount", 10_000)))
    log("downloading TCIA series", collection=collection, images=smallest.get("ImageCount"))
    blob = httpx.get(
        f"{api}/getImage", params={"SeriesInstanceUID": smallest["SeriesInstanceUID"]}, timeout=600
    ).content
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(io.BytesIO(blob)) as zf:
        zf.extractall(tmp)
        datasets = [pydicom.dcmread(p) for p in sorted(Path(tmp).rglob("*.dcm"))]
    return send_datasets(host, port, datasets, project)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--dicom-port", type=int, default=int(dotenv("DICOM_PORT", "24112")))
    parser.add_argument("--mllp-port", type=int, default=int(dotenv("MLLP_PORT", "26661")))
    parser.add_argument("--project", default=dotenv("DEFAULT_PROJECT", "SCOUT_DEMO"))
    parser.add_argument("--skip-dicom", action="store_true")
    parser.add_argument("--skip-hl7", action="store_true")
    parser.add_argument("--tcia", metavar="COLLECTION", help="also push one small TCIA CT series")
    args = parser.parse_args()
    start = time.time()
    instances = 0 if args.skip_dicom else seed_dicom(args.host, args.dicom_port, args.project)
    hl7 = 0 if args.skip_hl7 else seed_hl7(args.host, args.mllp_port)
    if args.tcia:
        instances += seed_tcia(args.host, args.dicom_port, args.project, args.tcia)
    log(
        "seed complete",
        instances=instances,
        hl7_messages=hl7,
        seconds=round(time.time() - start, 1),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
