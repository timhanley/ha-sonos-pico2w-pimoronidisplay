# CPython smoke tests for the hardware-free modules.
# Run with: python3 -m unittest discover tests
import sys
import unittest
from os.path import dirname

sys.path.insert(0, dirname(dirname(__file__)))

from app.httpc import build_request, parse_url  # noqa: E402
from app.textutil import wrap_two_lines  # noqa: E402


class TestParseUrl(unittest.TestCase):
    def test_host_port_path(self):
        self.assertEqual(parse_url("http://192.168.1.10:8123/api/states/x"),
                         ("192.168.1.10", 8123, "/api/states/x"))

    def test_default_port(self):
        self.assertEqual(parse_url("http://ha.local/api"), ("ha.local", 80, "/api"))

    def test_no_path(self):
        self.assertEqual(parse_url("http://ha.local:8123"), ("ha.local", 8123, "/"))

    def test_rejects_https(self):
        with self.assertRaises(ValueError):
            parse_url("https://ha.local/api")


class TestBuildRequest(unittest.TestCase):
    def test_get(self):
        head = build_request("GET", "h", 80, "/p", {"A": "b"}, 0)
        self.assertEqual(head, b"GET /p HTTP/1.0\r\nHost: h:80\r\nA: b\r\n\r\n")

    def test_post_content_length(self):
        head = build_request("POST", "h", 8123, "/p", None, 12)
        self.assertIn(b"Content-Length: 12\r\n", head)
        self.assertTrue(head.endswith(b"\r\n\r\n"))


class TestWrap(unittest.TestCase):
    def test_short_text_single_line(self):
        self.assertEqual(wrap_two_lines("Hello", 20), ("Hello", None))

    def test_breaks_at_space(self):
        self.assertEqual(wrap_two_lines("Hello Wide World", 10), ("Hello", "Wide World"))

    def test_force_split_without_space(self):
        self.assertEqual(wrap_two_lines("Antidisestablish", 10), ("Antidisest", "ablish"))


if __name__ == "__main__":
    unittest.main()
