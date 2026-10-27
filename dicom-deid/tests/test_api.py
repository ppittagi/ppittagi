import pytest
from fastapi.testclient import TestClient

from deid import api

from conftest import make_dataset, write

UPLOADER = {"X-User": "site1", "X-Roles": "SITE_UPLOADER,DM"}
AUTHOR = {"X-User": "author1", "X-Roles": "TEMPLATE_AUTHOR"}
APPROVER = {"X-User": "approver1", "X-Roles": "TEMPLATE_APPROVER"}
REVIEWER = {"X-User": "rev1", "X-Roles": "DEID_REVIEWER"}
BASE = "/api/v1/studies/STUDY001"


@pytest.fixture
def client(store):
    api.app.dependency_overrides[api.get_store] = lambda: store
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


def dcm(tmp_path, name="a.dcm", **kw):
    return ("file", (name, write(make_dataset(**kw), tmp_path / name).read_bytes(), "application/dicom"))


def test_template_workflow_and_job(client, tmp_path, sample_template):
    r = client.post(BASE + "/templates", headers=AUTHOR, json={**sample_template, "reason": "initial"})
    assert r.status_code == 201, r.text
    tid = r.json()["templateId"]
    for i in range(3):
        r = client.post(f"{BASE}/templates/{tid}/versions/1/dry-run", headers=AUTHOR, files=[dcm(tmp_path, f"s{i}.dcm", seed=i)])
        assert r.json()["matched"] is True
    sample_id = r.json()["sampleId"]
    assert client.get(f"{BASE}/template-samples/{sample_id}/preview?stage=after", headers=AUTHOR).headers["content-type"] == "image/png"
    assert client.post(f"{BASE}/templates/{tid}/versions/1/submit", headers=AUTHOR, json={"reason": "ready"}).status_code == 200
    r = client.post(f"{BASE}/templates/{tid}/versions/1/approve", headers=AUTHOR, json={"meaning": "승인함", "reason": "x"})
    assert r.status_code == 403
    r = client.post(f"{BASE}/templates/{tid}/versions/1/approve", headers=APPROVER, json={"meaning": "승인함", "reason": "ok"})
    assert r.json()["versions"][0]["status"] == "APPROVED"

    r = client.post(BASE + "/jobs", headers=UPLOADER, data={"site_id": "S01", "subject_no": "S-01-0001"},
                    files=[dcm(tmp_path, "upload.dcm", seed=9)])
    assert r.status_code == 202
    job_id = r.json()["jobId"]
    job = client.get(f"{BASE}/jobs/{job_id}", headers=UPLOADER).json()  # TestClient runs background tasks first
    assert job["status"] == "COMPLETED" and job["counts"]["done"] == 1
    item = client.get(f"{BASE}/jobs/{job_id}/items", headers=UPLOADER).json()[0]
    r = client.get(f"{BASE}/items/{item['id']}/download", headers=UPLOADER)
    assert r.status_code == 200 and b"CANARY" not in r.content.split(b"\x7f\xe0\x10\x00")[0]
    assert client.get(f"{BASE}/items/{item['id']}/preview?stage=before", headers=UPLOADER).status_code == 403
    assert client.get(f"{BASE}/items/{item['id']}/preview?stage=before", headers=REVIEWER).status_code == 200
    assert client.get(f"/api/v1/studies/OTHER/jobs/{job_id}", headers=UPLOADER).status_code == 404


def test_errors_are_structured(client, sample_template):
    bad = {**sample_template, "reason": "x"}
    bad["spec"] = {**bad["spec"], "regions": [{"x": 900, "y": 0, "w": 200, "h": 10}]}
    r = client.post(BASE + "/templates", headers=AUTHOR, json=bad)
    assert r.status_code == 400 and r.json()["code"] == "TEMPLATE_REGION_OUT_OF_BOUNDS"
    r = client.get(BASE + "/quarantine", headers={"X-User": "x", "X-Roles": "ADMIN"})
    assert r.status_code == 403 and r.json()["code"] == "UNKNOWN_ROLE"
    assert client.get(BASE + "/quarantine", headers=UPLOADER).status_code == 403
