#!/usr/bin/env python3
"""本地判分入口：swebench harness 的 docker-rt 适配 wrapper。

docker-rt 与 swebench 本地判分路径有两处环境不兼容（均不修改 daemon 或
harness 安装文件，仅在本进程内 patch）：

1. /images/json 列表响应含 inspect 404 的幽灵条目（本机其他项目的
   pier-egress 镜像，不可删除），swebench 的 list_images 逐条 inspect_image
   会抛 ImageNotFound。改用原始列表响应的 RepoTags 取 tags——信息等价，
   不做逐条 inspect。
2. 镜像 WorkingDir 不被保留（docker run <sweb image> pwd 得到 /），而
   swebench 本地判分路径的 git apply / 测试执行不带 cd /testbed、依赖容器
   WorkingDir。SWE-bench 约定 repo 在 /testbed，故对 swebench 特征的容器
   创建（command="tail -f /dev/null"）显式注入 working_dir="/testbed"。

用法（参数与 `swebench eval verified` 对齐，默认 timeout 1800、单进程）：

    DOCKER_HOST=unix:///tmp/docker-rt.sock python grade.py \\
        --predictions results/<run-id>/<variant>/preds.jsonl \\
        --instance-ids <instance_id> [...] \\
        --run-id <id> --report-dir results/<run-id>/eval
"""
import argparse

from docker.models.containers import ContainerCollection
from swebench.harness import reporting
from swebench.image_builder import prepare_images


def safe_list_images(client):
    tags = set()
    for r in client.api.images(all=True):
        for t in r.get("RepoTags") or []:
            tags.add(t)
    return tags


def _create_with_testbed(self, image, *args, **kwargs):
    if kwargs.get("command") == "tail -f /dev/null" and "working_dir" not in kwargs:
        kwargs["working_dir"] = "/testbed"
    return ContainerCollection._orig_create(self, image, *args, **kwargs)


ContainerCollection._orig_create = ContainerCollection.create
ContainerCollection.create = _create_with_testbed
reporting.list_images = safe_list_images
prepare_images.list_images = safe_list_images

from swebench.cli._datasets import resolve_dataset  # noqa: E402
from swebench.harness.run_evaluation import main  # noqa: E402


def parse_args():
    ap = argparse.ArgumentParser(
        description="Grade a run's preds.jsonl with the swebench harness "
                    "(docker-rt compatible).")
    ap.add_argument("--predictions", required=True,
                    help="path to the run's preds.jsonl")
    ap.add_argument("--instance-ids", nargs="+", required=True,
                    help="instance ids to grade")
    ap.add_argument("--run-id", required=True, help="evaluation run id")
    ap.add_argument("--report-dir", default=".",
                    help="directory for the report json")
    ap.add_argument("--dataset", default="verified",
                    help="dataset alias (verified, lite, full, multimodal)")
    ap.add_argument("--timeout", type=int, default=1800,
                    help="per-instance seconds")
    ap.add_argument("--max-workers", type=int, default=1)
    return ap.parse_args()


if __name__ == "__main__":
    args = parse_args()
    main(
        dataset_name=resolve_dataset(args.dataset),
        split="test",
        instance_ids=args.instance_ids,
        predictions_path=args.predictions,
        max_workers=args.max_workers,
        open_file_limit=4096,
        run_id=args.run_id,
        timeout=args.timeout,
        rewrite_reports=False,
        modal=False,
        report_dir=args.report_dir,
    )
