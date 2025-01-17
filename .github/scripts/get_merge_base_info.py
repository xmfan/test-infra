import subprocess
from datetime import datetime
from multiprocessing import Pool
from pathlib import Path
from typing import List

from rockset_utils import query_rockset, remove_from_rockset, upload_to_rockset

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

FAILED_TEST_SHAS_QUERY = """
SELECT
    DISTINCT j.head_sha,
FROM
    commons.failed_tests_run t
    join workflow_job j on t.job_id = j.id
    left outer join commons.merge_bases mb on j.head_sha = mb.sha
where
    mb.merge_base is null
"""


DUP_MERGE_BASE_INFO = """
select
    ARRAY_AGG(m._id) as ids
from
    commons.merge_bases m
group by
    m.sha
having
    count(*) > 1
"""


def dedup_merge_base_info() -> None:
    ids = []
    for item in query_rockset(DUP_MERGE_BASE_INFO):
        for val in item.values():
            ids.extend(val)
    interval = 500

    for i in range(0, len(ids), interval):
        remove_from_rockset("merge_bases", ids[i : i + interval])


def run_command(command: str) -> str:
    cwd = REPO_ROOT / ".." / "pytorch"
    return (
        subprocess.check_output(
            command.split(" "),
            cwd=cwd,
        )
        .decode("utf-8")
        .strip()
    )


def pull_shas(shas: List[str]) -> None:
    all_shas = " ".join(shas)
    run_command(
        f"git -c protocol.version=2 fetch --no-tags --prune --quiet --no-recurse-submodules origin {all_shas}"
    )


def upload_merge_base_info(shas: List[str]) -> None:
    docs = []
    for sha in shas:
        try:
            merge_base = run_command(f"git merge-base main {sha}")
            if merge_base == sha:
                # The commit was probably already on main, so take the previous
                # commit as the merge base
                merge_base = run_command(f"git rev-parse {sha}^")
            changed_files = run_command(f"git diff {sha} {merge_base} --name-only")
            unix_timestamp = run_command(
                f"git show --no-patch --format=%ct {merge_base}"
            )
            timestamp = datetime.utcfromtimestamp(int(unix_timestamp)).isoformat() + "Z"
            docs.append(
                {
                    "sha": sha,
                    "merge_base": merge_base,
                    "changed_files": changed_files.splitlines(),
                    "merge_base_commit_date": timestamp,
                    "repo": "pytorch/pytorch",
                }
            )
        except Exception as e:
            return e

    upload_to_rockset(collection="merge_bases", docs=docs, workspace="commons")


if __name__ == "__main__":
    dedup_merge_base_info()

    failed_test_shas = [x["head_sha"] for x in query_rockset(FAILED_TEST_SHAS_QUERY)]
    interval = 100
    print(
        f"There are {len(failed_test_shas)} shas, uploading in intervals of {interval}"
    )
    pool = Pool(20)
    errors = []
    for i in range(0, len(failed_test_shas), interval):
        pull_shas(failed_test_shas[i : i + interval])
        errors.append(
            pool.apply_async(
                upload_merge_base_info, args=(failed_test_shas[i : i + interval],)
            )
        )
    print("done pulling")
    pool.close()
    pool.join()
    for i in errors:
        if i.get() is not None:
            print(i.get())
