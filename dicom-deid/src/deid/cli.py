"""Command line entry point: ``python -m deid <command>``.

The CLI calls the same service functions as the API. Each command acts with
the one role it needs; ``--user`` is recorded in the audit trail, and the
author/approver separation is still enforced by user name.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import service
from .service import Actor, ServiceError
from .store import Store


def _actor(args, role: str) -> Actor:
    return Actor(user=args.user, roles=frozenset({role}))


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _find_template(store: Store, code: str) -> dict:
    row = store.one("SELECT id FROM redaction_template WHERE code = ? ORDER BY id DESC LIMIT 1", (code,))
    if row is None:
        raise ServiceError("NOT_FOUND", f"template {code} not found", 404)
    return service.get_template(store, row["id"])


def cmd_template_create(store: Store, args) -> None:
    body = json.loads(Path(args.file).read_text(encoding="utf-8"))
    shared = body.get("shared", args.study is None)
    _print(service.create_template(
        store, _actor(args, "TEMPLATE_AUTHOR"), code=body["code"], name=body["name"],
        study_id=None if shared else args.study, spec_raw=body["spec"], reason=args.reason,
    ))


def cmd_template_dry_run(store: Store, args) -> None:
    t = _find_template(store, args.code)
    version = args.version or t["versions"][-1]["version"]
    results = []
    for sample in args.samples:
        r = service.dry_run(store, _actor(args, "TEMPLATE_AUTHOR"), t["templateId"], version, Path(sample))
        results.append({"sample": Path(sample).name, "matched": r["matched"], "score": r["score"],
                        "pixelsChanged": r.get("pixel", {}).get("pixelsChanged"), "sampleId": r["sampleId"]})
    _print(results)


def cmd_template_submit(store: Store, args) -> None:
    t = _find_template(store, args.code)
    _print(service.submit(store, _actor(args, "TEMPLATE_AUTHOR"), t["templateId"], args.version, args.reason))


def cmd_template_approve(store: Store, args) -> None:
    t = _find_template(store, args.code)
    _print(service.approve(store, _actor(args, "TEMPLATE_APPROVER"), t["templateId"], args.version,
                           meaning=args.meaning, reason=args.reason))


def cmd_template_list(store: Store, args) -> None:
    _print(service.list_templates(store, _actor(args, "TEMPLATE_AUTHOR"), args.study))


def cmd_process(store: Store, args) -> None:
    job = service.create_job(store, _actor(args, "SITE_UPLOADER"), study_id=args.study, site_id=args.site,
                             subject_no=args.subject, upload=Path(args.path), source_name=Path(args.path).name)
    job = service.run_job(store, job["jobId"])
    _print(job)


def cmd_match_test(store: Store, args) -> None:
    _print(service.match_test(store, _actor(args, "TEMPLATE_AUTHOR"), args.study, Path(args.sample)))


def cmd_audit_verify(store: Store, args) -> None:
    ok, broken = store.verify_audit_chain()
    _print({"chainValid": ok, "firstBrokenId": broken})
    if not ok:
        sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="deid", description="DICOM de-identification (Phase 1 MVP)")
    p.add_argument("--data-dir", default=os.environ.get("DEID_DATA_DIR", "./data"))
    p.add_argument("--user", default=os.environ.get("USER", "cli"), help="user recorded in the audit trail")
    sub = p.add_subparsers(dest="command", required=True)

    t = sub.add_parser("template", help="manage redaction templates").add_subparsers(dest="action", required=True)
    c = t.add_parser("create", help="create a template from a JSON file (version 1, DRAFT)")
    c.add_argument("file")
    c.add_argument("--study", help="study id; omit for a shared template")
    c.add_argument("--reason", default="initial version")
    c.set_defaults(func=cmd_template_create)
    d = t.add_parser("dry-run", help="match samples against a version and render previews")
    d.add_argument("code")
    d.add_argument("samples", nargs="+")
    d.add_argument("--version", type=int)
    d.set_defaults(func=cmd_template_dry_run)
    s = t.add_parser("submit", help="DRAFT -> IN_REVIEW")
    s.add_argument("code")
    s.add_argument("--version", type=int, default=1)
    s.add_argument("--reason", default="ready for review")
    s.set_defaults(func=cmd_template_submit)
    a = t.add_parser("approve", help="IN_REVIEW -> APPROVED (must be a different user than the author)")
    a.add_argument("code")
    a.add_argument("--version", type=int, default=1)
    a.add_argument("--meaning", default="승인함")
    a.add_argument("--reason", default="dry-run results reviewed")
    a.set_defaults(func=cmd_template_approve)
    ls = t.add_parser("list")
    ls.add_argument("--study")
    ls.set_defaults(func=cmd_template_list)

    pr = sub.add_parser("process", help="de-identify a .dcm or .zip for one subject")
    pr.add_argument("path")
    pr.add_argument("--study", required=True)
    pr.add_argument("--site", required=True)
    pr.add_argument("--subject", required=True)
    pr.set_defaults(func=cmd_process)

    m = sub.add_parser("match-test", help="show which approved template a file would get")
    m.add_argument("sample")
    m.add_argument("--study", required=True)
    m.set_defaults(func=cmd_match_test)

    v = sub.add_parser("audit-verify", help="verify the audit trail hash chain")
    v.set_defaults(func=cmd_audit_verify)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    store = Store(args.data_dir)
    try:
        args.func(store, args)
    except ServiceError as exc:
        _print({"code": exc.code, "message": str(exc)})
        sys.exit(2)


if __name__ == "__main__":
    main()
