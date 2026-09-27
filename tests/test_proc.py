import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from subflow import proc


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class ProcessTreeTests(unittest.TestCase):
    def test_timeout_kills_grandchildren_and_untracks_process(self):
        with tempfile.TemporaryDirectory() as temp_value:
            pid_file = Path(temp_value) / "grandchild.pid"
            script = f"sleep 30 & echo $! > {pid_file}; wait"
            with self.assertRaises(subprocess.TimeoutExpired):
                proc.run_captured(["/bin/sh", "-c", script], timeout=0.5)
            grandchild = int(pid_file.read_text().strip())
            deadline = time.monotonic() + 3
            while _pid_alive(grandchild) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(_pid_alive(grandchild))
        self.assertEqual(proc._live, set())

    def test_run_captured_passes_input_and_replaces_invalid_utf8(self):
        completed = proc.run_captured(
            ["/bin/sh", "-c", "cat; printf '\\377'"],
            input="hello",
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "hello�")

    def test_terminate_all_kills_live_processes(self):
        process = proc.popen(["/bin/sleep", "30"])
        try:
            proc.terminate_all()
            self.assertIsNotNone(process.poll())
        finally:
            proc.release(process)


if __name__ == "__main__":
    unittest.main()
