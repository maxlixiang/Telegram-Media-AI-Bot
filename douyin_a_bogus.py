import logging
import re
import subprocess
from pathlib import Path


SCRIPT_PATH = Path(__file__).with_name("douyin_a_bogus.js")
logger = logging.getLogger(__name__)


def get_a_bogus(target_query: str, user_agent: str, cookie_str: str) -> tuple[str, int, str, str]:
    process = subprocess.run(
        ["node", str(SCRIPT_PATH), target_query, user_agent, cookie_str],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    stdout = process.stdout.strip()
    stderr = process.stderr.strip()
    match = re.search(r"a_bogus:\s*(.+)", stdout)
    if process.returncode != 0 or not match:
        logger.error(
            "抖音签名失败 node_returncode=%s node_stdout_preview=%s node_stderr_preview=%s",
            process.returncode,
            stdout[:300],
            stderr[:300],
        )
        raise RuntimeError("抖音签名生成失败")
    return match.group(1).strip(), process.returncode, stdout, stderr
