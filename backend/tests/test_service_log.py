"""Log configuration with temporary files and in-memory streams only."""
import io
import logging
import logging.config
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.service_log import LOG_CONFIG, redact, rotate_log

NAMES = ("", "uvicorn", "uvicorn.error", "uvicorn.access", "app", "httpx")


class ServiceLog(unittest.TestCase):
    def setUp(self):
        saved = {name:(logging.getLogger(name).handlers[:],logging.getLogger(name).level,logging.getLogger(name).propagate) for name in NAMES}
        def restore():
            for name,(handlers,level,propagate) in saved.items():
                logger = logging.getLogger(name)
                logger.handlers[:] = handlers
                logger.setLevel(level)
                logger.propagate = propagate
        self.addCleanup(restore)

    def configured(self):
        stream = io.StringIO()
        with patch("sys.stderr",stream),patch("sys.stdout",stream):
            logging.config.dictConfig(LOG_CONFIG)
        return stream

    def test_lines_have_timestamps_and_third_party_info_stays_out(self):
        stream = self.configured()
        logging.getLogger("app.planner").info("Run synthetic completed after 1.0s")
        logging.getLogger("uvicorn.error").info("Model request completed")
        logging.getLogger("httpx").info("HTTP Request: GET https://mcp.amap.com/mcp?key=synthetic")
        lines = stream.getvalue().splitlines()
        self.assertEqual(len(lines),2)
        self.assertTrue(all(re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}",line) for line in lines))
        self.assertIn("app.planner: Run synthetic completed",lines[0])

    def test_access_log_masks_map_keys_in_request_paths(self):
        stream = self.configured()
        logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d',"127.0.0.1:1","GET",
            "/_AMapService/v3/log/init?key=abc123&jscode=secret&t=1","1.1",200)
        output = stream.getvalue()
        self.assertNotIn("abc123",output)
        self.assertNotIn("secret",output)
        self.assertIn("t=1",output)
        self.assertEqual(redact("maps?city=杭州&key=abc"),"maps?city=杭州&key=[已隐藏]")
        self.assertEqual(redact("monkey=banana"),"monkey=banana")

    def test_rotation_keeps_three_old_logs_and_skips_small_files(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "server.log"
            log.write_text("small",encoding="utf-8")
            self.assertFalse(rotate_log(log,limit=100))
            for generation in range(5):
                log.write_text(str(generation)*200,encoding="utf-8")
                self.assertTrue(rotate_log(log,limit=100,keep=3))
            self.assertFalse(log.exists())
            self.assertEqual(sorted(path.name for path in Path(folder).iterdir()),["server.log.1","server.log.2","server.log.3"])
            self.assertEqual(Path(folder,"server.log.1").read_text(encoding="utf-8")[0],"4")
            self.assertEqual(Path(folder,"server.log.3").read_text(encoding="utf-8")[0],"2")


if __name__ == "__main__":
    unittest.main()
